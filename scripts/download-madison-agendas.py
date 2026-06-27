#!/usr/bin/env python3
# download-madison-agendas.py
# Download municipal meeting agendas, minutes, and video recordings from
# Madison CT AgendaCenter for meetings whose date falls within the past N days.
#
# USAGE:
#   python3 scripts/download-madison-agendas.py [options]
#
# REQUIREMENTS:
#   - Python 3.6+  (no third-party packages needed for docs)
#   - yt-dlp       (required for --include-video; pip install yt-dlp)
#   - Internet connection
#
# WHAT IT DOES:
#   1. Fetches the Madison CT AgendaCenter listing page
#   2. Finds all agendas, minutes, and (optionally) video recordings whose
#      meeting date falls within the lookback window
#   3. Downloads PDFs to beat-archive/madison-agendas/YYYY-MM/
#   4. Downloads YouTube video recordings via yt-dlp (--include-video flag;
#      videos are typically 500 MB – 2 GB each)
#   5. Appends a download log to beat-archive/madison-agendas/download-log.txt
#
# SITE STRUCTURE:
#   Madison CT uses CivicPlus AgendaCenter (https://www.madisonct.org/AgendaCenter).
#   Board sections are collapsible panels; the current year is pre-loaded in
#   the page HTML. Previous years load via a POST to /AgendaCenter/UpdateCategoryList.
#
#   Document URLs:
#     /AgendaCenter/ViewFile/Agenda/_MMDDYYYY-NNNN   → agenda PDF
#     /AgendaCenter/ViewFile/Minutes/_MMDDYYYY-NNNN  → minutes PDF
#
#   Video links appear in meeting rows as <a href="..."> wrapping an image
#   with alt="Videos". URLs are YouTube (youtu.be or youtube.com).
#   Multiple boards maintain separate YouTube channels:
#     Town of Madison, CT
#     Board of Selectmen
#     Board of Finance
#     Board of Education
#
# NOTE: CivicPlus AgendaCenter exposes meeting dates, not upload/posted dates.
# The script filters by meeting date. A future meeting whose agenda was posted
# early will appear once its meeting date enters the lookback window.

import argparse
import datetime
import glob
import os
import re
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

# --- Configuration ---
BASE_URL = "https://www.madisonct.org"
AGENDA_CENTER_URL = f"{BASE_URL}/AgendaCenter"
UPDATE_URL = f"{BASE_URL}/AgendaCenter/UpdateCategoryList"
OUTPUT_DIR = "beat-archive/madison-agendas"
DAYS_BACK = 4
DELAY_SECONDS = 1

UA = "Madison-Agendas-Downloader/1.0 (journalism research)"

YOUTUBE_RE = re.compile(
    r'href="(https://(?:www\.)?youtu(?:\.be|be\.com)/[^"]+)"',
    re.IGNORECASE,
)


# --- HTTP helpers ---

def fetch_html(url, post_data=None):
    """GET or POST url; return decoded HTML or None on error."""
    req = urllib.request.Request(
        url,
        data=post_data,
        headers={
            "User-Agent": UA,
            "Content-Type": "application/x-www-form-urlencoded" if post_data else "text/html",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return r.read().decode("utf-8", errors="replace")
    except urllib.error.URLError as e:
        print(f"  ERROR fetching {url}: {e}", file=sys.stderr)
        return None


def download_file(path, dest_path):
    """Download BASE_URL + path (PDF) to dest_path. Returns True on success."""
    url = BASE_URL + path if path.startswith("/") else path
    url = url.split("?")[0]  # strip ?html=true if present
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            if r.status != 200:
                print(f"  WARNING: HTTP {r.status} — {url}", file=sys.stderr)
                return False
            with open(dest_path, "wb") as f:
                f.write(r.read())
        return True
    except Exception as e:
        print(f"  WARNING: {e} — {url}", file=sys.stderr)
        return False


def download_video(youtube_url, dest_template):
    """
    Download a YouTube video using yt-dlp.
    dest_template must end in .%(ext)s — yt-dlp fills in the extension.
    Returns True on success.
    """
    cmd = [
        "yt-dlp",
        "--no-playlist",
        "-f", "bestvideo[ext=mp4]+bestaudio[ext=m4a]/best[ext=mp4]/best",
        "--merge-output-format", "mp4",
        "-o", dest_template,
        "--no-overwrites",
        "--quiet",
        "--no-warnings",
        youtube_url,
    ]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=3600)
        if result.returncode != 0 and result.stderr:
            print(f"  WARNING: yt-dlp: {result.stderr.strip()}", file=sys.stderr)
        return result.returncode == 0
    except FileNotFoundError:
        print(
            "  ERROR: yt-dlp not found. Install it with: pip install yt-dlp",
            file=sys.stderr,
        )
        return False
    except subprocess.TimeoutExpired:
        print(f"  WARNING: yt-dlp timed out for {youtube_url}", file=sys.stderr)
        return False
    except Exception as e:
        print(f"  WARNING: yt-dlp error: {e}", file=sys.stderr)
        return False


def video_already_exists(dest_template):
    """Return True if any file matching dest_template (with %(ext)s) already exists."""
    base = dest_template.replace(".%(ext)s", "")
    return bool(glob.glob(base + ".*"))


# --- HTML parsing ---

def parse_boards(html):
    """Return list of (cat_id, board_name) from the AgendaCenter page."""
    pattern = r'aria-controls="category-panel-(\d+)"[^>]*>\s*([^<]+)\s*</h2>'
    return [
        (cat_id, name.strip())
        for cat_id, name in re.findall(pattern, html)
    ]


def _parse_row(row):
    """
    Parse a single catAgendaRow <tr> inner HTML.
    Returns a dict with date, agenda_url, minutes_url, video_url, title,
    or None if the date cannot be parsed.
    """
    date_m = re.search(r'aria-label="Agenda for ([^"]+)"', row)
    if not date_m:
        return None
    try:
        meeting_date = datetime.datetime.strptime(date_m.group(1), "%B %d, %Y").date()
    except ValueError:
        try:
            meeting_date = datetime.datetime.strptime(date_m.group(1), "%B %-d, %Y").date()
        except ValueError:
            return None

    agenda_m = re.search(r'href="(/AgendaCenter/ViewFile/Agenda/[^"?]+)', row)
    minutes_m = re.search(r'href="(/AgendaCenter/ViewFile/Minutes/[^"?]+)', row)
    video_m = YOUTUBE_RE.search(row)
    title_m = re.search(r'<p[^>]*>.*?<a[^>]+>\s*([^<]+)\s*</a>', row, re.DOTALL)

    return {
        "date": meeting_date,
        "agenda_url": agenda_m.group(1) if agenda_m else None,
        "minutes_url": minutes_m.group(1) if minutes_m else None,
        "video_url": video_m.group(1) if video_m else None,
        "title": title_m.group(1).strip() if title_m else "",
    }


def parse_rows(html, cat_id):
    """
    Return list of parsed meeting dicts for the category panel cat_id in html.
    """
    panel_start = html.find(f'id="category-panel-{cat_id}"')
    if panel_start < 0:
        return []
    next_panel = html.find('id="category-panel-', panel_start + 1)
    chunk = html[panel_start: next_panel if next_panel > 0 else len(html)]
    rows = re.findall(r'<tr[^>]+class="catAgendaRow"[^>]*>(.*?)</tr>', chunk, re.DOTALL)
    return [r for row in rows for r in [_parse_row(row)] if r]


def parse_rows_from_fragment(html):
    """Parse catAgendaRow entries from an UpdateCategoryList HTML fragment."""
    rows = re.findall(r'<tr[^>]+class="catAgendaRow"[^>]*>(.*?)</tr>', html, re.DOTALL)
    return [r for row in rows for r in [_parse_row(row)] if r]


# --- Utilities ---

def slugify(text):
    text = text.lower().strip()
    text = re.sub(r"[/\\]", "-", text)
    text = re.sub(r"\s+-\s+", "-", text)
    text = re.sub(r"[^\w\s-]", "", text)
    text = re.sub(r"[\s_]+", "-", text)
    text = re.sub(r"-{2,}", "-", text)
    return text.strip("-")[:60]


def make_dest_path(board_name, doc_type, meeting_date, output_dir):
    date_prefix = meeting_date.strftime("%Y-%m-%d")
    month_dir = meeting_date.strftime("%Y-%m")
    board_slug = slugify(board_name)
    month_path = os.path.join(output_dir, month_dir)
    os.makedirs(month_path, exist_ok=True)
    return os.path.join(month_path, f"{date_prefix}-{board_slug}-{doc_type}.pdf")


def make_video_dest_path(board_name, meeting_date, output_dir):
    """Return a yt-dlp output template (ends in .%(ext)s) for a video download."""
    date_prefix = meeting_date.strftime("%Y-%m-%d")
    month_dir = meeting_date.strftime("%Y-%m")
    board_slug = slugify(board_name)
    month_path = os.path.join(output_dir, month_dir)
    os.makedirs(month_path, exist_ok=True)
    return os.path.join(month_path, f"{date_prefix}-{board_slug}-video.%(ext)s")


# --- Main ---

def main():
    parser = argparse.ArgumentParser(
        description=(
            "Download Madison CT municipal agendas, minutes, and video recordings "
            "for meetings in the past N days."
        )
    )
    parser.add_argument("--days", type=int, default=DAYS_BACK, metavar="N",
                        help=f"Look back N days (default: {DAYS_BACK})")
    parser.add_argument("--output-dir", default=OUTPUT_DIR, metavar="DIR",
                        help=f"Destination directory (default: {OUTPUT_DIR})")
    parser.add_argument("--dry-run", action="store_true",
                        help="List matching items without downloading")
    parser.add_argument("--board", metavar="NAME",
                        help="Only process boards whose name contains NAME (case-insensitive)")
    parser.add_argument("--include-video", action="store_true",
                        help="Also download YouTube video recordings via yt-dlp (can be very large)")
    parser.add_argument("--docs-only", action="store_true",
                        help="Download only PDFs; skip video even if --include-video is set")
    args = parser.parse_args()

    now = datetime.datetime.now()
    if (now.weekday() == 5 and now.hour >= 18) or (now.weekday() == 6 and now.hour < 12):  # Saturday night, Sunday morning
        print("Skipping — no downloads on Saturday nights or Sunday mornings.")
        sys.exit(0)

    today = datetime.date.today()
    cutoff = today - datetime.timedelta(days=args.days)

    years_needed = {today.year}
    if cutoff.year != today.year:
        years_needed.add(cutoff.year)

    include_video = args.include_video and not args.docs_only

    print(f"Cutoff date : {cutoff}  ({args.days} days back)")
    print(f"Fetching    : {AGENDA_CENTER_URL}")
    if not args.dry_run:
        print(f"Output dir  : {args.output_dir}")
    if include_video:
        print("Video       : enabled (yt-dlp)")
    print()

    # --- Step 1: fetch the main page ---
    print("Fetching AgendaCenter index...")
    main_html = fetch_html(AGENDA_CENTER_URL)
    if not main_html:
        print("ERROR: Could not fetch AgendaCenter page.", file=sys.stderr)
        sys.exit(1)

    boards = parse_boards(main_html)
    if not boards:
        print("ERROR: No boards found — page structure may have changed.", file=sys.stderr)
        sys.exit(1)

    print(f"Found {len(boards)} board(s).\n")

    if args.board:
        filter_name = args.board.lower()
        boards = [(cid, name) for cid, name in boards if filter_name in name.lower()]
        print(f"Filtered to {len(boards)} board(s) matching '{args.board}'.\n")

    # --- Step 2: collect matching meetings ---
    matches = []

    for cat_id, board_name in boards:
        rows = parse_rows(main_html, cat_id)

        if len(years_needed) > 1:
            prior_year = min(years_needed)
            post_data = urllib.parse.urlencode(
                {"year": prior_year, "catID": cat_id}
            ).encode()
            prior_html = fetch_html(UPDATE_URL, post_data=post_data)
            if prior_html:
                rows += parse_rows_from_fragment(prior_html)
            time.sleep(0.2)

        for row in rows:
            if row["date"] < cutoff or not row["agenda_url"]:
                continue
            matches.append({
                "board": board_name,
                "date": row["date"],
                "title": row["title"],
                "agenda_url": row["agenda_url"],
                "minutes_url": row["minutes_url"],
                "video_url": row["video_url"],
            })

    matches.sort(key=lambda x: (x["date"], x["board"]), reverse=True)

    video_count = sum(1 for m in matches if m["video_url"])
    total_docs = sum(1 + bool(m["minutes_url"]) for m in matches)
    print(
        f"Found {len(matches)} meeting(s) with up to {total_docs} document(s) "
        f"and {video_count} video recording(s) in the past {args.days} days."
    )
    print()

    if not matches:
        sys.exit(0)

    if args.dry_run:
        print(f"{'Board':<50} {'Date':<12} Docs")
        print("-" * 76)
        for m in matches:
            docs = ["agenda"]
            if m["minutes_url"]:
                docs.append("minutes")
            if m["video_url"]:
                docs.append("video")
            print(f"{m['board'][:49]:<50} {m['date']!s:<12} {', '.join(docs)}")
        print(f"\n{len(matches)} meeting(s). Re-run without --dry-run to download.")
        return

    # --- Step 3: download ---
    os.makedirs(args.output_dir, exist_ok=True)
    log_path = os.path.join(args.output_dir, "download-log.txt")
    log_lines = []
    downloaded = skipped = failed = 0

    for m in matches:
        board = m["board"]
        date = m["date"]
        print(f"[{date}] {board}")

        # PDFs: agenda and minutes
        for doc_type, url in (
            ("agenda", m["agenda_url"]),
            ("minutes", m["minutes_url"]),
        ):
            if not url:
                continue

            dest = make_dest_path(board, doc_type, date, args.output_dir)
            label = os.path.basename(dest)

            if os.path.exists(dest):
                print(f"  skip (exists)  {label}")
                skipped += 1
                continue

            print(f"  downloading    {label}")
            if download_file(url, dest):
                downloaded += 1
                log_lines.append(f"{datetime.datetime.now().isoformat()}  OK       {dest}")
            else:
                failed += 1
                log_lines.append(f"{datetime.datetime.now().isoformat()}  FAILED   {BASE_URL + url}")
                if os.path.exists(dest):
                    os.remove(dest)

            time.sleep(DELAY_SECONDS)

        # Video
        if include_video and m["video_url"]:
            dest = make_video_dest_path(board, date, args.output_dir)
            label = os.path.basename(dest)

            if video_already_exists(dest):
                print(f"  skip (exists)  {label}")
                skipped += 1
                continue

            print(f"  downloading    {label}")
            print(f"  source         {m['video_url']}")
            if download_video(m["video_url"], dest):
                downloaded += 1
                log_lines.append(f"{datetime.datetime.now().isoformat()}  OK       {dest}")
            else:
                failed += 1
                log_lines.append(f"{datetime.datetime.now().isoformat()}  FAILED   {m['video_url']}")
        elif m["video_url"]:
            print(f"  VIDEO (not downloaded): {m['video_url']}")

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
#    python3 scripts/download-madison-agendas.py --dry-run
#
# 2. Download docs + video recordings for the past 30 days:
#    python3 scripts/download-madison-agendas.py --include-video
#
# 3. Narrow to one board:
#    python3 scripts/download-madison-agendas.py --board "Board of Selectmen"
#
# 4. Change the lookback window:
#    python3 scripts/download-madison-agendas.py --days 7
#
# 5. Documents only (no video even if flag is passed):
#    python3 scripts/download-madison-agendas.py --docs-only
#
# 6. Save files somewhere else:
#    python3 scripts/download-madison-agendas.py --output-dir ~/Downloads/madison
#
# 7. Run on a schedule (cron — 8 AM daily):
#    0 8 * * * cd /path/to/repo && python3 scripts/download-madison-agendas.py
#
# 8. Run daily with video included:
#    0 8 * * * cd /path/to/repo && python3 scripts/download-madison-agendas.py --include-video
#
# 9. Process downloaded PDFs with Claude afterward:
#    python3 scripts/download-madison-agendas.py && bash scripts/batch-process.sh beat-archive/madison-agendas/
#
# NOTE: CivicPlus AgendaCenter exposes meeting dates, not upload/posted dates.
# The script filters by meeting date. A future meeting whose agenda was posted
# early will appear once its meeting date enters the lookback window.
#
# NOTE: Use --include-video to also download YouTube video recordings via yt-dlp.
# Videos are typically 500 MB – 2 GB each. Files that already exist on disk
# are skipped, so re-runs are safe.
#
# NOTE: Madison maintains multiple YouTube channels for different boards:
#   Town of Madison, CT
#   Board of Selectmen
#   Board of Finance
#   Board of Education
# The script discovers video links directly from each meeting row in AgendaCenter,
# so only meetings within your configured date window are downloaded.
