#!/usr/bin/env python3
# download-east-lyme-agendas.py
# Download municipal meeting agendas, minutes, and video recordings from the
# East Lyme CT town website (eltownhall.com) for documents posted in the last
# N days.
#
# USAGE:
#   python3 scripts/download-east-lyme-agendas.py [options]
#
# REQUIREMENTS:
#   - Python 3.6+  (no third-party packages needed for docs)
#   - yt-dlp       (for video: pip install yt-dlp or brew install yt-dlp)
#   - Internet connection
#
# WHAT IT DOES:
#   Documents (default or --docs-only):
#     1. Fetches https://eltownhall.com/government/boards-commissions/ to get
#        the list of board/commission slugs (35 boards as of 2026)
#     2. For each board, fetches its [slug]-YYYY-agendas/ and [slug]-YYYY-
#        minutes/ sub-pages (404s are silently skipped)
#     3. Collects all PDF links (wp-content/uploads/...) and pre-filters to
#        those whose upload month falls within the date window
#     4. Issues a HEAD request for each candidate PDF to read its
#        Last-Modified header — the reliable proxy for "posted date"
#     5. Downloads PDFs whose Last-Modified falls within the date window
#        to beat-archive/east-lyme-agendas/YYYY-MM/
#     6. Appends a download log to beat-archive/east-lyme-agendas/download-log.txt
#
#   Video (--include-video or --video-only):
#     7. Fetches the static YouTube index at wp-content/uploads/YouTubeIndexes/
#        YouTubeLinks{YEAR}.html, which lists each meeting by date + board name
#        alongside a playVideo('VIDEO_ID') call
#     8. Filters to entries whose meeting date falls within the date window
#        (videos are typically posted 0–2 days after the meeting)
#     9. Downloads recordings with yt-dlp
#
# SITE STRUCTURE (WordPress, custom theme by Brown Bear Creative):
#   Base:        https://eltownhall.com
#   Boards:      /government/boards-commissions/
#   Board:       /government/boards-commissions/{slug}/
#   Agendas:     /government/boards-commissions/{slug}/{slug}-{YEAR}-agendas/
#   Minutes:     /government/boards-commissions/{slug}/{slug}-{YEAR}-minutes/
#   PDF:         /wp-content/uploads/{YYYY}/{MM}/{filename}.pdf
#   Video index: /wp-content/uploads/YouTubeIndexes/YouTubeLinks{YEAR}.html
#   YouTube:     https://www.youtube.com/watch?v={VIDEO_ID}
#
#   PDF "posted" date: read from Last-Modified HTTP response header.
#   Video index layout (one entry per <p>):
#     <p><b>&diams;MM/DD/YYYY</b> <b>Board Name</b>
#        <input ... onclick="javascript:playVideo('VIDEO_ID');" value="DURATION">
#        <br>Meeting Type<br></p>
#
# NOTE: The WP REST API (/wp-json/wp/v2/media) requires authentication and
# cannot be used to list recent uploads. The Last-Modified header on PDF files
# is the only reliable way to determine exact upload date.
#
# NOTE: The video index is pre-filtered to recent years and loaded inside an
# iframe — it is a static HTML file, not a JS-rendered page, so a plain urllib
# request suffices.
#
# NOTE: The server sits behind Sucuri and returns 403 for non-browser User-Agent
# strings on some paths. The script uses a browser-like UA throughout.
#
# NOTE: Because this script makes HEAD requests for each candidate PDF, runtime
# scales with the number of boards active in the current month. For a 3-day
# window it typically makes 70–120 HEAD requests, completing in under 2 minutes.

import argparse
import datetime
import email.utils
import gzip
import os
import re
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

YT_DLP_NODE = "node:/home/richkirby/.nvm/versions/node/v20.20.2/bin/node"  # yt-dlp needs Node 20+; system node is 18

# --- Configuration ---
BASE_URL = "https://eltownhall.com"
BOARDS_URL = f"{BASE_URL}/government/boards-commissions/"
OUTPUT_DIR = "beat-archive/east-lyme-agendas"
DAYS_BACK = 3
PAGE_DELAY = 0.5    # seconds between page fetches
HEAD_DELAY = 0.25   # seconds between HEAD requests
DOWNLOAD_DELAY = 0.8

UA = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36"
)

# Matches board/commission slug pages in the boards index
_BOARD_URL_RE = re.compile(
    r'href="(https?://eltownhall\.com/government/boards-commissions/([^/"]+)/)"',
    re.IGNORECASE,
)

# Matches PDF links inside agenda/minutes sub-pages
_PDF_LINK_RE = re.compile(
    r'href="(https?://eltownhall\.com/wp-content/uploads/(\d{4})/(\d{2})/[^"]+\.pdf)"',
    re.IGNORECASE,
)

# Matches upload-year/month folder in a PDF URL for fast pre-filtering
_UPLOAD_MONTH_RE = re.compile(r"/wp-content/uploads/(\d{4})/(\d{2})/")

# Matches video entries in the YouTube index HTML:
#   <b>&diams;MM/DD/YYYY</b> <b>Board Name</b>
#   <input ... onclick="javascript:playVideo('VIDEO_ID');" ...>
_VIDEO_DATE_RE = re.compile(r"&diams;(\d{2}/\d{2}/\d{4})")
_VIDEO_BOARD_RE = re.compile(r"&diams;\d{2}/\d{2}/\d{4}</b>\s*<b>(.*?)</b>")
_VIDEO_ID_RE = re.compile(r"playVideo\(['\"]([A-Za-z0-9_-]+)['\"]\)")


# --- HTTP helpers ---

def _make_request(url, method="GET", accept="text/html,*/*"):
    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": UA,
            "Accept": accept,
            "Accept-Encoding": "gzip, deflate",
        },
        method=method,
    )
    return req


def _decompress(raw, headers):
    enc = headers.get("Content-Encoding", "")
    if enc == "gzip" or raw[:2] == b"\x1f\x8b":
        return gzip.decompress(raw)
    return raw


def fetch_html(url):
    req = _make_request(url)
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            if r.status != 200:
                return None
            raw = _decompress(r.read(), r.headers)
            charset = r.headers.get_content_charset() or "utf-8"
            return raw.decode(charset, errors="replace")
    except urllib.error.HTTPError as e:
        if e.code != 404:
            print(f"  HTTP {e.code} — {url}", file=sys.stderr)
        return None
    except urllib.error.URLError as e:
        print(f"  ERROR fetching {url}: {e}", file=sys.stderr)
        return None


def head_last_modified(url):
    """Return the Last-Modified date of a URL as a datetime.date, or None."""
    req = _make_request(url, method="HEAD", accept="application/pdf,*/*")
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            lm = r.headers.get("Last-Modified")
            if lm:
                dt = email.utils.parsedate_to_datetime(lm)
                return dt.date()
    except Exception:
        pass
    return None


def download_pdf(url, dest_path):
    """Download a PDF to dest_path. Returns True on success."""
    req = _make_request(url, accept="application/pdf,*/*")
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            with open(dest_path, "wb") as f:
                f.write(r.read())
        return True
    except Exception as e:
        print(f"  WARNING: {e}", file=sys.stderr)
        return False


def download_video(video_url, dest_path):
    """Download a YouTube video via yt-dlp. Returns True on success."""
    cmd = [
        "yt-dlp", "--js-runtimes", YT_DLP_NODE,
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

def parse_boards(html_text):
    """
    Extract board slug URLs from the boards-commissions index page.
    Returns list of {name_slug, url} dicts (one per unique board slug).
    Sub-pages of boards (e.g. mooring-info) are excluded.
    """
    boards = []
    seen = set()
    for m in _BOARD_URL_RE.finditer(html_text):
        url = m.group(1)
        slug = m.group(2).lower()
        # Skip sub-pages (those whose URL ends with a second slug level)
        # Boards index links are all exactly one level deep under boards-commissions/
        if slug in seen:
            continue
        seen.add(slug)
        boards.append({"slug": slug, "url": url})
    return boards


def parse_pdf_links(html_text):
    """Return list of PDF URLs found in agenda/minutes sub-page HTML."""
    seen = set()
    urls = []
    for m in _PDF_LINK_RE.finditer(html_text):
        url = m.group(1)
        if url not in seen:
            seen.add(url)
            urls.append(url)
    return urls


def parse_video_index(html_text, cutoff, video_ahead):
    """
    Parse the YouTube index HTML (YouTubeLinks{YEAR}.html).
    Returns list of {board, meeting_date, video_id, video_url} dicts
    whose meeting date falls in [cutoff - video_ahead, today].
    """
    today = datetime.date.today()
    look_back = cutoff
    look_ahead = today + datetime.timedelta(days=video_ahead)

    entries = []
    # Split on paragraph boundaries to handle per-entry parsing robustly
    for para in re.split(r"</p>", html_text, flags=re.IGNORECASE):
        date_m = _VIDEO_DATE_RE.search(para)
        vid_m = _VIDEO_ID_RE.search(para)
        if not (date_m and vid_m):
            continue

        date_str = date_m.group(1)  # MM/DD/YYYY
        try:
            meeting_date = datetime.datetime.strptime(date_str, "%m/%d/%Y").date()
        except ValueError:
            continue

        if not (look_back <= meeting_date <= look_ahead):
            continue

        board_m = _VIDEO_BOARD_RE.search(para)
        board_name = re.sub(r"<[^>]+>", "", board_m.group(1)).strip() if board_m else "Unknown"
        video_id = vid_m.group(1)

        entries.append({
            "board": board_name,
            "meeting_date": meeting_date,
            "video_id": video_id,
            "video_url": f"https://www.youtube.com/watch?v={video_id}",
        })
    return entries


# --- File naming ---

def slugify(text, max_len=50):
    text = text.lower().strip()
    text = re.sub(r"[/\\&]", "-", text)
    text = re.sub(r"\s+-\s+", "-", text)
    text = re.sub(r"[^\w\s-]", "", text)
    text = re.sub(r"[\s_]+", "-", text)
    text = re.sub(r"-{2,}", "-", text)
    return text.strip("-")[:max_len]


def make_doc_dest(board_slug, doc_type, date_uploaded, output_dir, counter=0):
    month_dir = os.path.join(output_dir, date_uploaded.strftime("%Y-%m"))
    os.makedirs(month_dir, exist_ok=True)
    suffix = f"-{counter}" if counter > 0 else ""
    fname = f"{date_uploaded.strftime('%Y-%m-%d')}-{board_slug}-{doc_type}{suffix}.pdf"
    return os.path.join(month_dir, fname)


def make_video_dest(board, meeting_date, output_dir, counter=0):
    month_dir = os.path.join(output_dir, meeting_date.strftime("%Y-%m"))
    os.makedirs(month_dir, exist_ok=True)
    suffix = f"-{counter}" if counter > 0 else ""
    fname = f"{meeting_date.strftime('%Y-%m-%d')}-{slugify(board)}-video{suffix}.mp4"
    return os.path.join(month_dir, fname)


# --- Main ---

def main():
    parser = argparse.ArgumentParser(
        description=(
            "Download East Lyme CT municipal agendas, minutes, and video recordings "
            "posted in the past N days."
        )
    )
    parser.add_argument(
        "--days", type=int, default=DAYS_BACK, metavar="N",
        help=f"Look back N days for documents (default: {DAYS_BACK})",
    )
    parser.add_argument(
        "--video-days", type=int, default=None, metavar="N",
        help="Look back N days for videos (default: same as --days)",
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
        help="Only process boards whose slug contains NAME (case-insensitive)",
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
        help="Download only video recordings (skip PDFs)",
    )
    args = parser.parse_args()

    now = datetime.datetime.now()
    if (now.weekday() == 5 and now.hour >= 18) or (now.weekday() == 6 and now.hour < 12):
        print("Skipping — no downloads on Saturday nights or Sunday mornings.")
        sys.exit(0)

    do_docs = not args.video_only
    do_video = args.include_video or args.video_only
    video_days = args.video_days if args.video_days is not None else args.days

    today = datetime.date.today()
    cutoff = today - datetime.timedelta(days=args.days)
    video_cutoff = today - datetime.timedelta(days=video_days)

    # Pre-compute the set of YYYY/MM strings that overlap the date window,
    # used to skip HEAD requests for PDFs uploaded months ago.
    recent_months = set()
    d = cutoff.replace(day=1)
    while d <= today:
        recent_months.add(f"{d.year:04d}/{d.month:02d}")
        # advance to next month
        if d.month == 12:
            d = d.replace(year=d.year + 1, month=1)
        else:
            d = d.replace(month=d.month + 1)

    print(f"Date window (docs)  : {cutoff} to {today}")
    print(f"Date window (videos): {video_cutoff} to {today}")
    print(f"Boards index        : {BOARDS_URL}")
    if not args.dry_run:
        print(f"Output dir          : {args.output_dir}")
    print()

    # ------------------------------------------------------------------ #
    # Phase 1: discover PDF candidates from agenda/minutes sub-pages       #
    # ------------------------------------------------------------------ #

    all_pdf_tasks = []   # {board_slug, doc_type, url, upload_month}

    if do_docs:
        print("Fetching boards index...")
        index_html = fetch_html(BOARDS_URL)
        if not index_html:
            print("ERROR: Could not fetch the boards index.", file=sys.stderr)
            sys.exit(1)

        boards = parse_boards(index_html)
        print(f"  Found {len(boards)} board/commission page(s).")

        if args.board:
            flt = args.board.lower()
            boards = [b for b in boards if flt in b["slug"]]
            print(f"  Filtered to {len(boards)} board(s) matching '{args.board}'.")

        print()
        year = today.year
        n_boards = len(boards)

        for i, board in enumerate(boards, 1):
            slug = board["slug"]
            board_url = board["url"]

            doc_types = []
            if not args.no_agendas:
                doc_types.append(("agenda", f"{board_url}{slug}-{year}-agendas/"))
            if not args.no_minutes:
                doc_types.append(("minutes", f"{board_url}{slug}-{year}-minutes/"))

            board_label = slug[:35]
            print(f"  [{i:>2}/{n_boards}] {board_label}...", end=" ", flush=True)

            found = 0
            for doc_type, sub_url in doc_types:
                page_html = fetch_html(sub_url)
                if not page_html:
                    time.sleep(PAGE_DELAY)
                    continue

                pdf_urls = parse_pdf_links(page_html)
                for pdf_url in pdf_urls:
                    m = _UPLOAD_MONTH_RE.search(pdf_url)
                    if not m:
                        continue
                    upload_month = f"{m.group(1)}/{m.group(2)}"
                    if upload_month in recent_months:
                        all_pdf_tasks.append({
                            "board_slug": slug,
                            "doc_type": doc_type,
                            "url": pdf_url,
                            "upload_month": upload_month,
                        })
                        found += 1

                time.sleep(PAGE_DELAY)

            print(f"{found} candidate(s)")

        print()
        print(f"PDF candidates in date window: {len(all_pdf_tasks)}")
        print()

        # HEAD requests to get exact Last-Modified date
        print("Checking Last-Modified dates...")
        confirmed_pdfs = []
        fname_counters: dict = {}

        for t in all_pdf_tasks:
            lm = head_last_modified(t["url"])
            time.sleep(HEAD_DELAY)
            if lm is None or lm < cutoff:
                continue

            key = (t["board_slug"], t["doc_type"], lm)
            fname_counters[key] = fname_counters.get(key, 0) + 1
            counter = fname_counters[key] - 1
            t["last_modified"] = lm
            t["counter"] = counter
            confirmed_pdfs.append(t)

        confirmed_pdfs.sort(key=lambda x: x["last_modified"], reverse=True)
        print(f"  {len(confirmed_pdfs)} PDF(s) posted within {args.days} day(s).")
        print()
    else:
        confirmed_pdfs = []

    # ------------------------------------------------------------------ #
    # Phase 2: parse video index                                           #
    # ------------------------------------------------------------------ #

    confirmed_videos = []
    if do_video:
        video_index_url = (
            f"{BASE_URL}/wp-content/uploads/YouTubeIndexes/"
            f"YouTubeLinks{today.year}.html"
        )
        print(f"Fetching video index: {video_index_url}")
        vid_html = fetch_html(video_index_url)
        if vid_html:
            confirmed_videos = parse_video_index(vid_html, video_cutoff, video_ahead=0)
            confirmed_videos.sort(key=lambda x: x["meeting_date"], reverse=True)
            print(f"  {len(confirmed_videos)} video(s) found in window.")
        else:
            print("  WARNING: Could not fetch video index.", file=sys.stderr)
        print()

    # ------------------------------------------------------------------ #
    # Phase 3: report or download                                          #
    # ------------------------------------------------------------------ #

    total = len(confirmed_pdfs) + len(confirmed_videos)

    if total == 0:
        print("No items found within the date window.")
        return

    if args.dry_run:
        if confirmed_pdfs:
            print(f"{'Board':<38} {'Posted':<12} Type")
            print("-" * 62)
            for t in confirmed_pdfs:
                print(f"{t['board_slug'][:37]:<38} {t['last_modified']!s:<12} {t['doc_type']}")
        if confirmed_videos:
            print()
            print(f"{'Board':<38} {'Meeting':<12} Type")
            print("-" * 62)
            for v in confirmed_videos:
                print(f"{v['board'][:37]:<38} {v['meeting_date']!s:<12} video  {v['video_url']}")
        noun = (
            "item(s)" if (confirmed_pdfs and confirmed_videos)
            else ("recording(s)" if confirmed_videos else "document(s)")
        )
        print(f"\n{total} {noun}. Re-run without --dry-run to download.")
        return

    os.makedirs(args.output_dir, exist_ok=True)
    log_path = os.path.join(args.output_dir, "download-log.txt")
    log_lines = []
    downloaded = skipped = failed = 0

    for t in confirmed_pdfs:
        dest = make_doc_dest(
            t["board_slug"], t["doc_type"], t["last_modified"],
            args.output_dir, t["counter"]
        )
        label = os.path.basename(dest)

        if os.path.exists(dest):
            print(f"  skip (exists)  {label}")
            skipped += 1
            continue

        print(f"  [posted {t['last_modified']}] {t['board_slug']} — {t['doc_type']}")
        print(f"  downloading    {label}")

        if download_pdf(t["url"], dest):
            downloaded += 1
            log_lines.append(f"{datetime.datetime.now().isoformat()}  OK       {dest}")
        else:
            failed += 1
            log_lines.append(f"{datetime.datetime.now().isoformat()}  FAILED   {t['url']}")
            if os.path.exists(dest):
                os.remove(dest)

        time.sleep(DOWNLOAD_DELAY)

    vid_fname_counters: dict = {}
    for v in confirmed_videos:
        key = (v["board"], v["meeting_date"])
        vid_fname_counters[key] = vid_fname_counters.get(key, 0) + 1
        counter = vid_fname_counters[key] - 1

        dest = make_video_dest(v["board"], v["meeting_date"], args.output_dir, counter)
        label = os.path.basename(dest)

        if os.path.exists(dest):
            print(f"  skip (exists)  {label}")
            skipped += 1
            continue

        print(f"  [{v['meeting_date']}] {v['board']} — video")
        print(f"  downloading    {label}")

        if download_video(v["video_url"], dest):
            downloaded += 1
            log_lines.append(f"{datetime.datetime.now().isoformat()}  OK       {dest}")
        else:
            failed += 1
            log_lines.append(f"{datetime.datetime.now().isoformat()}  FAILED   {v['video_url']}")
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
#    python3 scripts/download-east-lyme-agendas.py --dry-run
#
# 2. Include video recordings:
#    python3 scripts/download-east-lyme-agendas.py --include-video
#
# 3. Videos only:
#    python3 scripts/download-east-lyme-agendas.py --video-only
#
# 4. Widen the lookback window:
#    python3 scripts/download-east-lyme-agendas.py --days 7
#
# 5. Single board (partial slug match):
#    python3 scripts/download-east-lyme-agendas.py --board "board-of-selectmen"
#
# 6. Agendas only (skip minutes):
#    python3 scripts/download-east-lyme-agendas.py --no-minutes
#
# 7. Save files somewhere else:
#    python3 scripts/download-east-lyme-agendas.py --output-dir ~/Downloads/east-lyme
#
# 8. Run on a schedule (cron — nightly at 8 PM):
#    0 20 * * * cd /path/to/repo && python3 scripts/download-east-lyme-agendas.py
#
# 9. Process downloaded files with Claude afterward:
#    python3 scripts/download-east-lyme-agendas.py && \
#    bash scripts/batch-process.sh beat-archive/east-lyme-agendas/
#
# BOARDS (35 as of 2026):
#   ad-hoc-short-term-rental-committee, agriculture-in-our-community,
#   aquifer-protection-agency, board-of-assessment-appeals, board-of-education,
#   board-of-finance, board-of-selectmen, brookside-farm-museum-commission,
#   cable-tv-advisory-council, charter-revision-commission,
#   commission-on-aging, conservation-of-natural-resources-commission,
#   east-lyme-public-library-meetings-page, economic-development-commission,
#   fair-rent-commission, harbor-mgt-shellfish-commission,
#   hazard-mitigation-plan-committee, health-safety-committee,
#   historic-properties-commission, inland-wetlands-agency,
#   niantic-river-watershed-committee, parks-recreation-commission,
#   pension-board, planning-commission, police-commission,
#   town-building-committee, town-meetings,
#   waterford-east-lyme-shellfish-commission, water-sewer-commission,
#   yfhs-commission, youth-services-commission, zoning-board-of-appeals,
#   zoning-commission, and others.
#
# HOW "POSTED DATE" WORKS:
#   East Lyme uses WordPress with files stored at:
#     /wp-content/uploads/{YYYY}/{MM}/{filename}.pdf
#   The server returns a Last-Modified header for each file that reflects
#   when it was uploaded. The script uses this as the canonical "posted" date.
#   For videos, the posting date is approximated by the meeting date shown
#   in the YouTube index file (videos typically go up same-day or next-day).
