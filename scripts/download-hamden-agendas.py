#!/usr/bin/env python3
# download-hamden-agendas.py
# Download municipal meeting agendas, minutes, and video recordings from
# Hamden CT AgendaCenter for meetings whose date falls within the past N days.
#
# USAGE:
#   python3 scripts/download-hamden-agendas.py [options]
#
# REQUIREMENTS:
#   - Python 3.6+  (no third-party packages needed)
#   - Internet connection
#
# WHAT IT DOES:
#   1. Fetches the Hamden CT AgendaCenter listing page
#   2. Finds all agendas, minutes, and (optionally) Zoom video recordings whose
#      meeting date falls within the lookback window
#   3. Downloads PDFs to beat-archive/hamden-agendas/YYYY-MM/
#   4. Downloads Zoom cloud recordings via Zoom's recording API (--include-video)
#   5. Appends a download log to beat-archive/hamden-agendas/download-log.txt
#
# SITE STRUCTURE:
#   Hamden CT uses CivicPlus AgendaCenter (https://www.hamden.com/AgendaCenter).
#   Board sections are collapsible panels; the current year is pre-loaded in
#   the page HTML. Previous years load via a POST to /AgendaCenter/UpdateCategoryList.
#
#   Document URLs:
#     /AgendaCenter/ViewFile/Agenda/_MMDDYYYY-NNNN   → agenda PDF
#     /AgendaCenter/ViewFile/Minutes/_MMDDYYYY-NNNN  → minutes PDF
#
#   Video links appear as <a href="..."> wrapping an image with alt="Videos".
#   All recordings are Zoom cloud recordings (rec/share/ URLs). Live meeting
#   join links (zoom.us/j/...) appear in the same rows but are skipped.
#
# ZOOM VIDEO DOWNLOAD:
#   Zoom cloud recordings are fetched via a three-step API chain without
#   requiring a passcode:
#     1. Visit rec/share/ URL → establishes session cookies
#     2. GET /nws/recording/1.0/play/share-info/{share_token} → play token
#     3. GET /nws/recording/1.0/play/info/{play_token} → signed MP4 CDN URL
#   The MP4 URL is time-limited (~24 hours) and requires the session cookies.

import argparse
import datetime
import gzip
import http.cookiejar
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

# --- Configuration ---
BASE_URL = "https://www.hamden.com"
AGENDA_CENTER_URL = f"{BASE_URL}/AgendaCenter"
UPDATE_URL = f"{BASE_URL}/AgendaCenter/UpdateCategoryList"
OUTPUT_DIR = "beat-archive/hamden-agendas"
DAYS_BACK = 4
DELAY_SECONDS = 1

UA = "Hamden-Agendas-Downloader/1.0 (journalism research)"

ZOOM_REC_RE = re.compile(
    r'href="(https?://(?:[\w-]+\.)?zoom\.us/rec/(?:share|play)/[^"]+)"',
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
            raw = r.read()
        if raw[:2] == b'\x1f\x8b':
            raw = gzip.decompress(raw)
        return raw.decode("utf-8", errors="replace")
    except urllib.error.URLError as e:
        print(f"  ERROR fetching {url}: {e}", file=sys.stderr)
        return None


def download_file(path, dest_path):
    """Download BASE_URL + path (PDF) to dest_path. Returns True on success."""
    url = BASE_URL + path if path.startswith("/") else path
    url = url.split("?")[0]
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


# --- Zoom video helpers ---

def _zoom_opener():
    jar = http.cookiejar.CookieJar()
    return urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar))


def _zoom_request(opener, url, accept="application/json, text/plain, */*", referer=None):
    """Fetch url via opener; return decoded text."""
    headers = {
        "User-Agent": UA,
        "Accept": accept,
        "Accept-Encoding": "gzip, deflate",
        "Accept-Language": "en-US,en;q=0.9",
    }
    if referer:
        headers["Referer"] = referer
    req = urllib.request.Request(url, headers=headers)
    with opener.open(req, timeout=30) as r:
        raw = r.read()
    if raw[:2] == b'\x1f\x8b':
        raw = gzip.decompress(raw)
    return raw.decode("utf-8", errors="replace")


def get_zoom_mp4_url(share_url):
    """
    Walk the Zoom recording API chain and return (mp4_url, opener).
    The opener holds session cookies needed to download the MP4.
    Returns (None, None) on failure.

    Three-step flow:
      1. Visit share page → session cookies
      2. share-info API → play token
      3. play/info API → signed MP4 URL
    """
    opener = _zoom_opener()
    parsed = urllib.parse.urlparse(share_url)
    host = f"{parsed.scheme}://{parsed.netloc}"
    share_token = parsed.path.split("/rec/share/")[-1]

    # Step 1: visit share page → cookies
    try:
        _zoom_request(opener, share_url,
                      accept="text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8")
    except Exception as e:
        print(f"  WARNING: Could not load Zoom share page: {e}", file=sys.stderr)
        return None, None

    # Step 2: share-info → redirectUrl containing play token
    enc = urllib.parse.quote(share_token, safe="")
    share_info_url = f"{host}/nws/recording/1.0/play/share-info/{enc}?continueMode=true"
    try:
        raw = _zoom_request(opener, share_info_url, referer=share_url)
        data = json.loads(raw)
        if not data.get("status", True):
            err = data.get("errorMessage", "unknown error")
            print(f"  WARNING: Zoom recording unavailable: {err}", file=sys.stderr)
            return None, None
        redirect = data.get("result", {}).get("redirectUrl", "")
        if "/rec/play/" not in redirect:
            print(f"  WARNING: Zoom share-info returned unexpected response", file=sys.stderr)
            return None, None
        play_token = redirect.split("/rec/play/")[-1]
    except Exception as e:
        print(f"  WARNING: Zoom share-info API failed: {e}", file=sys.stderr)
        return None, None

    # Step 3: play/info → viewMp4Url
    enc2 = urllib.parse.quote(play_token, safe="")
    play_info_url = (
        f"{host}/nws/recording/1.0/play/info/{enc2}"
        f"?continueMode=true&from=share_recording_detail"
    )
    play_referer = f"{host}/rec/play/{play_token}"
    try:
        raw2 = _zoom_request(opener, play_info_url, referer=play_referer)
        data2 = json.loads(raw2)
        mp4_url = data2.get("result", {}).get("viewMp4Url")
        if not mp4_url:
            print(f"  WARNING: No viewMp4Url in Zoom play/info response", file=sys.stderr)
            return None, None
        return mp4_url, opener
    except Exception as e:
        print(f"  WARNING: Zoom play/info API failed: {e}", file=sys.stderr)
        return None, None


def download_zoom_video(share_url, dest_path):
    """
    Download a Zoom cloud recording to dest_path (.mp4).
    Returns True on success.
    """
    mp4_url, opener = get_zoom_mp4_url(share_url)
    if not mp4_url:
        return False

    req = urllib.request.Request(mp4_url, headers={
        "User-Agent": UA,
        "Referer": "https://zoom.us/",
    })
    try:
        with opener.open(req, timeout=7200) as r:
            total = int(r.headers.get("Content-Length", 0))
            chunk_size = 1024 * 1024  # 1 MB
            downloaded = 0
            with open(dest_path, "wb") as f:
                while True:
                    chunk = r.read(chunk_size)
                    if not chunk:
                        break
                    f.write(chunk)
                    downloaded += len(chunk)
                    if total:
                        pct = 100 * downloaded // total
                        mb = downloaded // (1024 * 1024)
                        total_mb = total // (1024 * 1024)
                        print(f"\r    {pct}%  {mb}/{total_mb} MB", end="", flush=True)
            print()
        return True
    except Exception as e:
        print(f"\n  WARNING: Error downloading Zoom video: {e}", file=sys.stderr)
        if os.path.exists(dest_path):
            os.remove(dest_path)
        return False


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
    video_m = ZOOM_REC_RE.search(row)
    title_m = re.search(r'<p[^>]*>.*?<a[^>]+>\s*([^<]+)\s*</a>', row, re.DOTALL)

    return {
        "date": meeting_date,
        "agenda_url": agenda_m.group(1) if agenda_m else None,
        "minutes_url": minutes_m.group(1) if minutes_m else None,
        "video_url": video_m.group(1) if video_m else None,
        "title": title_m.group(1).strip() if title_m else "",
    }


def parse_rows(html, cat_id):
    """Return list of parsed meeting dicts for the category panel cat_id in html."""
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
    date_prefix = meeting_date.strftime("%Y-%m-%d")
    month_dir = meeting_date.strftime("%Y-%m")
    board_slug = slugify(board_name)
    month_path = os.path.join(output_dir, month_dir)
    os.makedirs(month_path, exist_ok=True)
    return os.path.join(month_path, f"{date_prefix}-{board_slug}-video.mp4")


# --- Main ---

def main():
    parser = argparse.ArgumentParser(
        description=(
            "Download Hamden CT municipal agendas, minutes, and Zoom video recordings "
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
                        help="Also download Zoom cloud recordings (can be 300 MB – 1 GB+ each)")
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
        print("Video       : enabled (Zoom API direct download)")
    print()

    # --- Step 1: fetch main page ---
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
        f"and {video_count} Zoom recording(s) in the past {args.days} days."
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
                log_lines.append(
                    f"{datetime.datetime.now().isoformat()}  OK       {dest}"
                )
            else:
                failed += 1
                log_lines.append(
                    f"{datetime.datetime.now().isoformat()}  FAILED   {BASE_URL + url}"
                )
                if os.path.exists(dest):
                    os.remove(dest)

            time.sleep(DELAY_SECONDS)

        if include_video and m["video_url"]:
            dest = make_video_dest_path(board, date, args.output_dir)
            label = os.path.basename(dest)

            if os.path.exists(dest):
                print(f"  skip (exists)  {label}")
                skipped += 1
                continue

            print(f"  downloading    {label}  (Zoom recording)")
            print(f"  source         {m['video_url'][:80]}")
            if download_zoom_video(m["video_url"], dest):
                downloaded += 1
                log_lines.append(
                    f"{datetime.datetime.now().isoformat()}  OK       {dest}"
                )
            else:
                failed += 1
                log_lines.append(
                    f"{datetime.datetime.now().isoformat()}  FAILED   {m['video_url']}"
                )
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
#    python3 scripts/download-hamden-agendas.py --dry-run
#
# 2. Download docs + Zoom recordings for the past 30 days:
#    python3 scripts/download-hamden-agendas.py --include-video
#
# 3. Narrow to one board:
#    python3 scripts/download-hamden-agendas.py --board "Legislative Council"
#
# 4. Change the lookback window:
#    python3 scripts/download-hamden-agendas.py --days 7
#
# 5. Documents only (no video even if flag is passed):
#    python3 scripts/download-hamden-agendas.py --docs-only
#
# 6. Save files somewhere else:
#    python3 scripts/download-hamden-agendas.py --output-dir ~/Downloads/hamden
#
# 7. Run on a schedule (cron — 8 AM daily):
#    0 8 * * * cd /path/to/repo && python3 scripts/download-hamden-agendas.py
#
# NOTE: CivicPlus AgendaCenter exposes meeting dates, not upload/posted dates.
# The script filters by meeting date.
#
# NOTE: Zoom cloud recordings are downloaded via Zoom's recording API chain
# (not yt-dlp). The signed MP4 URL is valid for ~24 hours from when the API
# is called; partial downloads are cleaned up automatically on failure.
# Zoom join links (zoom.us/j/...) are ignored — only rec/share/ URLs are downloaded.
#
# NOTE: Zoom recordings are typically 300 MB – 1 GB+ each. Files that already
# exist on disk are skipped, so re-runs are safe.
