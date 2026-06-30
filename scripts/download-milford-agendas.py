#!/usr/bin/env python3
# download-milford-agendas.py
# Download municipal meeting agendas, minutes, and video recordings from the
# Milford CT Agenda Center for meetings within the past N days (and up to 7
# days ahead, to catch agendas posted early for upcoming meetings).
#
# USAGE:
#   python3 scripts/download-milford-agendas.py [options]
#
# REQUIREMENTS:
#   - Python 3.6+  (no third-party packages needed for docs)
#   - yt-dlp       (for video: pip install yt-dlp or brew install yt-dlp)
#   - Internet connection
#
# WHAT IT DOES:
#   Documents (default):
#     1. Fetches the Milford CT Agenda Center page (all current-year data is inline)
#     2. Parses each board section for meeting dates, agenda URLs, and minutes URLs
#     3. Downloads PDFs whose meeting date falls in the date window to
#        beat-archive/milford-agendas/YYYY-MM/
#     4. Appends a download log to beat-archive/milford-agendas/download-log.txt
#
#   Video (enabled by default; disable with --no-video):
#     5. Extracts video links from the media column of each meeting row
#     6. YouTube recordings: downloaded with yt-dlp
#     7. Zoom/Teams join links: live meeting URLs; logged but not downloaded
#
# SITE STRUCTURE (CivicPlus CivicEngage):
#   Hub:     https://milfordct.us/AgendaCenter
#   Agenda:  https://milfordct.us/AgendaCenter/ViewFile/Agenda/_MMDDYYYY-ID
#   Minutes: https://milfordct.us/AgendaCenter/ViewFile/Minutes/_MMDDYYYY-ID
#   Video:   href in <td class="media"> — YouTube, Zoom join, or Teams join
#
#   Page layout per meeting row (<tr class="catAgendaRow">):
#     <strong aria-label="Agenda for Month DD, YYYY">
#     <a href="/AgendaCenter/ViewFile/Agenda/_MMDDYYYY-ID">
#     <td class="minutes"><a href="/AgendaCenter/ViewFile/Minutes/...">
#     <td class="media"><span class="videos"><a href="[video URL]">
#
# NOTE: The Agenda Center page embeds only the current year's meetings.
# Older years load via javascript:changeYear(). For a 30-day lookback this
# is always sufficient. If your --days window crosses a year boundary, the
# prior-year data will be missing — use --days ≤ 30 near year end.
#
# NOTE: Zoom and Microsoft Teams links in the media column are live meeting
# join URLs, not recordings. They are printed in the dry-run output and
# logged, but yt-dlp is not called for them.
#
# NOTE: The server at milfordct.us does not require authentication.
# A plain urllib request with a browser-like User-Agent is sufficient.

import argparse
import datetime
import os
import re
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

# --- Configuration ---
BASE_URL = "https://milfordct.us"
HUB_URL = f"{BASE_URL}/AgendaCenter"
OUTPUT_DIR = "beat-archive/milford-agendas"
DAYS_BACK = 4
DAYS_AHEAD = 7
DELAY_SECONDS = 0.8

UA = "Milford-CT-Agendas-Downloader/2.0 (journalism research)"

_URL_DATE_RE = re.compile(r"_(\d{2})(\d{2})(\d{4})-\d+")

# Matches YouTube watch or live video URLs
_YT_RE = re.compile(
    r"https?://(?:www\.)?youtube\.com/[^\s\"'<>]+",
    re.IGNORECASE,
)

# Matches Zoom or Teams join links (live meetings, not recordings)
_ZOOM_TEAMS_RE = re.compile(
    r"https?://[^\s\"'<>]*(?:zoom\.us/j/|teams\.microsoft\.com/meet/)[^\s\"'<>]+",
    re.IGNORECASE,
)


# --- HTML helpers ---

def fetch_html(url):
    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": UA,
            "Accept": "text/html,application/xhtml+xml,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.5",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            charset = r.headers.get_content_charset() or "utf-8"
            return r.read().decode(charset, errors="replace")
    except urllib.error.HTTPError as e:
        print(f"  HTTP {e.code} — {url}", file=sys.stderr)
        return None
    except urllib.error.URLError as e:
        print(f"  ERROR fetching {url}: {e}", file=sys.stderr)
        return None


def download_pdf(path, dest_path):
    """Download BASE_URL + path to dest_path. Returns True on success."""
    url = (BASE_URL + path) if path.startswith("/") else path
    url = url.split("?")[0]  # strip ?html=true if present — always get PDF
    req = urllib.request.Request(
        url,
        headers={"User-Agent": UA, "Accept": "application/pdf,*/*"},
    )
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            data = r.read()
        if not data:
            print(f"  WARNING: empty response for {url}", file=sys.stderr)
            return False
        os.makedirs(os.path.dirname(dest_path), exist_ok=True)
        with open(dest_path, "wb") as f:
            f.write(data)
        return True
    except Exception as e:
        print(f"  WARNING: {e}", file=sys.stderr)
        return False


def download_video(video_url, dest_path):
    """Download a YouTube video with yt-dlp. Returns True on success."""
    cmd = [
        "yt-dlp", "--js-runtimes", "node",
        "--no-playlist",
        "-f", "bestvideo+bestaudio/best",
        "--merge-output-format", "mp4",
        "-o", dest_path,
        "--no-overwrites",
        "--quiet",
        "--no-warnings",
        video_url,
    ]
    try:
        subprocess.run(cmd, check=True)
        return True
    except FileNotFoundError:
        print("  ERROR: yt-dlp not found. Install with: pip install yt-dlp", file=sys.stderr)
        return False
    except subprocess.CalledProcessError as e:
        print(f"  WARNING: yt-dlp failed (exit {e.returncode}) for {video_url}", file=sys.stderr)
        return False


# --- Parsing ---

def parse_boards(html):
    """Return list of (cat_id, board_name) tuples from the AgendaCenter page."""
    return [
        (cat_id, name.strip())
        for cat_id, name in re.findall(
            r'aria-controls="category-panel-(\d+)"[^>]*>\s*([^<]+)\s*</h2>', html
        )
    ]


def parse_rows(html, cat_id):
    """
    Parse meeting rows from category panel cat_id.
    Returns list of dicts: {date, agenda_url, minutes_url, video_url, video_type, title}.
    video_type is 'youtube', 'zoom', 'teams', or None.
    """
    panel_start = html.find(f'id="category-panel-{cat_id}"')
    if panel_start < 0:
        return []
    next_panel = html.find('id="category-panel-', panel_start + 1)
    chunk = html[panel_start: next_panel if next_panel > 0 else len(html)]

    rows = re.findall(r'<tr[^>]+class="catAgendaRow"[^>]*>(.*?)</tr>', chunk, re.DOTALL)
    items = []
    for row in rows:
        # Date from aria-label
        date_m = re.search(r'aria-label="Agenda for ([^"]+)"', row)
        if date_m:
            try:
                meeting_date = datetime.datetime.strptime(
                    date_m.group(1), "%B %d, %Y"
                ).date()
            except ValueError:
                meeting_date = None
        else:
            meeting_date = None

        # Fallback: parse date from the agenda URL (_MMDDYYYY-ID)
        if not meeting_date:
            url_date_m = _URL_DATE_RE.search(row)
            if url_date_m:
                try:
                    meeting_date = datetime.date(
                        int(url_date_m.group(3)),
                        int(url_date_m.group(1)),
                        int(url_date_m.group(2)),
                    )
                except ValueError:
                    pass
        if not meeting_date:
            continue

        agenda_m = re.search(r'href="(/AgendaCenter/ViewFile/Agenda/[^"?]+)', row)
        minutes_m = re.search(
            r'<td class="minutes">.*?href="(/AgendaCenter/ViewFile/Minutes/[^"?]+)',
            row, re.DOTALL,
        )

        # Video: href inside <td class="media">
        media_m = re.search(
            r'<td class="media">.*?<a\s[^>]*href="([^"]+)"',
            row, re.DOTALL,
        )
        video_url = media_m.group(1) if media_m else None
        video_type = _classify_video(video_url)

        title_m = re.search(r'<p[^>]*>.*?<a[^>]+>\s*([^<]+)\s*</a>', row, re.DOTALL)
        title = " ".join(title_m.group(1).split()) if title_m else ""

        items.append({
            "date": meeting_date,
            "agenda_url": agenda_m.group(1) if agenda_m else None,
            "minutes_url": minutes_m.group(1) if minutes_m else None,
            "video_url": video_url,
            "video_type": video_type,
            "title": title,
        })
    return items


def _classify_video(url):
    if not url:
        return None
    if _YT_RE.match(url):
        return "youtube"
    low = url.lower()
    if "zoom.us/j/" in low:
        return "zoom"
    if "teams.microsoft.com/meet/" in low:
        return "teams"
    return "other"


# --- Utilities ---

def slugify(text, max_len=50):
    text = text.lower().strip()
    text = re.sub(r"[/\\&]", "-", text)
    text = re.sub(r"\s+-\s+", "-", text)
    text = re.sub(r"[^\w\s-]", "", text)
    text = re.sub(r"[\s_]+", "-", text)
    text = re.sub(r"-{2,}", "-", text)
    return text.strip("-")[:max_len]


def _doc_id(url):
    """Extract the numeric ID from a ViewFile URL like /AgendaCenter/ViewFile/Agenda/_MMDDYYYY-1234."""
    m = re.search(r"-(\d+)$", url.rstrip("/").split("?")[0])
    return m.group(1) if m else None


def make_dest_path(board, doc_type, meeting_date, output_dir, doc_id=None, ext=".pdf"):
    date_prefix = meeting_date.strftime("%Y-%m-%d")
    month_dir = os.path.join(output_dir, meeting_date.strftime("%Y-%m"))
    board_slug = slugify(board, max_len=40)
    suffix = f"-{doc_id}" if doc_id else ""
    return os.path.join(month_dir, f"{date_prefix}-{board_slug}-{doc_type}{suffix}{ext}")


# --- Main ---

def main():
    parser = argparse.ArgumentParser(
        description=(
            "Download Milford CT municipal agendas, minutes, and video recordings "
            "from the Agenda Center for meetings within the date window."
        )
    )
    parser.add_argument(
        "--days", type=int, default=DAYS_BACK, metavar="N",
        help=f"Look back N days (default: {DAYS_BACK})",
    )
    parser.add_argument(
        "--ahead", type=int, default=DAYS_AHEAD, metavar="N",
        help=f"Include meetings up to N days ahead (default: {DAYS_AHEAD})",
    )
    parser.add_argument(
        "--output-dir", default=OUTPUT_DIR, metavar="DIR",
        help=f"Destination directory (default: {OUTPUT_DIR})",
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="List matching items without downloading",
    )
    parser.add_argument(
        "--board", metavar="NAME",
        help="Only process boards whose name contains NAME (case-insensitive)",
    )
    parser.add_argument(
        "--no-agendas", action="store_true",
        help="Skip agenda PDFs",
    )
    parser.add_argument(
        "--no-minutes", action="store_true",
        help="Skip minutes PDFs",
    )
    parser.add_argument(
        "--no-video", action="store_true",
        help="Skip video recordings (YouTube); still logs Zoom/Teams join links",
    )
    args = parser.parse_args()

    now = datetime.datetime.now()
    if (now.weekday() == 5 and now.hour >= 18) or (now.weekday() == 6 and now.hour < 12):  # Saturday night, Sunday morning
        print("Skipping — no downloads on Saturday nights or Sunday mornings.")
        sys.exit(0)

    do_video = not args.no_video

    today = datetime.date.today()
    cutoff = today - datetime.timedelta(days=args.days)
    future_limit = today + datetime.timedelta(days=args.ahead)

    print(f"Date window : {cutoff} to {future_limit}")
    print(f"Hub page    : {HUB_URL}")
    if not args.dry_run:
        print(f"Output dir  : {args.output_dir}")
    print()

    # --- Step 1: fetch the Agenda Center page ---
    print("Fetching Agenda Center page...")
    main_html = fetch_html(HUB_URL)
    if not main_html:
        print("ERROR: Could not fetch Agenda Center page.", file=sys.stderr)
        sys.exit(1)

    boards = parse_boards(main_html)
    if not boards:
        print("ERROR: No boards found — page structure may have changed.", file=sys.stderr)
        sys.exit(1)

    print(f"  Parsed {len(boards)} board(s).")

    if args.board:
        filter_str = args.board.lower()
        boards = [(cid, name) for cid, name in boards if filter_str in name.lower()]
        print(f"  Filtered to {len(boards)} board(s) matching '{args.board}'.")

    # --- Step 2: collect matching items ---
    all_items = []

    for cat_id, board_name in boards:
        rows = parse_rows(main_html, cat_id)
        for row in rows:
            if row["date"] < cutoff or row["date"] > future_limit:
                continue

            if not args.no_agendas and row["agenda_url"]:
                all_items.append({
                    "board": board_name,
                    "date": row["date"],
                    "doc_type": "agenda",
                    "url": row["agenda_url"],
                    "doc_id": _doc_id(row["agenda_url"]),
                    "ext": ".pdf",
                })

            if not args.no_minutes and row["minutes_url"]:
                all_items.append({
                    "board": board_name,
                    "date": row["date"],
                    "doc_type": "minutes",
                    "url": row["minutes_url"],
                    "doc_id": _doc_id(row["minutes_url"]),
                    "ext": ".pdf",
                })

            if row["video_url"]:
                vtype = row["video_type"]
                if vtype == "youtube" and do_video:
                    all_items.append({
                        "board": board_name,
                        "date": row["date"],
                        "doc_type": "video",
                        "url": row["video_url"],
                        "doc_id": None,
                        "ext": ".mp4",
                    })
                elif vtype in ("zoom", "teams"):
                    # Join links — not downloadable; record for dry-run / log
                    all_items.append({
                        "board": board_name,
                        "date": row["date"],
                        "doc_type": vtype,
                        "url": row["video_url"],
                        "doc_id": None,
                        "ext": None,
                    })

    all_items.sort(key=lambda x: (x["date"], x["board"]), reverse=True)

    doc_count = sum(1 for x in all_items if x["doc_type"] in ("agenda", "minutes"))
    vid_count = sum(1 for x in all_items if x["doc_type"] == "video")
    join_count = sum(1 for x in all_items if x["doc_type"] in ("zoom", "teams"))
    board_count = len({x["board"] for x in all_items if x["doc_type"] in ("agenda", "minutes", "video")})

    print(
        f"  Found {doc_count} document(s)"
        + (f", {vid_count} recording(s)" if vid_count else "")
        + (f", {join_count} live meeting link(s)" if join_count else "")
        + f" across {board_count} board(s) in date window."
    )
    print()

    if not all_items:
        print("No items to download.")
        return

    # --- Dry run ---
    if args.dry_run:
        print(f"{'Board':<44} {'Date':<12} Type")
        print("-" * 70)
        for item in all_items:
            url_hint = ""
            if item["doc_type"] in ("zoom", "teams", "video"):
                short = item["url"][:55]
                url_hint = f"  {short}..." if len(item["url"]) > 55 else f"  {item['url']}"
            print(
                f"{item['board'][:43]:<44} "
                f"{item['date']!s:<12} "
                f"{item['doc_type']}{url_hint}"
            )
        downloadable = sum(1 for x in all_items if x["doc_type"] in ("agenda", "minutes", "video"))
        print(f"\n{downloadable} item(s). Re-run without --dry-run to download.")
        return

    # --- Step 3: download ---
    os.makedirs(args.output_dir, exist_ok=True)
    log_path = os.path.join(args.output_dir, "download-log.txt")
    log_lines = []
    downloaded = skipped = failed = 0

    for item in all_items:
        if item["doc_type"] in ("zoom", "teams"):
            # Not downloadable — just log the join link
            log_lines.append(
                f"{datetime.datetime.now().isoformat()}  {item['doc_type'].upper():6}  "
                f"{item['board']} | {item['date']} | {item['url']}"
            )
            continue

        dest = make_dest_path(
            item["board"], item["doc_type"], item["date"],
            args.output_dir, doc_id=item.get("doc_id"), ext=item["ext"],
        )
        label = os.path.basename(dest)

        if os.path.exists(dest):
            print(f"  skip (exists)  {label}")
            skipped += 1
            continue

        print(f"  [{item['date']}] {item['board']} — {item['doc_type']}")
        print(f"  downloading    {label}")

        os.makedirs(os.path.dirname(dest), exist_ok=True)

        if item["doc_type"] == "video":
            ok = download_video(item["url"], dest)
        else:
            ok = download_pdf(item["url"], dest)

        if ok:
            downloaded += 1
            log_lines.append(f"{datetime.datetime.now().isoformat()}  OK       {dest}")
        else:
            failed += 1
            log_lines.append(f"{datetime.datetime.now().isoformat()}  FAILED   {dest}")
            if os.path.exists(dest):
                os.remove(dest)

        time.sleep(DELAY_SECONDS)

    if log_lines:
        with open(log_path, "a") as f:
            f.write("\n".join(log_lines) + "\n")

    print()
    print(f"Done — downloaded: {downloaded}  skipped: {skipped}  failed: {failed}")
    if downloaded + skipped:
        print(f"Files in: {args.output_dir}")
    if log_lines:
        print(f"Log:      {log_path}")


if __name__ == "__main__":
    main()


# --- Tips ---
#
# 1. Preview without downloading:
#    python3 scripts/download-milford-agendas.py --dry-run
#
# 2. Narrow to one board:
#    python3 scripts/download-milford-agendas.py --board "Board of Aldermen"
#
# 3. Skip video downloads:
#    python3 scripts/download-milford-agendas.py --no-video
#
# 4. Agendas only:
#    python3 scripts/download-milford-agendas.py --no-minutes --no-video
#
# 5. Change the lookback window:
#    python3 scripts/download-milford-agendas.py --days 7
#
# 6. Save files somewhere else:
#    python3 scripts/download-milford-agendas.py --output-dir ~/Downloads/milford
#
# 7. Run on a schedule (cron — 8 AM daily):
#    0 8 * * * cd /path/to/repo && python3 scripts/download-milford-agendas.py
#
# 8. Process downloaded files with Claude afterward:
#    python3 scripts/download-milford-agendas.py && bash scripts/batch-process.sh beat-archive/milford-agendas/
#
# NOTE: Zoom and Microsoft Teams links in the media column are live meeting
# join URLs — they appear in the dry-run list for reference but cannot be
# downloaded. YouTube recordings (published to the city's channel after the
# meeting) are downloaded with yt-dlp.
#
# NOTE: The Agenda Center page embeds only the current year. For a standard
# 30-day lookback, this is always sufficient.
