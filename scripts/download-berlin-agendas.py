#!/usr/bin/env python3
# download-berlin-agendas.py
# Download municipal meeting agendas, minutes, and video recordings from the
# Berlin, CT eGov site and YouTube channel.
#
# USAGE:
#   python3 scripts/download-berlin-agendas.py [options]
#
# REQUIREMENTS:
#   - Python 3.6+  (no third-party packages needed for docs)
#   - yt-dlp       (for video: pip install yt-dlp or brew install yt-dlp)
#   - Internet connection
#
# WHAT IT DOES:
#   Documents (default):
#     1. Fetches berlinct.gov/department/index.php?structureid=97 to discover
#        all boards and committees, then supplements with Town Council (dept 115)
#        and Police Commission (dept 130) which are listed separately
#     2. For each board, queries the eGov Document Center for agendas (type 11)
#        and minutes (type 12), year by year within the date window
#     3. Paginates through all result pages (25 docs per page)
#     4. Filters to documents whose meeting date falls within the date window
#     5. Downloads each file to beat-archive/berlin-agendas/YYYY-MM/
#     6. Appends a download log to beat-archive/berlin-agendas/download-log.txt
#
#   Video (enabled by default; disable with --no-video):
#     7. Downloads recent videos from the Berlin CT YouTube channel using yt-dlp
#        Channel: https://www.youtube.com/channel/UC5AtkQIY7aNVJEd6S4XWhzA
#
# SITE STRUCTURE (eGov CMS, berlinct.gov):
#   Boards index:   /department/index.php?structureid=97
#   Board page:     /department/board.php?structureid={id}
#   Doc search:     /egov/apps/document/center.egov
#                     ?eGov_searchDepartment={id}&eGov_searchType={type}
#                     &eGov_searchYear={year}
#                     [&app=4&sect=content&page=4_{N}  for page N>1]
#   Download:       /egov/apps/document/center.egov?view=item&id={docid}
#                     → served directly as PDF, DOCX, or DOC
#   Doc types:      11 = Agendas, 12 = Minutes
#   YouTube:        https://www.youtube.com/channel/UC5AtkQIY7aNVJEd6S4XWhzA
#
#   The "Date" column in search results is the posting/upload date (MM/DD/YYYY),
#   not necessarily the meeting date. Meeting dates are in document titles as
#   YYYY-MM-DD, at either the start or end of the title.
#
# NOTE: Site requires a browser-like User-Agent; plain urllib with the right
#   UA header works fine. No JavaScript challenge (unlike some CT towns).
#
# NOTE: Town Council and Police Commission are NOT in the boards index at
#   structureid=97; they are listed separately on the meetings page. This
#   script adds them via SUPPLEMENTAL_BOARDS.
#
# NOTE: Minutes for some boards also appear on the town's Dropbox shared folder:
#   https://www.dropbox.com/sh/8wg3yonxctgwr7p/AABhLzYWNf0sjk-80E8VVDUWa
#   That folder cannot be scraped automatically. Documents in the eGov system
#   (type 12) are downloaded by this script; the Dropbox folder requires
#   manual access.
#
# DATA RANGE: Documents available from 2020 onward.

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
BASE_URL = "https://www.berlinct.gov"
DOC_CENTER = f"{BASE_URL}/egov/apps/document/center.egov"
BOARDS_INDEX_URL = f"{BASE_URL}/department/index.php?structureid=97"
YOUTUBE_CHANNEL = "https://www.youtube.com/channel/UC5AtkQIY7aNVJEd6S4XWhzA"
OUTPUT_DIR = "beat-archive/berlin-agendas"
DAYS_BACK = 4
DAYS_AHEAD = 7
PAGE_DELAY = 0.5
DOWNLOAD_DELAY = 0.8
PER_PAGE = 25
MIN_YEAR = 2020

DOC_TYPES = {11: "agenda", 12: "minutes"}

# Boards present on the meetings page but absent from the boards index (structureid=97)
SUPPLEMENTAL_BOARDS = [
    (115, "Town Council"),
    (130, "Police Commission"),
]

UA = "Berlin-CT-Agendas-Downloader/1.0 (journalism research)"

# Matches board links: board.php?structureid=NNN
_BOARD_RE = re.compile(
    r'href="https://www\.berlinct\.gov/department/board\.php\?structureid=(\d+)"'
    r'[^>]*>([^<]+)</a>',
    re.IGNORECASE,
)

# Skip this structureid — it's a general info page, not a real board
_SKIP_STRUCTURES = {157}

# Date in title: YYYY-MM-DD anywhere in the string
_TITLE_DATE_RE = re.compile(r'(20\d{2})-(\d{2})-(\d{2})')

# Posted date in listing: MM/DD/YYYY
_POSTED_DATE_RE = re.compile(r'(\d{2})/(\d{2})/(\d{4})')

# Document row in listing HTML
_ROW_RE = re.compile(
    r'class="eGov_row(?:Odd|Even)"[^>]*>(.*?)</tr>',
    re.S,
)
# Within a row: posted date, doc id, title, file type
_ROW_DATE_RE = re.compile(r'(\d{2}/\d{2}/\d{4})')
_ROW_ID_RE = re.compile(r'view=item&(?:amp;)?id=(\d+)')
_ROW_TITLE_RE = re.compile(r'eGov_listItemLink"[^>]*>([^<]+)</a>')
_ROW_EXT_RE = re.compile(r'alt="(pdf|docx|doc)"', re.IGNORECASE)

# Total result count: "of NNN"
_TOTAL_RE = re.compile(r'\bof (\d+)\b')


# --- HTTP helpers ---

def fetch_html(url, retries=3):
    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": UA,
            "Accept": "text/html,application/xhtml+xml,*/*",
        },
    )
    for attempt in range(retries + 1):
        try:
            with urllib.request.urlopen(req, timeout=60) as r:
                charset = r.headers.get_content_charset() or "utf-8"
                return r.read().decode(charset, errors="replace")
        except urllib.error.HTTPError as e:
            if e.code == 404:
                return None
            print(f"  HTTP {e.code} fetching {url}", file=sys.stderr)
            if attempt < retries:
                time.sleep(3 * (attempt + 1))
        except (urllib.error.URLError, TimeoutError, OSError) as e:
            print(f"  WARNING (attempt {attempt+1}): {e}", file=sys.stderr)
            if attempt < retries:
                time.sleep(3 * (attempt + 1))
    return None


def download_file(url, dest_path):
    """Download url → dest_path. Returns (True, content_type) or (False, None)."""
    req = urllib.request.Request(
        url,
        headers={"User-Agent": UA, "Accept": "*/*"},
    )
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            content_type = r.headers.get_content_type() or ""
            data = r.read()
        with open(dest_path, "wb") as f:
            f.write(data)
        return True, content_type
    except Exception as e:
        print(f"  WARNING: {e}", file=sys.stderr)
        return False, None


def download_channel_videos(channel_url, output_dir, date_after, dry_run=False):
    """
    Download recent videos from the Berlin CT YouTube channel using yt-dlp.
    date_after: datetime.date — skip videos uploaded before this date.
    Returns (downloaded, skipped, failed) counts.
    """
    date_str = date_after.strftime("%Y%m%d")
    video_dir = os.path.join(output_dir, "videos")

    if dry_run:
        # List videos without downloading
        cmd = [
            "yt-dlp", "--flat-playlist", "--dateafter", date_str,
            "--print", "%(upload_date)s %(title)s",
            "--no-warnings", "--quiet",
            channel_url,
        ]
        try:
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
            lines = [l for l in result.stdout.splitlines() if l.strip()]
            return lines
        except FileNotFoundError:
            print("  WARNING: yt-dlp not found — skipping video listing", file=sys.stderr)
            return []
        except subprocess.TimeoutExpired:
            print("  WARNING: yt-dlp timed out listing channel videos", file=sys.stderr)
            return []

    os.makedirs(video_dir, exist_ok=True)
    cmd = [
        "yt-dlp",
        "--dateafter", date_str,
        "-f", "bestvideo+bestaudio/best",
        "--merge-output-format", "mp4",
        "-o", os.path.join(video_dir, "%(upload_date)s-%(title)s.%(ext)s"),
        "--no-overwrites",
        "--quiet",
        "--no-warnings",
        "--write-info-json",
        channel_url,
    ]
    downloaded = skipped = failed = 0
    try:
        result = subprocess.run(cmd, timeout=1800)
        if result.returncode == 0:
            downloaded = 1  # yt-dlp handles counting internally
        else:
            failed = 1
    except FileNotFoundError:
        print("  ERROR: yt-dlp not found. Install with: pip install yt-dlp", file=sys.stderr)
        failed = 1
    except subprocess.TimeoutExpired:
        print("  WARNING: yt-dlp timed out downloading channel videos", file=sys.stderr)
        failed = 1
    return downloaded, skipped, failed


# --- Board discovery ---

def discover_boards(index_html):
    """
    Return list of (structure_id: int, board_name: str) from the boards index,
    supplemented with Town Council and Police Commission which are absent from the
    index page but present on the meetings page.
    Skips the general-information page (id 157).
    """
    boards = []
    seen = set()
    for m in _BOARD_RE.finditer(index_html):
        sid = int(m.group(1))
        name = m.group(2).strip()
        if sid in _SKIP_STRUCTURES or sid in seen:
            continue
        seen.add(sid)
        boards.append((sid, name))
    for sid, name in SUPPLEMENTAL_BOARDS:
        if sid not in seen:
            boards.append((sid, name))
            seen.add(sid)
    return boards


# --- Document center search ---

def search_docs(dept_id, doc_type, year, page=1):
    """
    Fetch one page of search results from the eGov Document Center.
    Returns (rows: list[dict], total: int).
    Each row dict: {doc_id, title, posted_date, ext}.
    """
    params = {
        "eGov_searchDepartment": dept_id,
        "eGov_searchType": doc_type,
        "eGov_searchYear": year,
    }
    url = DOC_CENTER + "?" + urllib.parse.urlencode(params)
    if page > 1:
        url += f"&app=4&sect=content&page=4_{page}"

    html = fetch_html(url)
    if not html:
        return [], 0

    # Parse total
    total_match = _TOTAL_RE.search(html)
    total = int(total_match.group(1)) if total_match else 0

    rows = []
    for row_m in _ROW_RE.finditer(html):
        row_html = row_m.group(1)

        id_m = _ROW_ID_RE.search(row_html)
        if not id_m:
            continue
        doc_id = int(id_m.group(1))

        title_m = _ROW_TITLE_RE.search(row_html)
        title = title_m.group(1).strip() if title_m else ""
        # Unescape HTML entities in title
        title = title.replace("&amp;", "&").replace("&lt;", "<").replace("&gt;", ">")

        date_m = _ROW_DATE_RE.search(row_html)
        posted_date = None
        if date_m:
            mm, dd, yyyy = int(date_m.group(1)[:2]), int(date_m.group(1)[3:5]), int(date_m.group(1)[6:])
            try:
                posted_date = datetime.date(yyyy, mm, dd)
            except ValueError:
                pass

        ext_m = _ROW_EXT_RE.search(row_html)
        ext = ext_m.group(1).lower() if ext_m else "pdf"

        rows.append({
            "doc_id": doc_id,
            "title": title,
            "posted_date": posted_date,
            "ext": ext,
        })

    return rows, total


def get_all_docs_for_board_type(dept_id, doc_type, year):
    """
    Fetch all pages of results for a given department + doc_type + year.
    Returns list of row dicts.
    """
    all_rows = []
    page = 1
    while True:
        rows, total = search_docs(dept_id, doc_type, year, page)
        all_rows.extend(rows)
        if not rows or len(all_rows) >= total:
            break
        page += 1
        time.sleep(PAGE_DELAY)
    return all_rows


# --- Date parsing ---

def parse_meeting_date(title):
    """
    Extract meeting date from a document title.
    Looks for YYYY-MM-DD anywhere in the title string.
    Returns datetime.date or None.
    """
    for m in _TITLE_DATE_RE.finditer(title):
        try:
            return datetime.date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
        except ValueError:
            continue
    return None


# --- File naming ---

def slugify(text, max_len=50):
    text = text.lower().strip()
    text = re.sub(r"[&/\\]", "-", text)
    text = re.sub(r"\s+-\s+", "-", text)
    text = re.sub(r"[^\w\s-]", "", text)
    text = re.sub(r"[\s_]+", "-", text)
    text = re.sub(r"-{2,}", "-", text)
    return text.strip("-")[:max_len]


def ext_from_content_type(ct):
    ct = ct.lower()
    if "pdf" in ct:
        return "pdf"
    if "wordprocessingml" in ct or "docx" in ct:
        return "docx"
    if "msword" in ct or "doc" in ct:
        return "doc"
    return None


def make_dest_path(doc, output_dir):
    meeting_date = doc["meeting_date"]
    posted_date = doc["posted_date"]

    date_for_path = meeting_date or posted_date
    if date_for_path:
        month_dir = os.path.join(output_dir, date_for_path.strftime("%Y-%m"))
        date_prefix = date_for_path.strftime("%Y-%m-%d")
    else:
        month_dir = os.path.join(output_dir, "undated")
        date_prefix = "undated"

    os.makedirs(month_dir, exist_ok=True)
    board_slug = slugify(doc["board_name"])
    doc_type_label = doc["doc_type"]
    doc_id = doc["doc_id"]
    ext = doc["ext"]
    return os.path.join(month_dir, f"{date_prefix}_{board_slug}_{doc_type_label}_{doc_id}.{ext}")


# --- Main ---

def main():
    parser = argparse.ArgumentParser(
        description="Download Berlin, CT municipal agendas and minutes.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--days", type=int, default=DAYS_BACK, metavar="N",
        help=f"Look back N days from today (default: {DAYS_BACK})",
    )
    parser.add_argument(
        "--ahead", type=int, default=DAYS_AHEAD, metavar="N",
        help=f"Also include meetings up to N days ahead (default: {DAYS_AHEAD})",
    )
    parser.add_argument(
        "--all", action="store_true",
        help=f"Download all available documents (from {MIN_YEAR} onward), ignoring date window",
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
        "--no-video", action="store_true",
        help="Skip YouTube channel video downloads",
    )
    args = parser.parse_args()

    now = datetime.datetime.now()
    if (now.weekday() == 5 and now.hour >= 18) or (now.weekday() == 6 and now.hour < 12):  # Saturday night, Sunday morning
        print("Skipping — no downloads on Saturday nights or Sunday mornings.")
        sys.exit(0)

    today = datetime.date.today()
    if args.all:
        cutoff = datetime.date(MIN_YEAR, 1, 1)
        future_limit = today + datetime.timedelta(days=DAYS_AHEAD)
    else:
        cutoff = today - datetime.timedelta(days=args.days)
        future_limit = today + datetime.timedelta(days=args.ahead)

    target_years = list(range(max(MIN_YEAR, cutoff.year), future_limit.year + 1))

    print(f"Date window : {cutoff} to {future_limit}")
    print(f"Years queried: {target_years}")
    print(f"Boards index: {BOARDS_INDEX_URL}")
    if not args.dry_run:
        print(f"Output dir  : {args.output_dir}")
    print()

    # --- Step 1: discover boards ---
    print("Fetching boards index...")
    index_html = fetch_html(BOARDS_INDEX_URL)
    if not index_html:
        print("ERROR: Could not fetch the boards index page.", file=sys.stderr)
        sys.exit(1)

    boards = discover_boards(index_html)
    if not boards:
        print("WARNING: No boards found — page structure may have changed.", file=sys.stderr)
        sys.exit(1)

    if args.board:
        filter_str = args.board.lower()
        boards = [(sid, name) for sid, name in boards if filter_str in name.lower()]
        if not boards:
            print(f"No boards match --board '{args.board}'", file=sys.stderr)
            sys.exit(1)

    print(f"  Found {len(boards)} board(s).")
    print()

    # --- Step 2: search document center for each board ---
    all_docs = []
    seen_ids = set()

    doc_types_to_fetch = {}
    if not args.no_agendas:
        doc_types_to_fetch[11] = "agenda"
    if not args.no_minutes:
        doc_types_to_fetch[12] = "minutes"

    print(f"Searching document center ({len(boards)} boards × {len(doc_types_to_fetch)} type(s) × {len(target_years)} year(s))...")
    for board_idx, (dept_id, board_name) in enumerate(boards, 1):
        board_docs = []

        for type_id, type_label in doc_types_to_fetch.items():
            for year in target_years:
                rows = get_all_docs_for_board_type(dept_id, type_id, year)
                time.sleep(PAGE_DELAY)

                for row in rows:
                    doc_id = row["doc_id"]
                    if doc_id in seen_ids:
                        continue

                    meeting_date = parse_meeting_date(row["title"])
                    posted_date = row["posted_date"]

                    # Determine the date we use for window filtering
                    filter_date = meeting_date or posted_date

                    if filter_date:
                        if not (cutoff <= filter_date <= future_limit):
                            continue
                    else:
                        # No parseable date — include if the queried year is in window
                        if year < cutoff.year or year > future_limit.year:
                            continue

                    seen_ids.add(doc_id)
                    board_docs.append({
                        "doc_id": doc_id,
                        "title": row["title"],
                        "meeting_date": meeting_date,
                        "posted_date": posted_date,
                        "ext": row["ext"],
                        "doc_type": type_label,
                        "board_name": board_name,
                        "dept_id": dept_id,
                    })

        if board_docs:
            board_docs.sort(
                key=lambda x: x["meeting_date"] or x["posted_date"] or datetime.date(1900, 1, 1),
                reverse=True,
            )
            print(f"  [{board_idx:>2}/{len(boards)}] {board_name}: {len(board_docs)} doc(s)")
            all_docs.extend(board_docs)
        else:
            print(f"  [{board_idx:>2}/{len(boards)}] {board_name}: 0 doc(s)")

    all_docs.sort(
        key=lambda x: (
            x["meeting_date"] or x["posted_date"] or datetime.date(1900, 1, 1),
            x["board_name"],
        ),
        reverse=True,
    )

    print()
    boards_with_docs = len({d["board_name"] for d in all_docs})
    print(f"Found {len(all_docs)} document(s) across {boards_with_docs} board(s).")
    print()

    if not all_docs:
        print("No documents found within the date window.")
        if not args.all:
            print(f"Try a wider window: --days {args.days * 6} or --all")
        sys.exit(0)

    if args.dry_run:
        print(f"{'Board':<40} {'Meeting date':<13} {'Posted':<11} {'Type':<8} Title")
        print("-" * 100)
        for doc in all_docs:
            dt = str(doc["meeting_date"]) if doc["meeting_date"] else "?"
            posted = str(doc["posted_date"]) if doc["posted_date"] else "?"
            print(f"{doc['board_name'][:39]:<40} {dt:<13} {posted:<11} {doc['doc_type']:<8} {doc['title'][:50]}")
        print(f"\n{len(all_docs)} document(s).")

        if not args.no_video:
            print(f"\nYouTube channel videos (uploaded since {cutoff}):")
            video_lines = download_channel_videos(YOUTUBE_CHANNEL, args.output_dir, cutoff, dry_run=True)
            if video_lines:
                for line in video_lines:
                    print(f"  {line}")
            else:
                print("  (none found or yt-dlp not available)")

        print("\nRe-run without --dry-run to download.")
        return

    # --- Step 3: download ---
    os.makedirs(args.output_dir, exist_ok=True)
    log_path = os.path.join(args.output_dir, "download-log.txt")
    log_lines = []
    downloaded = skipped = failed = 0

    for doc in all_docs:
        dest = make_dest_path(doc, args.output_dir)

        if os.path.exists(dest):
            print(f"  skip (exists)  {os.path.basename(dest)}")
            skipped += 1
            continue

        dt = str(doc["meeting_date"] or doc["posted_date"] or "?")
        print(f"  [{dt}] {doc['board_name']} — {doc['doc_type']}")
        print(f"  downloading    {os.path.basename(dest)}")

        download_url = f"{DOC_CENTER}?view=item&id={doc['doc_id']}"
        ok, content_type = download_file(download_url, dest)

        if ok:
            # Rename if content-type reveals a different extension
            if content_type:
                actual_ext = ext_from_content_type(content_type)
                if actual_ext and not dest.endswith(f".{actual_ext}"):
                    new_dest = re.sub(r'\.[a-z]+$', f'.{actual_ext}', dest)
                    os.rename(dest, new_dest)
                    dest = new_dest
            downloaded += 1
            log_lines.append(f"{datetime.datetime.now().isoformat()}  OK       {dest}")
        else:
            failed += 1
            log_lines.append(f"{datetime.datetime.now().isoformat()}  FAILED   {download_url}")
            if os.path.exists(dest):
                os.remove(dest)

        time.sleep(DOWNLOAD_DELAY)

    if log_lines:
        with open(log_path, "a") as f:
            f.write("\n".join(log_lines) + "\n")

    # --- Step 4: download YouTube channel videos ---
    if not args.no_video:
        print()
        print(f"Downloading YouTube channel videos (since {cutoff})...")
        print(f"  Channel: {YOUTUBE_CHANNEL}")
        print(f"  Output:  {os.path.join(args.output_dir, 'videos')}/")
        v_dl, v_skip, v_fail = download_channel_videos(YOUTUBE_CHANNEL, args.output_dir, cutoff)
        if v_fail:
            print("  WARNING: one or more video downloads failed", file=sys.stderr)

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
# 1. Preview without downloading (past 30 days):
#    python3 scripts/download-berlin-agendas.py --dry-run
#
# 2. Extend the lookback window (e.g., 1 year):
#    python3 scripts/download-berlin-agendas.py --days 365 --dry-run
#
# 3. Download everything from 2020 onward:
#    python3 scripts/download-berlin-agendas.py --all --dry-run
#
# 4. Filter to a specific board:
#    python3 scripts/download-berlin-agendas.py --board "planning and zoning" --dry-run
#    python3 scripts/download-berlin-agendas.py --board "town council" --dry-run
#
# 5. Agendas only (skip minutes and video):
#    python3 scripts/download-berlin-agendas.py --no-minutes --no-video
#
# 6. Documents only (skip YouTube channel videos):
#    python3 scripts/download-berlin-agendas.py --no-video
#
# 7. Save files to a custom directory:
#    python3 scripts/download-berlin-agendas.py --output-dir ~/Downloads/berlin-meetings
#
# 8. Run on a schedule (cron — 8 AM daily):
#    0 8 * * * cd /path/to/repo && python3 scripts/download-berlin-agendas.py
#
# 9. Process downloaded files with Claude afterward:
#    python3 scripts/download-berlin-agendas.py --no-video && bash scripts/batch-process.sh beat-archive/berlin-agendas/
#
# SITE NOTES:
#   - berlinct.gov uses the eGov CMS.
#   - The site requires a browser-like User-Agent; plain urllib with UA works.
#   - The Document Center search supports filtering by department, type, and year.
#   - Pagination uses GET params: &app=4&sect=content&page=4_{N} for page N.
#   - The "view=item&id=N" URL directly serves the file (PDF, DOCX, or DOC).
#   - The date column in search results is the upload/posting date, not the
#     meeting date. Meeting dates appear in document titles as YYYY-MM-DD.
#   - Data is available from 2020 onward; most boards have gaps in 2022-2023.
#   - Town Council (dept 115) and Police Commission (dept 130) are not in the
#     boards index at structureid=97; they are added via SUPPLEMENTAL_BOARDS.
#   - Minutes for some boards are also on Dropbox (town-managed shared folder);
#     those cannot be scraped automatically and require manual download.
#   - Meeting videos are on YouTube: https://www.youtube.com/channel/UC5AtkQIY7aNVJEd6S4XWhzA
#
# BOARDS (~34 as of 2026):
#   Aquifer Protection Agency, Affordable Housing Plan Advisory Committee,
#   Berlin-Peck Memorial Library Board, BHS Advisory Committee,
#   Board of Assessment Appeals, Board of Education, Board of Ethics,
#   Board of Finance, Cemetery Committee, Charter Revision Commission (2016 & 2022),
#   Commission for Persons with Disabilities, Commission for the Aging,
#   Community/Senior Center Advisory Committee, Conservation Commission,
#   Economic Development Commission, Environmental Protection Advisory Commission,
#   Golf Course Commission, Historic District Commission, Housing Authority Commission,
#   Inland Wetlands & Water Courses Commission, Parks and Recreation Commission,
#   Planning and Zoning Commission, Police Commission, Public Building Commission,
#   Town Council, Veterans' Commission, Visiting Nurses Association Board,
#   Water Control Commission, Youth Services Advisory Board, Zoning Board of Appeals,
#   Plan of Conservation and Development Implementation Committee
