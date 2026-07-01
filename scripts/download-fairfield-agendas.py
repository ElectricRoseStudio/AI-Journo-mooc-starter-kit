#!/usr/bin/env python3
# download-fairfield-agendas.py
# Download municipal meeting agendas, minutes, and video recordings from
# Fairfield CT for meetings whose date falls within the past N days
# (and up to 7 days ahead, to catch agendas posted early for upcoming meetings).
#
# USAGE:
#   python3 scripts/download-fairfield-agendas.py [options]
#
# REQUIREMENTS:
#   - Python 3.6+  (no third-party packages needed for docs)
#   - yt-dlp       (required for --include-video; pip install yt-dlp)
#   - Internet connection
#
# WHAT IT DOES:
#   1. Fetches the Fairfield CT Agendas & Minutes Manager index to discover
#      all boards and their individual agenda page URLs
#   2. For each board, fetches its agenda page
#   3. Parses each meeting row for the date and document links
#   4. Downloads Agenda and Minutes PDFs whose meeting date falls within
#      the date window to beat-archive/fairfield-agendas/YYYY-MM/
#   5. Optionally downloads YouTube video recordings via yt-dlp
#      (--include-video flag; videos are typically 500 MB – 2 GB each)
#   6. Appends a download log to beat-archive/fairfield-agendas/download-log.txt
#
# SITE STRUCTURE:
#   Fairfield CT uses the Revize CMS (https://fairfieldct.gov/).
#   Boards do not use a third-party agenda platform; instead each board has
#   a dedicated page with all its meeting history:
#     /government/agendas___minutes_manager/index.php  — master index
#     /government/boards___commissions/{board}/agendas___minutes.php — board page
#
#   Each board page contains one HTML table per meeting. Tables use the
#   style "width: 100%; border-top: solid 1px #CCCCCC;". The first <td>
#   holds the meeting date in MM/DD/YY format. Subsequent columns link to:
#     Agenda  — "Document Center/Agendas & Minutes/.../Agenda ....pdf"
#     Packet  — "Document Center/.../Packet....pdf"  (large; skipped by default)
#     Minutes — "Document Center/Agendas & Minutes/.../Minutes ....pdf"
#     Video   — YouTube URL (FairTV; requires --include-video)
#
#   PDFs are served directly from fairfieldct.gov. No authentication required.
#   Older records (pre-Nov 2023) are in a separate archive at
#   filecloud.town.fairfield.ct.us and are NOT downloaded by this script.
#
#   Videos are posted to FairTV's YouTube channels:
#     Government: https://www.youtube.com/@fairtvgovernment720
#     Education:  https://www.youtube.com/@fairtveducation7011
#   YouTube URLs appear in three forms on board pages:
#     https://www.youtube.com/watch?v=VIDEO_ID
#     https://youtube.com/live/VIDEO_ID?feature=share
#     https://youtu.be/VIDEO_ID

import argparse
import datetime
import glob
import html as html_module
import os
import re
import subprocess
import sys

YT_DLP_NODE = "node:/home/richkirby/.nvm/versions/node/v20.20.2/bin/node"  # yt-dlp needs Node 20+; system node is 18
import time
import urllib.error
import urllib.parse
import urllib.request

# --- Configuration ---
BASE_URL = "https://fairfieldct.gov"
INDEX_URL = f"{BASE_URL}/government/agendas___minutes_manager/index.php"
OUTPUT_DIR = "beat-archive/fairfield-agendas"
DAYS_BACK = 4
DAYS_AHEAD = 7   # capture agendas posted early for upcoming meetings
DELAY_SECONDS = 1
PAGE_DELAY = 0.5  # delay between board page fetches

# Document link texts to download (lowercase; case-insensitive match)
DEFAULT_TYPES = {"agenda", "minutes"}

UA = "Fairfield-Agendas-Downloader/1.0 (journalism research)"


# --- HTTP helpers ---

def fetch_html(url):
    """GET url and return decoded HTML, or None on error."""
    req = urllib.request.Request(
        url, headers={"User-Agent": UA, "Accept": "text/html"}
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return r.read().decode("utf-8", errors="replace")
    except urllib.error.URLError as e:
        print(f"  ERROR fetching {url}: {e}", file=sys.stderr)
        return None


def download_file(url, dest_path):
    """Download url (a Document Center PDF) to dest_path. Returns True on success."""
    # URL-encode the path component, preserving / and & (server accepts unencoded &)
    parsed = urllib.parse.urlparse(url)
    encoded_path = urllib.parse.quote(parsed.path, safe="/&")
    encoded_url = urllib.parse.urlunparse(parsed._replace(path=encoded_path, query=""))
    req = urllib.request.Request(encoded_url, headers={"User-Agent": UA})
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            with open(dest_path, "wb") as f:
                f.write(r.read())
        return True
    except Exception as e:
        print(f"  WARNING: {e}", file=sys.stderr)
        return False


def download_video(youtube_url, dest_template):
    """
    Download a YouTube video using yt-dlp.
    dest_template must end in .%(ext)s — yt-dlp fills in the extension.
    Returns True on success.
    """
    cmd = [
        "yt-dlp", "--js-runtimes", YT_DLP_NODE,
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

def parse_board_list(html):
    """
    Return a list of (board_name, board_url) from the Agendas & Minutes Manager
    index page. Only includes boards under /government/boards___commissions/.
    """
    boards = []
    seen_urls = set()
    for href, raw_name in re.findall(
        r'href=\s*"(government/boards___commissions/[^"]+\.php)"[^>]*>\s*([^<]+)',
        html,
        re.IGNORECASE,
    ):
        board_url = f"{BASE_URL}/{href}"
        if board_url in seen_urls:
            continue
        seen_urls.add(board_url)
        board_name = html_module.unescape(raw_name.strip())
        boards.append((board_name, board_url))
    return boards


def parse_board_page(html, doc_types, include_video=False):
    """
    Parse all meeting rows from a board's agenda page.

    Each meeting is one <table style="...border-top..."> element.
    The first <td> contains date (MM/DD/YY) and meeting type.
    Subsequent <td> elements may contain <A href="...">Type</A> document links.

    Returns list of {meeting_date, doc_type, url}.
    doc_type is "Video" for YouTube links (only when include_video=True).
    """
    results = []

    # Each meeting is its own table with this border-top style
    tables = re.findall(
        r'<table[^>]+border-top[^>]*>(.*?)</table>',
        html,
        re.DOTALL | re.IGNORECASE,
    )

    for table in tables:
        # Extract the first <td> to find the meeting date
        first_td = re.search(r'<td[^>]*>(.*?)</td>', table, re.DOTALL | re.IGNORECASE)
        if not first_td:
            continue

        td_text = re.sub(r"<[^>]+>", " ", first_td.group(1))
        td_text = re.sub(r"\s+", " ", td_text).strip()

        # Date: MM/DD/YY at the start of the cell
        date_m = re.match(r"(\d{1,2})/(\d{1,2})/(\d{2})\b", td_text)
        if not date_m:
            continue
        month = int(date_m.group(1))
        day = int(date_m.group(2))
        year = 2000 + int(date_m.group(3))
        try:
            meeting_date = datetime.date(year, month, day)
        except ValueError:
            continue

        # Find all document links in this table row
        for href, link_text in re.findall(
            r'href="([^"]+)"[^>]*>\s*([^<]+?)\s*</[Aa]>',
            table,
            re.IGNORECASE,
        ):
            link_text = link_text.strip()
            link_lower = link_text.lower()

            # Handle YouTube video links separately from PDF doc_types
            if link_lower == "video":
                if include_video and ("youtube.com" in href or "youtu.be" in href):
                    results.append({
                        "meeting_date": meeting_date,
                        "doc_type": "Video",
                        "url": href,
                    })
                continue

            if link_lower not in doc_types:
                continue

            # Skip links to other external sites
            if href.startswith("http") and BASE_URL not in href:
                continue

            # Build full URL; strip the ?t=... cache-buster
            href_clean = href.split("?")[0].strip()
            if href_clean.startswith("http"):
                doc_url = href_clean
            else:
                doc_url = f"{BASE_URL}/{href_clean.lstrip('/')}"

            results.append({
                "meeting_date": meeting_date,
                "doc_type": link_text,
                "url": doc_url,
            })

    return results


# --- Utilities ---

def slugify(text, max_len=60):
    text = text.lower().strip()
    text = re.sub(r"[/\\]", "-", text)
    text = re.sub(r"\s+-\s+", "-", text)
    text = re.sub(r"[^\w\s-]", "", text)
    text = re.sub(r"[\s_]+", "-", text)
    text = re.sub(r"-{2,}", "-", text)
    return text.strip("-")[:max_len]


def make_dest_path(board_name, doc_type, meeting_date, output_dir, suffix=""):
    date_prefix = meeting_date.strftime("%Y-%m-%d")
    month_dir = meeting_date.strftime("%Y-%m")
    board_slug = slugify(board_name, max_len=40)
    type_slug = slugify(doc_type, max_len=10)
    month_path = os.path.join(output_dir, month_dir)
    os.makedirs(month_path, exist_ok=True)
    fname = f"{date_prefix}-{board_slug}-{type_slug}{suffix}.pdf"
    return os.path.join(month_path, fname)


def make_video_dest_path(board_name, meeting_date, output_dir, suffix=""):
    """Return a yt-dlp output template (ends in .%(ext)s) for a video download."""
    date_prefix = meeting_date.strftime("%Y-%m-%d")
    month_dir = meeting_date.strftime("%Y-%m")
    board_slug = slugify(board_name, max_len=40)
    month_path = os.path.join(output_dir, month_dir)
    os.makedirs(month_path, exist_ok=True)
    fname = f"{date_prefix}-{board_slug}-video{suffix}.%(ext)s"
    return os.path.join(month_path, fname)


# --- Main ---

def main():
    parser = argparse.ArgumentParser(
        description=(
            "Download Fairfield CT municipal agendas, minutes, and video recordings "
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
        help="Only process boards whose name contains NAME (case-insensitive)",
    )
    parser.add_argument(
        "--include-packets", action="store_true",
        help="Also download Packet files (can be large)",
    )
    parser.add_argument(
        "--include-video", action="store_true",
        help="Also download YouTube video recordings via yt-dlp (can be very large)",
    )
    parser.add_argument(
        "--docs-only", action="store_true",
        help="Download only PDFs; skip video even if --include-video is set",
    )
    args = parser.parse_args()

    now = datetime.datetime.now()
    if (now.weekday() == 5 and now.hour >= 18) or (now.weekday() == 6 and now.hour < 12):  # Saturday night, Sunday morning
        print("Skipping — no downloads on Saturday nights or Sunday mornings.")
        sys.exit(0)

    today = datetime.date.today()
    cutoff = today - datetime.timedelta(days=args.days)
    future_limit = today + datetime.timedelta(days=args.ahead)

    doc_types = set(DEFAULT_TYPES)
    if args.include_packets:
        doc_types.add("packet")

    include_video = args.include_video and not args.docs_only

    print(f"Date window : {cutoff} to {future_limit}")
    print(f"Site        : {BASE_URL}")
    if not args.dry_run:
        print(f"Output dir  : {args.output_dir}")
    if include_video:
        print("Video       : enabled (yt-dlp)")
    print()

    # --- Step 1: fetch the board index ---
    print("Fetching Agendas & Minutes Manager index...")
    index_html = fetch_html(INDEX_URL)
    if not index_html:
        print("ERROR: Could not fetch the index page.", file=sys.stderr)
        sys.exit(1)

    boards = parse_board_list(index_html)
    print(f"Found {len(boards)} board(s).\n")

    if args.board:
        filter_name = args.board.lower()
        boards = [(name, url) for name, url in boards if filter_name in name.lower()]
        print(f"Filtered to {len(boards)} board(s) matching '{args.board}'.\n")

    # --- Step 2: collect matching documents ---
    matches = []

    for board_name, board_url in boards:
        board_html = fetch_html(board_url)
        if not board_html:
            continue

        rows = parse_board_page(board_html, doc_types, include_video=include_video)
        for row in rows:
            if row["meeting_date"] < cutoff or row["meeting_date"] > future_limit:
                continue
            matches.append({
                "board": board_name,
                "meeting_date": row["meeting_date"],
                "doc_type": row["doc_type"],
                "url": row["url"],
            })

        time.sleep(PAGE_DELAY)

    # Detect duplicate (board, doc_type, meeting_date) combos and assign suffixes
    seen_keys: dict = {}
    for m in matches:
        key = (m["board"], m["doc_type"], m["meeting_date"])
        seen_keys[key] = seen_keys.get(key, 0) + 1
    key_counter: dict = {}
    for m in matches:
        key = (m["board"], m["doc_type"], m["meeting_date"])
        if seen_keys[key] > 1:
            key_counter[key] = key_counter.get(key, 0) + 1
            m["suffix"] = f"-{key_counter[key]}"
        else:
            m["suffix"] = ""

    matches.sort(key=lambda x: (x["meeting_date"], x["board"]), reverse=True)

    print(
        f"\nFound {len(matches)} document(s) across "
        f"{len({m['board'] for m in matches})} board(s)."
    )
    print()

    if not matches:
        return

    if args.dry_run:
        print(f"{'Board':<42} {'Date':<12} Type")
        print("-" * 68)
        for m in matches:
            print(f"{m['board'][:41]:<42} {m['meeting_date']!s:<12} {m['doc_type']}")
        print(f"\n{len(matches)} document(s). Re-run without --dry-run to download.")
        return

    # --- Step 3: download ---
    os.makedirs(args.output_dir, exist_ok=True)
    log_path = os.path.join(args.output_dir, "download-log.txt")
    log_lines = []
    downloaded = skipped = failed = 0

    for m in matches:
        is_video = m["doc_type"].lower() == "video"

        if is_video:
            dest = make_video_dest_path(
                m["board"], m["meeting_date"],
                args.output_dir, suffix=m.get("suffix", ""),
            )
            label = os.path.basename(dest)

            if video_already_exists(dest):
                print(f"  skip (exists)  {label}")
                skipped += 1
                continue

            print(f"  [{m['meeting_date']}] {m['board']} — Video")
            print(f"  downloading    {label}")
            print(f"  source         {m['url']}")

            if download_video(m["url"], dest):
                downloaded += 1
                log_lines.append(
                    f"{datetime.datetime.now().isoformat()}  OK       {dest}"
                )
            else:
                failed += 1
                log_lines.append(
                    f"{datetime.datetime.now().isoformat()}  FAILED   {m['url']}"
                )

        else:
            dest = make_dest_path(
                m["board"], m["doc_type"], m["meeting_date"],
                args.output_dir, suffix=m.get("suffix", ""),
            )
            label = os.path.basename(dest)

            if os.path.exists(dest):
                print(f"  skip (exists)  {label}")
                skipped += 1
                continue

            print(f"  [{m['meeting_date']}] {m['board']} — {m['doc_type']}")
            print(f"  downloading    {label}")

            if download_file(m["url"], dest):
                downloaded += 1
                log_lines.append(
                    f"{datetime.datetime.now().isoformat()}  OK       {dest}"
                )
            else:
                failed += 1
                log_lines.append(
                    f"{datetime.datetime.now().isoformat()}  FAILED   {m['url']}"
                )
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
#    python3 scripts/download-fairfield-agendas.py --dry-run
#
# 2. Download docs + video recordings for the past 30 days:
#    python3 scripts/download-fairfield-agendas.py --include-video
#
# 3. Narrow to one board:
#    python3 scripts/download-fairfield-agendas.py --board "Board of Selectpersons"
#
# 4. Change the lookback window:
#    python3 scripts/download-fairfield-agendas.py --days 7
#
# 5. Also include full agenda packets (can be large):
#    python3 scripts/download-fairfield-agendas.py --include-packets
#
# 6. Documents only (no video even if flag is passed):
#    python3 scripts/download-fairfield-agendas.py --docs-only
#
# 7. Save files somewhere else:
#    python3 scripts/download-fairfield-agendas.py --output-dir ~/Downloads/fairfield
#
# 8. Run on a schedule (cron — 8 AM daily):
#    0 8 * * * cd /path/to/repo && python3 scripts/download-fairfield-agendas.py
#
# 9. Run daily with video included:
#    0 8 * * * cd /path/to/repo && python3 scripts/download-fairfield-agendas.py --include-video
#
# 10. Process downloaded PDFs with Claude afterward:
#    python3 scripts/download-fairfield-agendas.py && bash scripts/batch-process.sh beat-archive/fairfield-agendas/
#
# NOTE: The --ahead flag (default: 7 days) captures agendas for upcoming meetings
# that have already been published. Run daily to stay current.
#
# NOTE: Fairfield CT does not use CivicPlus, CivicClerk, or any other third-party
# agenda platform. Documents are served directly from fairfieldct.gov as PDFs
# in the Document Center. No browser or authentication is required.
#
# NOTE: Records before November 2023 are in a separate archive at
# https://filecloud.town.fairfield.ct.us/url/archive and are NOT downloaded
# by this script.
#
# NOTE: Only "Agenda" and "Minutes" link types are downloaded by default.
# Use --include-packets to also get backup packets (can be large).
# Use --include-video to also download YouTube video recordings via yt-dlp.
# Videos are typically 500 MB – 2 GB each. Files that already exist on disk
# are skipped, so re-runs are safe.
#
# NOTE: Fairfield CT posts videos to its FairTV YouTube channels:
#   Government: https://www.youtube.com/@fairtvgovernment720
#   Education:  https://www.youtube.com/@fairtveducation7011
# The script discovers video links from each board's agenda page, so only
# meetings within your configured date window are downloaded.
