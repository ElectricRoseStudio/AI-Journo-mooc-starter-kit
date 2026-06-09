#!/usr/bin/env python3
# download-groton-agendas.py
# Download municipal meeting agendas, minutes, and video recordings from
# the Groton, CT AgendaSuite portal for meetings within the past N days
# (and up to 7 days ahead, to capture freshly posted agendas).
#
# USAGE:
#   python3 scripts/download-groton-agendas.py [options]
#
# REQUIREMENTS:
#   - Python 3.6+  (no third-party packages needed for docs)
#   - yt-dlp       (only needed for --video; install: pip install yt-dlp)
#   - Internet connection
#
# WHAT IT DOES:
#   1. GETs the AgendaSuite meeting-search page to obtain a session cookie
#      and CSRF token (ASP.NET antiforgery).
#   2. POSTs to /meetingsearch with the date window to get all matching
#      meeting IDs.
#   3. Fetches each meeting's detail page to collect:
#        - "Meeting files" links classified as Agenda or Minutes
#          (by aria-label attribute — no filename guessing needed)
#        - YouTube links in the meeting description
#   4. Downloads PDFs directly; downloads videos with yt-dlp.
#   5. Saves files to beat-archive/groton-agendas/YYYY-MM/
#   6. Appends a download log to beat-archive/groton-agendas/download-log.txt
#
# SITE STRUCTURE:
#   Portal:   https://www.agendasuite.org/iip/groton
#   Search:   https://www.agendasuite.org/iip/groton/meetingsearch  (POST)
#   Detail:   https://www.agendasuite.org/iip/groton/meeting/details/{ID}
#   File:     https://www.agendasuite.org/iip/groton/file/getfile/{FILE_ID}
#
# DETAIL PAGE — Meeting files section:
#   <a aria-label="Agenda"   href="/iip/groton/file/getfile/{ID}">
#   <a aria-label="Minutes"  href="/iip/groton/file/getfile/{ID}">
#   <a aria-label="Agenda package" ...>   ← combined PDF; skipped by default
#
# DETAIL PAGE — Video link (free-form description text):
#   <p>...Watch the meeting here: <br/>
#      <a href="https://www.youtube.com/watch?v=...">...</a></p>

import argparse
import datetime
import html as html_module
import http.cookiejar
import os
import re
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

# --- Configuration ---
BASE_URL = "https://www.agendasuite.org"
PORTAL = f"{BASE_URL}/iip/groton"
SEARCH_URL = f"{PORTAL}/meetingsearch"
OUTPUT_DIR = "beat-archive/groton-agendas"
DAYS_BACK = 4
DAYS_AHEAD = 7
DETAIL_DELAY = 0.5
DOWNLOAD_DELAY = 0.8

UA = "Mozilla/5.0 (compatible; Groton-CT-Agendas-Downloader/2.0; journalism research)"

# Matches YouTube watch or live URLs
_YT_URL_RE = re.compile(
    r"https?://(?:www\.)?youtube\.com/(?:watch\?v=|live/)[A-Za-z0-9_-]+[^\s\"'<>]*",
    re.IGNORECASE,
)

# Matches getfile links with their aria-label
_MEETING_FILE_RE = re.compile(
    r'<a\s[^>]*aria-label="([^"]+)"[^>]*href="(/iip/groton/file/getfile/(\d+))"',
    re.IGNORECASE,
)


# --- Network helpers ---

def _make_opener():
    jar = http.cookiejar.CookieJar()
    opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar))
    opener.addheaders = [
        ("User-Agent", UA),
        ("Accept", "text/html,application/xhtml+xml,*/*"),
    ]
    return opener


def _fetch(opener, url, post_data=None, extra_headers=None):
    """GET or POST a URL; returns decoded HTML string or None on error."""
    req = urllib.request.Request(url, data=post_data)
    if extra_headers:
        for k, v in extra_headers.items():
            req.add_header(k, v)
    try:
        with opener.open(req, timeout=30) as r:
            charset = r.headers.get_content_charset() or "utf-8"
            return r.read().decode(charset, errors="replace")
    except urllib.error.HTTPError as e:
        print(f"  HTTP {e.code} — {url}", file=sys.stderr)
        return None
    except urllib.error.URLError as e:
        print(f"  URL error — {url}: {e}", file=sys.stderr)
        return None


def _extract_csrf(html_text):
    m = re.search(
        r'<input[^>]+name="__RequestVerificationToken"[^>]+value="([^"]+)"',
        html_text,
    )
    return m.group(1) if m else None


# --- Meetingsearch ---

def fetch_meetings(opener, from_date, to_date):
    """
    Return list of dicts: {meeting_id, meeting_date, board}.
    Uses meetingsearch POST endpoint to find meetings in the given date window.
    """
    # GET the search page to establish session + get CSRF token
    page_html = _fetch(opener, SEARCH_URL)
    if not page_html:
        print("ERROR: Could not fetch the meetingsearch page.", file=sys.stderr)
        sys.exit(1)

    token = _extract_csrf(page_html)
    if not token:
        print("ERROR: CSRF token not found on meetingsearch page.", file=sys.stderr)
        sys.exit(1)

    # Extract organization options for building a name lookup (id → name)
    org_options = dict(re.findall(r'<option value="(\d+)">([^<]+)</option>', page_html))

    post_data = urllib.parse.urlencode({
        "SelectedOrganizationId": "",
        "FromDateStr": from_date.strftime("%m/%d/%Y"),
        "ToDateStr": to_date.strftime("%m/%d/%Y"),
        "ResultsLimit": "500",
        "__RequestVerificationToken": token,
    }).encode()

    result_html = _fetch(opener, SEARCH_URL, post_data=post_data)
    if not result_html:
        print("ERROR: meetingsearch POST returned nothing.", file=sys.stderr)
        sys.exit(1)

    # Parse the results table
    # Rows: <td>MM/DD/YYYY</td><td>Org name</td><td>Number</td>
    #       <td>...<a href="/iip/groton/meeting/details/{ID}">Details</a>...</td>
    _ROW_RE = re.compile(
        r"<tr>\s*"
        r"<td>(\d{2}/\d{2}/\d{4})</td>\s*"
        r"<td>([^<]+)</td>\s*"
        r"<td[^>]*>[^<]*</td>\s*"
        r"<td>.*?/meeting/details/(\d+)",
        re.DOTALL,
    )

    meetings = []
    for m in _ROW_RE.finditer(result_html):
        date_str, board_raw, meeting_id = m.group(1), m.group(2), m.group(3)
        try:
            mm, dd, yyyy = date_str.split("/")
            meeting_date = datetime.date(int(yyyy), int(mm), int(dd))
        except ValueError:
            continue
        board = " ".join(html_module.unescape(board_raw).strip().split())
        meetings.append({
            "meeting_id": meeting_id,
            "meeting_date": meeting_date,
            "board": board,
        })

    return meetings


# --- Detail page parsing ---

def parse_detail(html_text):
    """
    Parse a meeting detail page.
    Returns:
      files:  list of {file_id, doc_type}  where doc_type in {'agenda','minutes','agenda_package'}
      videos: list of YouTube URL strings
    """
    files = []
    seen_ids = set()
    for m in _MEETING_FILE_RE.finditer(html_text):
        label = m.group(1).strip().lower()
        file_id = m.group(3)
        if file_id in seen_ids:
            continue
        seen_ids.add(file_id)
        if label == "agenda":
            doc_type = "agenda"
        elif label == "minutes":
            doc_type = "minutes"
        elif label == "agenda package":
            doc_type = "agenda_package"
        else:
            continue
        files.append({"file_id": file_id, "doc_type": doc_type})

    videos = list(dict.fromkeys(_YT_URL_RE.findall(html_text)))

    return files, videos


# --- Download helpers ---

def download_file(file_id, dest_path, opener, dry_run=False):
    """Download a PDF from getfile/{ID}. Returns True on success."""
    if dry_run:
        return True
    url = f"{PORTAL}/file/getfile/{file_id}"
    req = urllib.request.Request(
        url,
        headers={"User-Agent": UA, "Accept": "application/pdf,*/*"},
    )
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            data = r.read()
        if not data:
            print(f"  WARNING: empty response for file_id={file_id}", file=sys.stderr)
            return False
        os.makedirs(os.path.dirname(dest_path), exist_ok=True)
        with open(dest_path, "wb") as f:
            f.write(data)
        return True
    except Exception as e:
        print(f"  WARNING: {e}", file=sys.stderr)
        return False


def download_video(video_url, dest_path, dry_run=False):
    """Download a YouTube video with yt-dlp. Returns True on success."""
    if dry_run:
        return True
    cmd = [
        "yt-dlp",
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


# --- Path construction ---

def slugify(text, max_len=50):
    text = text.lower().strip()
    text = re.sub(r"[/\\&]", "-", text)
    text = re.sub(r"\s+-\s+", "-", text)
    text = re.sub(r"[^\w\s-]", "", text)
    text = re.sub(r"[\s_]+", "-", text)
    text = re.sub(r"-{2,}", "-", text)
    return text.strip("-")[:max_len]


def make_dest_path(board, doc_type, meeting_date, output_dir, counter=0, ext=".pdf"):
    date_prefix = meeting_date.strftime("%Y-%m-%d")
    month_dir = os.path.join(output_dir, meeting_date.strftime("%Y-%m"))
    board_slug = slugify(board, max_len=40)
    suffix = f"-{counter}" if counter else ""
    fname = f"{date_prefix}-{board_slug}-{doc_type}{suffix}{ext}"
    return os.path.join(month_dir, fname)


# --- Main ---

def main():
    parser = argparse.ArgumentParser(
        description=(
            "Download Groton CT municipal agendas, minutes, and video recordings "
            "from AgendaSuite for meetings within the past N days."
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
        help="Only include boards whose name contains NAME (case-insensitive)",
    )
    parser.add_argument(
        "--no-agendas", action="store_true",
        help="Skip agenda files",
    )
    parser.add_argument(
        "--no-minutes", action="store_true",
        help="Skip minutes files",
    )
    parser.add_argument(
        "--agenda-package", action="store_true",
        help="Also download agenda-package PDFs (combined docs; skipped by default)",
    )
    parser.add_argument(
        "--no-video", action="store_true",
        help="Skip video recordings (requires yt-dlp when enabled)",
    )
    args = parser.parse_args()

    now = datetime.datetime.now()
    if (now.weekday() == 5 and now.hour >= 18) or (now.weekday() == 6 and now.hour < 12):  # Saturday night, Sunday morning
        print("Skipping — no downloads on Saturday nights or Sunday mornings.")
        sys.exit(0)

    do_video = not args.no_video
    do_docs = not (args.no_agendas and args.no_minutes)

    today = datetime.date.today()
    cutoff = today - datetime.timedelta(days=args.days)
    future_limit = today + datetime.timedelta(days=args.ahead)

    print(f"Date window : {cutoff} to {future_limit}")
    print(f"Portal      : {PORTAL}")
    if not args.dry_run:
        print(f"Output dir  : {args.output_dir}")
    print()

    opener = _make_opener()

    # --- Step 1: meetingsearch ---
    print("Fetching meeting list...")
    meetings = fetch_meetings(opener, cutoff, future_limit)
    print(f"  Found {len(meetings)} meeting(s) in window.")

    if args.board:
        filter_str = args.board.lower()
        meetings = [m for m in meetings if filter_str in m["board"].lower()]
        print(f"  Filtered to {len(meetings)} meeting(s) matching '{args.board}'.")

    if not meetings:
        print("No meetings found.")
        return

    meetings.sort(key=lambda x: (x["meeting_date"], x["board"]), reverse=True)

    # --- Step 2: fetch detail pages ---
    print("Fetching meeting detail pages...")
    all_items = []

    for mtg in meetings:
        detail_url = f"{PORTAL}/meeting/details/{mtg['meeting_id']}"
        html_text = _fetch(opener, detail_url)
        if not html_text:
            print(
                f"  WARNING: could not fetch details for "
                f"{mtg['board']} {mtg['meeting_date']}",
                file=sys.stderr,
            )
            time.sleep(DETAIL_DELAY)
            continue

        files, videos = parse_detail(html_text)

        for f in files:
            if f["doc_type"] == "agenda" and args.no_agendas:
                continue
            if f["doc_type"] == "minutes" and args.no_minutes:
                continue
            if f["doc_type"] == "agenda_package" and not args.agenda_package:
                continue
            all_items.append({
                "board": mtg["board"],
                "meeting_date": mtg["meeting_date"],
                "doc_type": f["doc_type"],
                "file_id": f["file_id"],
                "url": None,
                "ext": ".pdf",
            })

        if do_video:
            for vid_url in videos:
                all_items.append({
                    "board": mtg["board"],
                    "meeting_date": mtg["meeting_date"],
                    "doc_type": "video",
                    "file_id": None,
                    "url": vid_url,
                    "ext": ".mp4",
                })

        time.sleep(DETAIL_DELAY)

    doc_count = sum(1 for x in all_items if x["doc_type"] != "video")
    vid_count = sum(1 for x in all_items if x["doc_type"] == "video")
    board_count = len({x["board"] for x in all_items})
    print(
        f"Found {doc_count} document(s)"
        + (f", {vid_count} video(s)" if do_video else "")
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
            url_hint = f"  {item['url'][:50]}..." if item["doc_type"] == "video" and item["url"] else ""
            print(
                f"{item['board'][:43]:<44} "
                f"{item['meeting_date']!s:<12} "
                f"{item['doc_type']}"
                f"{url_hint}"
            )
        total = len(all_items)
        print(f"\n{total} item(s). Re-run without --dry-run to download.")
        return

    # --- Step 3: download ---
    os.makedirs(args.output_dir, exist_ok=True)
    log_path = os.path.join(args.output_dir, "download-log.txt")
    log_lines = []
    downloaded = skipped = failed = 0

    # Track per-(board, date, doc_type) counter to avoid filename collisions
    counters = {}

    for item in all_items:
        key = (item["board"], item["meeting_date"], item["doc_type"])
        counters[key] = counters.get(key, 0) + 1
        counter = counters[key] if counters[key] > 1 else 0

        dest = make_dest_path(
            item["board"], item["doc_type"], item["meeting_date"],
            args.output_dir, counter=counter, ext=item["ext"],
        )
        label = os.path.basename(dest)

        if os.path.exists(dest):
            print(f"  skip (exists)  {label}")
            skipped += 1
            continue

        print(f"  [{item['meeting_date']}] {item['board']} — {item['doc_type']}")
        print(f"  downloading    {label}")

        os.makedirs(os.path.dirname(dest), exist_ok=True)

        if item["doc_type"] == "video":
            ok = download_video(item["url"], dest)
        else:
            ok = download_file(item["file_id"], dest, opener)

        if ok:
            downloaded += 1
            log_lines.append(f"{datetime.datetime.now().isoformat()}  OK       {dest}")
        else:
            failed += 1
            log_lines.append(
                f"{datetime.datetime.now().isoformat()}  FAILED   {dest}"
            )
            if os.path.exists(dest):
                os.remove(dest)

        time.sleep(DOWNLOAD_DELAY)

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
#    python3 scripts/download-groton-agendas.py --dry-run
#
# 2. Narrow to one board:
#    python3 scripts/download-groton-agendas.py --board "Town Council"
#
# 3. Skip video downloads:
#    python3 scripts/download-groton-agendas.py --no-video
#
# 4. Agendas only:
#    python3 scripts/download-groton-agendas.py --no-minutes --no-video
#
# 5. Include agenda-package PDFs (combined documents):
#    python3 scripts/download-groton-agendas.py --agenda-package
#
# 6. Change the lookback window:
#    python3 scripts/download-groton-agendas.py --days 7
#
# 7. Save files somewhere else:
#    python3 scripts/download-groton-agendas.py --output-dir ~/Downloads/groton
#
# 8. Run on a schedule (cron — 8 AM daily):
#    0 8 * * * cd /path/to/repo && python3 scripts/download-groton-agendas.py
#
# NOTE: Document types are classified from the aria-label attribute on the
# file links ("Agenda", "Minutes", "Agenda package") — not from filenames.
#
# NOTE: Videos are sourced from YouTube links in the meeting description text.
# GMTV broadcasts Town Council and select other board meetings live.
# yt-dlp handles the YouTube download; install with: pip install yt-dlp
#
# NOTE: This script covers the Town of Groton's boards and commissions.
# The City of Groton and Groton Utilities are separate entities.
