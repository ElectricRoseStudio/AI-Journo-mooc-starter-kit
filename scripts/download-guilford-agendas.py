#!/usr/bin/env python3
# download-guilford-agendas.py
# Download municipal meeting agendas, minutes, and video recordings from the
# Guilford CT Agenda Center for meetings within the past N days (and up to 7
# days ahead, to catch agendas posted early for upcoming meetings).
#
# USAGE:
#   python3 scripts/download-guilford-agendas.py [options]
#
# REQUIREMENTS:
#   - Python 3.6+  (no third-party packages needed for docs)
#   - yt-dlp       (for video: pip install yt-dlp or brew install yt-dlp)
#   - Internet connection
#
# WHAT IT DOES:
#   Documents (default or --docs-only):
#     1. Fetches the Guilford CT Agenda Center index page to get the list of all
#        board/committee pages (46 boards as of 2026)
#     2. Fetches each individual board page and scans for meeting dates (MM/DD/YY)
#        and document links (href contains "Document_Center")
#     3. Downloads agendas, minutes, and other docs whose meeting date falls within
#        the date window to beat-archive/guilford-agendas/YYYY-MM/
#     4. Appends a download log to beat-archive/guilford-agendas/download-log.txt
#
#   Video (--include-video or --video-only):
#     5. Extracts Video links (YouTube, SharePoint OneDrive, Dropbox) from meeting
#        rows, matched to their meeting date via document order
#     6. Downloads recordings with yt-dlp
#
# SITE STRUCTURE (Revize CMS / custom PHP):
#   Index:    https://www.guilfordct.gov/agenda_center/index.php
#   Board:    https://www.guilfordct.gov/agenda_center/<slug>.php
#   Document: https://www.guilfordct.gov/Document_Center/Agenda%20Center/<Board>/<Year>/...pdf
#   Video:    YouTube, SharePoint OneDrive, or Dropbox links embedded per meeting row
#
#   Per-board page layout (all content is inline static HTML, newest-first):
#     [Year heading]
#     <td>MM/DD/YY Meeting Type</td>
#     <td><a href="Document_Center/...pdf?t=...">Agenda</a>
#         <a href="https://...sharepoint.com/...">Video</a> ...</td>
#     ...
#
#   - Dates are in MM/DD/YY two-digit-year format (all assumed 2000s)
#   - Document hrefs are relative paths starting with "Document_Center/"
#   - Classified as agenda or minutes by link text or URL path component
#   - Active links always have an href; "visibility:hidden" placeholders do not
#   - No AJAX or JavaScript loading — all content is static inline HTML
#
# NOTE: With 46 board pages, the script makes 47 HTTP requests plus one per
# downloaded file. A 0.8s delay between page fetches keeps load reasonable.
# Total runtime for a 30-day window is typically under 3 minutes.
#
# NOTE: The server requires no authentication. A plain urllib request with a
# browser-like User-Agent is sufficient.

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
BASE_URL = "https://www.guilfordct.gov"
INDEX_URL = f"{BASE_URL}/agenda_center/index.php"
OUTPUT_DIR = "beat-archive/guilford-agendas"
DAYS_BACK = 4
DAYS_AHEAD = 7
PAGE_DELAY = 0.8    # seconds between board-page fetches
DOWNLOAD_DELAY = 0.8

UA = "Guilford-CT-Agendas-Downloader/1.0 (journalism research)"

# Matches board page links in the index: href="agenda_center/foo.php"
_BOARD_LINK_RE = re.compile(
    r'<a\s[^>]*href=["\']agenda_center/([a-z0-9_]+\.php)["\'][^>]*>(.*?)</a>',
    re.IGNORECASE | re.DOTALL,
)

# Matches Document_Center links on board pages
_DOC_LINK_RE = re.compile(
    r'<a\s[^>]*href=["\']([^"\']*Document_Center[^"\']*)["\'][^>]*>(.*?)</a>',
    re.IGNORECASE | re.DOTALL,
)

# Matches external video platform links where link text is "Video"
# Covers YouTube, SharePoint OneDrive (:v: share links), and Dropbox
_VIDEO_LINK_RE = re.compile(
    r'<a\s[^>]*href=["\']'
    r'(https?://(?!(?:www\.)?guilfordct\.gov)[^"\']+)'
    r'["\'][^>]*>\s*Video\s*</a>',
    re.IGNORECASE | re.DOTALL,
)

# Matches MM/DD/YY date strings (two-digit year, assumed 2000s)
_DATE_RE = re.compile(r'\b(\d{1,2})/(\d{1,2})/(\d{2})\b')


# --- HTTP helpers ---

def fetch_html(url):
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "text/html"})
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


def download_pdf(href, dest_path):
    """Download a Document_Center file to dest_path. Returns True on success."""
    # Strip the cache-busting query string (?t=...) and URL-encode the path.
    path = href.split("?")[0]
    encoded = urllib.parse.quote(path, safe="/")
    url = f"{BASE_URL}/{encoded}"
    req = urllib.request.Request(
        url,
        headers={"User-Agent": UA, "Accept": "application/pdf,application/msword,*/*"},
    )
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            with open(dest_path, "wb") as f:
                f.write(r.read())
        return True
    except Exception as e:
        print(f"  WARNING: {e}", file=sys.stderr)
        return False


def download_video(video_url, dest_path, dry_run=False):
    """Download a video via yt-dlp. Returns True on success."""
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
        print(f"  WARNING: yt-dlp failed ({e})", file=sys.stderr)
        return False


# --- Parsing ---

def parse_index(html_text):
    """
    Extract the board/committee list from agenda_center/index.php.
    Returns list of {name, url, slug}.
    """
    boards = []
    seen = set()
    for m in _BOARD_LINK_RE.finditer(html_text):
        slug = m.group(1).lower()
        if slug == "index.php" or slug in seen:
            continue
        seen.add(slug)
        name = re.sub(r"<[^>]+>", "", m.group(2)).strip()
        if name:
            boards.append({
                "name": name,
                "url": f"{BASE_URL}/agenda_center/{slug}",
                "slug": slug,
            })
    return boards


_DOC_EXTS = {".pdf", ".doc", ".docx", ".docm"}


def _classify_doc(link_text, href):
    """
    Return 'agenda', 'minutes', or None based on link text and href path.
    Accepts PDF, Word (.doc/.docx) files in Document_Center.
    """
    href_lower = href.lower()
    text_lower = link_text.lower().strip()

    # Must be a recognized document format
    path_part = href_lower.split("?")[0]
    _, ext = os.path.splitext(path_part)
    if ext not in _DOC_EXTS:
        return None

    if text_lower in ("agenda", "agenda packet", "amended agenda", "revised agenda"):
        return "agenda"
    if text_lower == "minutes":
        return "minutes"

    # Fall back to URL path inspection
    if "/agendas/" in href_lower or "/agenda/" in href_lower:
        return "agenda"
    if "/minutes/" in href_lower:
        return "minutes"

    return None


def parse_board_page(html_text, board_name):
    """
    Scan a board page for meeting date/document/video entries.

    Strategy: build a token stream of dates, document links, and video links
    sorted by their position in the HTML. Walk the stream in document order,
    tracking the most recently seen date. Each doc or video link is associated
    with that date. Works regardless of exact tag structure.

    Returns list of {board, meeting_date, doc_type, href, ext}.
    doc_type is 'agenda', 'minutes', or 'video'.
    ext is the file extension (e.g. '.pdf', '.docx', '.mp4').
    """
    items = []
    current_date = None
    seen_hrefs = set()

    date_tokens = [
        (m.start(), "date", m.group(1), m.group(2), m.group(3))
        for m in _DATE_RE.finditer(html_text)
    ]
    link_tokens = [
        (m.start(), "link", m.group(1), re.sub(r"<[^>]+>", "", m.group(2)).strip())
        for m in _DOC_LINK_RE.finditer(html_text)
    ]
    video_tokens = [
        (m.start(), "video", m.group(1))
        for m in _VIDEO_LINK_RE.finditer(html_text)
    ]

    tokens = sorted(date_tokens + link_tokens + video_tokens, key=lambda t: t[0])

    for token in tokens:
        if token[1] == "date":
            _, _, mm_s, dd_s, yy_s = token
            mm, dd, yy = int(mm_s), int(dd_s), int(yy_s)
            yyyy = 2000 + yy
            try:
                current_date = datetime.date(yyyy, mm, dd)
            except ValueError:
                current_date = None

        elif token[1] == "link" and current_date:
            _, _, href, link_text = token
            href_path = href.split("?")[0]

            if href_path in seen_hrefs:
                continue
            seen_hrefs.add(href_path)

            doc_type = _classify_doc(link_text, href_path)
            if doc_type:
                _, ext = os.path.splitext(href_path.lower())
                items.append({
                    "board": board_name,
                    "meeting_date": current_date,
                    "doc_type": doc_type,
                    "href": href_path,
                    "ext": ext if ext else ".pdf",
                })

        elif token[1] == "video" and current_date:
            _, _, video_url = token
            if video_url in seen_hrefs:
                continue
            seen_hrefs.add(video_url)
            items.append({
                "board": board_name,
                "meeting_date": current_date,
                "doc_type": "video",
                "href": video_url,
                "ext": ".mp4",
            })

    return items


# --- File naming ---

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
    os.makedirs(month_dir, exist_ok=True)
    board_slug = slugify(board)
    suffix = f"-{counter}" if counter > 0 else ""
    return os.path.join(month_dir, f"{date_prefix}-{board_slug}-{doc_type}{suffix}{ext}")


# --- Main ---

def main():
    parser = argparse.ArgumentParser(
        description=(
            "Download Guilford CT municipal agendas and minutes "
            "for meetings within the past N days."
        )
    )
    parser.add_argument(
        "--days", type=int, default=DAYS_BACK, metavar="N",
        help=f"Look back N days by meeting date (default: {DAYS_BACK})",
    )
    parser.add_argument(
        "--ahead", type=int, default=DAYS_AHEAD, metavar="N",
        help=f"Also include meetings up to N days ahead (default: {DAYS_AHEAD})",
    )
    parser.add_argument(
        "--output-dir", default=OUTPUT_DIR, metavar="DIR",
        help=f"Destination directory (default: {OUTPUT_DIR})",
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="List matching documents without downloading",
    )
    parser.add_argument(
        "--board", metavar="NAME",
        help="Only fetch boards whose name contains NAME (case-insensitive)",
    )
    parser.add_argument(
        "--no-minutes", action="store_true",
        help="Skip minutes, download agendas only",
    )
    parser.add_argument(
        "--no-agendas", action="store_true",
        help="Skip agendas, download minutes only",
    )
    parser.add_argument(
        "--include-video", action="store_true",
        help="Also download video recordings via yt-dlp",
    )
    parser.add_argument(
        "--video-only", action="store_true",
        help="Download only video recordings (skip documents)",
    )
    args = parser.parse_args()

    now = datetime.datetime.now()
    if (now.weekday() == 5 and now.hour >= 18) or (now.weekday() == 6 and now.hour < 12):  # Saturday night, Sunday morning
        print("Skipping — no downloads on Saturday nights or Sunday mornings.")
        sys.exit(0)

    do_docs  = not args.video_only
    do_video = args.include_video or args.video_only

    today = datetime.date.today()
    cutoff = today - datetime.timedelta(days=args.days)
    future_limit = today + datetime.timedelta(days=args.ahead)

    print(f"Date window : {cutoff} to {future_limit}")
    print(f"Index page  : {INDEX_URL}")
    if not args.dry_run:
        print(f"Output dir  : {args.output_dir}")
    print()

    # --- Step 1: fetch board list from index ---
    print("Fetching Agenda Center index...")
    index_html = fetch_html(INDEX_URL)
    if not index_html:
        print("ERROR: Could not fetch the index page.", file=sys.stderr)
        sys.exit(1)

    boards = parse_index(index_html)
    if not boards:
        print("WARNING: No board pages found in index — page structure may have changed.",
              file=sys.stderr)
        sys.exit(1)

    print(f"  Found {len(boards)} board/committee page(s).")

    if args.board:
        filter_str = args.board.lower()
        boards = [b for b in boards if filter_str in b["name"].lower()]
        print(f"  Filtered to {len(boards)} board(s) matching '{args.board}'.")

    print()

    # --- Step 2: fetch each board page and collect docs in date window ---
    all_docs = []

    print(f"Fetching board pages ({len(boards)} total)...")
    for i, board in enumerate(boards, 1):
        print(f"  [{i:>2}/{len(boards)}] {board['name']}...", end=" ", flush=True)
        page_html = fetch_html(board["url"])
        if not page_html:
            print("FAILED")
            time.sleep(PAGE_DELAY)
            continue

        items = parse_board_page(page_html, board["name"])
        in_window = [
            item for item in items
            if cutoff <= item["meeting_date"] <= future_limit
        ]
        n_docs = sum(1 for i in in_window if i["doc_type"] != "video")
        n_vids = sum(1 for i in in_window if i["doc_type"] == "video")
        parts = []
        if n_docs:
            parts.append(f"{n_docs} doc(s)")
        if n_vids:
            parts.append(f"{n_vids} video(s)")
        print(", ".join(parts) if parts else "0 items")

        for item in in_window:
            if item["doc_type"] == "video":
                if do_video:
                    all_docs.append(item)
            else:
                if not do_docs:
                    continue
                if args.no_agendas and item["doc_type"] == "agenda":
                    continue
                if args.no_minutes and item["doc_type"] == "minutes":
                    continue
                all_docs.append(item)

        time.sleep(PAGE_DELAY)

    all_docs.sort(key=lambda x: (x["meeting_date"], x["board"]), reverse=True)

    print()
    print(
        f"Found {len(all_docs)} document(s) across "
        f"{len({d['board'] for d in all_docs})} board(s)."
    )
    print()

    if not all_docs:
        print("No documents found within the date window.")
        return

    if args.dry_run:
        print(f"{'Board':<45} {'Date':<12} Type")
        print("-" * 70)
        for d in all_docs:
            extra = f"  {d['href'][:40]}..." if d["doc_type"] == "video" else ""
            print(f"{d['board'][:44]:<45} {d['meeting_date']!s:<12} {d['doc_type']}{extra}")
        noun = "item(s)" if (do_docs and do_video) else ("recording(s)" if do_video else "document(s)")
        print(f"\n{len(all_docs)} {noun}. Re-run without --dry-run to download.")
        return

    # --- Step 3: download ---
    os.makedirs(args.output_dir, exist_ok=True)
    log_path = os.path.join(args.output_dir, "download-log.txt")
    log_lines = []
    downloaded = skipped = failed = 0

    # Track (board_slug, date, doc_type) to deduplicate filenames
    filename_counters: dict = {}

    for d in all_docs:
        ext = d.get("ext", ".pdf")
        key = (slugify(d["board"]), d["meeting_date"], d["doc_type"])
        filename_counters[key] = filename_counters.get(key, 0) + 1
        counter = filename_counters[key] - 1  # 0 = no suffix, 1+ = "-N" suffix

        dest = make_dest_path(
            d["board"], d["doc_type"], d["meeting_date"], args.output_dir, counter, ext
        )
        label = os.path.basename(dest)

        if os.path.exists(dest):
            print(f"  skip (exists)  {label}")
            skipped += 1
            continue

        print(f"  [{d['meeting_date']}] {d['board']} — {d['doc_type']}")
        print(f"  downloading    {label}")

        if d["doc_type"] == "video":
            ok = download_video(d["href"], dest)
        else:
            ok = download_pdf(d["href"], dest)

        if ok:
            downloaded += 1
            log_lines.append(f"{datetime.datetime.now().isoformat()}  OK       {dest}")
        else:
            failed += 1
            src = d["href"] if d["doc_type"] == "video" else f"{BASE_URL}/{d['href']}"
            log_lines.append(f"{datetime.datetime.now().isoformat()}  FAILED   {src}")
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
#    python3 scripts/download-guilford-agendas.py --dry-run
#
# 2. Narrow to one board (fetches only that board's page):
#    python3 scripts/download-guilford-agendas.py --board "Board of Selectmen"
#
# 3. Agendas only (skip minutes):
#    python3 scripts/download-guilford-agendas.py --no-minutes
#
# 4. Change the lookback window:
#    python3 scripts/download-guilford-agendas.py --days 7
#
# 5. Save files somewhere else:
#    python3 scripts/download-guilford-agendas.py --output-dir ~/Downloads/guilford
#
# 6. Download documents AND video recordings:
#    python3 scripts/download-guilford-agendas.py --include-video
#
# 7. Download only video recordings (skip PDFs):
#    python3 scripts/download-guilford-agendas.py --video-only
#
# 8. Preview video recordings without downloading:
#    python3 scripts/download-guilford-agendas.py --video-only --dry-run
#
# 9. Run on a schedule (cron — 8 AM daily):
#    0 8 * * * cd /path/to/repo && python3 scripts/download-guilford-agendas.py
#
# 10. Process downloaded files with Claude afterward:
#    python3 scripts/download-guilford-agendas.py && bash scripts/batch-process.sh beat-archive/guilford-agendas/
#
# BOARDS (51 as of 2026):
#   Affordable Housing Commission, Agricultural Commission, Board of Assessment
#   Appeals, Board of Ethics, Board of Finance, Board of Fire Commissioners,
#   Board of Police Commissioners, Board of Selectmen, Building Code Board of
#   Appeals, Conservation Commission, Design Review Committee, Economic
#   Development Commission, and many others — see agenda_center/index.php
#   for the complete current list.
#
# NOTE: The --board filter reduces HTTP requests by skipping non-matching board
# pages entirely, which is useful for targeted monitoring of a single board.
#
# NOTE: The --ahead flag (default: 7 days) captures agendas for upcoming meetings
# already published. Run daily to stay current.
#
# NOTE: All content on each board page is static inline HTML going back to the
# earliest available records. Unlike CivicEngage sites, there is no dynamic
# year-loading — the full archive is always in the initial page load.
