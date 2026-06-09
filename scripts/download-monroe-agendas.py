#!/usr/bin/env python3
# download-monroe-agendas.py
# Download Monroe CT municipal agendas, minutes, meeting packets, voting records,
# and recordings for meetings held in the past N days.
#
# USAGE:
#   python3 scripts/download-monroe-agendas.py [options]
#
# REQUIREMENTS:
#   - Python 3.8+
#   - No third-party libraries needed (uses only stdlib)
#
# WHAT IT DOES:
#   1. Fetches https://www.monroect.gov/p/-agendas-voting-records-minutes-videos-recordings
#      to discover all 47 boards (UUIDs embedded in the static HTML)
#   2. For each board, calls GET /Home/Documents?dirId=UUID&mainDirId=MAIN_UUID to
#      enumerate document-type sub-folders (Agendas, Minutes, Meeting Packets, etc.)
#   3. For each doc-type, calls the same API to get year sub-folders
#   4. For each year that overlaps the date window, fetches the file list
#   5. Parses the YYYY-MM-DD date embedded at the start of every file title
#   6. Downloads matching PDFs to beat-archive/monroe-agendas/YYYY-MM/
#   7. Appends a download log to beat-archive/monroe-agendas/download-log.txt
#
# SITE STRUCTURE (QScend CMS):
#   Hub:      https://www.monroect.gov/p/-agendas-voting-records-minutes-videos-recordings
#   Folder API: GET /Home/Documents?dirId=<UUID>&mainDirId=<MAIN_UUID>
#   Download:  GET /Home/DownloadDocument?docId=<UUID>
#
# FOLDER HIERARCHY (consistent across all 47 boards):
#   Board (UUID from hub HTML)
#   └── DocType sub-folder (Agendas | Minutes | Meeting Packets | Voting Records)
#       └── Year sub-folder (2026 | 2025 | ...)
#           └── Files  →  titles start with YYYY-MM-DD
#
# RECORDINGS:
#   The "Videos & Recording" top-level folder contains IWC, ZBA, and P&Z
#   recordings from 2019–2022 stored as PDF link documents. Current recordings
#   (2023+) are not stored in the document system; the /p/recordings page directs
#   users to contact individual boards. Recording PDFs are downloaded just like
#   any other document.
#
# NOTES:
#   - No bot protection; plain urllib works.
#   - No authentication or CSRF tokens needed.
#   - All file titles begin with YYYY-MM-DD, enabling exact date filtering.
#   - No AJAX or JavaScript execution needed; all data is in plain GET responses.

import argparse
import datetime
import html as html_module
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

BASE_URL = "https://www.monroect.gov"
HUB_URL = (
    f"{BASE_URL}/p/-agendas-voting-records-minutes-videos-recordings"
)
DOC_API = f"{BASE_URL}/Home/Documents"
DOWNLOAD_BASE = f"{BASE_URL}/Home/DownloadDocument"
MAIN_DIR_ID = "366a0304-55a2-46a2-980b-4e7de065f8b7"
OUTPUT_DIR = "beat-archive/monroe-agendas"
DAYS_BACK = 4
DELAY_SECONDS = 0.3

UA = (
    "Mozilla/5.0 (X11; Linux x86_64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)

# The "Videos & Recording" top-level folder UUID (not listed with boards
# in the regular board list but processed as part of the full traversal)
VIDEOS_DIR_ID = "ef169e53-4456-4162-bffb-895926e18a70"
VIDEOS_DIR_NAME = "Videos & Recording"

# Regex to find a date at the start of a document title.
# Supports YYYY-MM-DD, YYYY MM DD, and MM DD YYYY (/ or - or space separator).
_DATE_YMD = re.compile(r"^(\d{4})([-\s])(\d{2})\2(\d{2})")   # YYYY?MM?DD
_DATE_MDY = re.compile(r"^(\d{2})([-/\s])(\d{2})\2(\d{4})")  # MM?DD?YYYY

# Regex to find UUID-based sub-folder entries in API HTML
_DIR_RE = re.compile(
    r'data-directory="([a-f0-9-]{36})"[^>]*><button[^>]*><i[^>]*></i>([^<]+)</button>',
    re.IGNORECASE,
)
# Regex to find file entries
_FILE_RE = re.compile(
    r'<a href="/Home/DownloadDocument\?docId=([a-f0-9-]{36})"[^>]*class="file-[^"]*">([^<]+)</a>',
    re.IGNORECASE,
)
# Regex to find all top-level board folders from the hub page
_BOARD_RE = re.compile(
    r'data-directory="([a-f0-9-]{36})"[^>]*><button[^>]*><i[^>]*></i>([^<]+)</button>',
    re.IGNORECASE,
)


def slugify(text):
    text = html_module.unescape(text).lower().strip()
    text = re.sub(r"[/\\&]", "-", text)
    text = re.sub(r"[^\w\s-]", "", text)
    text = re.sub(r"[\s_]+", "-", text)
    text = re.sub(r"-{2,}", "-", text)
    return text.strip("-")[:60]


def fetch(url):
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            charset = r.headers.get_content_charset() or "utf-8"
            return r.read().decode(charset, "replace")
    except urllib.error.HTTPError as e:
        print(f"  HTTP {e.code} — {url}", file=sys.stderr)
        return None
    except urllib.error.URLError as e:
        print(f"  ERROR: {e} — {url}", file=sys.stderr)
        return None


def fetch_dir(dir_id):
    """Fetch a folder's content from the Documents API."""
    url = f"{DOC_API}?dirId={urllib.parse.quote(dir_id)}&mainDirId={urllib.parse.quote(MAIN_DIR_ID)}"
    return fetch(url)


def parse_sub_folders(html):
    """Return [(uuid, label), ...] from a Documents API response."""
    if not html:
        return []
    return [
        (uuid, html_module.unescape(label.strip()))
        for uuid, label in _DIR_RE.findall(html)
        if uuid != MAIN_DIR_ID  # exclude breadcrumb entries
    ]


def parse_files(html):
    """Return [(doc_id, title), ...] from a Documents API response."""
    if not html:
        return []
    return [
        (doc_id, html_module.unescape(title.strip()))
        for doc_id, title in _FILE_RE.findall(html)
    ]


def parse_date(title):
    """Parse a leading date from a document title. Returns date or None.
    Handles YYYY-MM-DD, YYYY MM DD, and MM DD YYYY."""
    t = title.strip()
    m = _DATE_YMD.match(t)
    if m:
        try:
            return datetime.date(int(m.group(1)), int(m.group(3)), int(m.group(4)))
        except ValueError:
            pass
    m = _DATE_MDY.match(t)
    if m:
        try:
            return datetime.date(int(m.group(4)), int(m.group(1)), int(m.group(3)))
        except ValueError:
            pass
    return None


def download_doc(doc_id, dest_path):
    """Download a document by its docId. Returns True on success."""
    url = f"{DOWNLOAD_BASE}?docId={urllib.parse.quote(doc_id)}"
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            with open(dest_path, "wb") as f:
                f.write(r.read())
        return True
    except Exception as e:
        print(f"  WARNING: {e}", file=sys.stderr)
        return False


def should_skip_folder(label, no_minutes, no_agendas, no_packets, no_voting):
    """Return True if this folder label should be excluded."""
    lc = label.lower()
    if no_minutes and re.search(r"\bminutes\b", lc):
        return True
    if no_agendas and re.search(r"\bagendas?\b", lc):
        return True
    if no_packets and re.search(r"meeting packet", lc):
        return True
    if no_voting and re.search(r"voting record", lc):
        return True
    return False


def main():
    parser = argparse.ArgumentParser(
        description="Download Monroe CT municipal agendas, minutes, meeting "
                    "packets, voting records, and recordings for meetings "
                    "in the past N days."
    )
    parser.add_argument("--days", type=int, default=DAYS_BACK, metavar="N",
                        help=f"Look back N days (default: {DAYS_BACK})")
    parser.add_argument("--output-dir", default=OUTPUT_DIR, metavar="DIR",
                        help=f"Destination directory (default: {OUTPUT_DIR})")
    parser.add_argument("--dry-run", action="store_true",
                        help="List matching documents without downloading")
    parser.add_argument("--board", metavar="NAME",
                        help="Only process boards whose name contains NAME "
                             "(case-insensitive)")
    parser.add_argument("--no-minutes", action="store_true",
                        help="Skip Minutes folders")
    parser.add_argument("--no-agendas", action="store_true",
                        help="Skip Agendas folders")
    parser.add_argument("--no-packets", action="store_true",
                        help="Skip Meeting Packets folders")
    parser.add_argument("--no-voting", action="store_true",
                        help="Skip Voting Records folders")
    parser.add_argument("--no-video", action="store_true",
                        help="Skip the Videos & Recording folder entirely")
    args = parser.parse_args()

    now = datetime.datetime.now()
    if (now.weekday() == 5 and now.hour >= 18) or (now.weekday() == 6 and now.hour < 12):  # Saturday night, Sunday morning
        print("Skipping — no downloads on Saturday nights or Sunday mornings.")
        sys.exit(0)

    today = datetime.date.today()
    cutoff = today - datetime.timedelta(days=args.days)
    years_needed = set(range(cutoff.year, today.year + 1))

    print(f"Date window : {cutoff} to {today}  ({args.days} days back)")
    print(f"Hub URL     : {HUB_URL}")
    print(f"Output dir  : {args.output_dir}")
    print()

    # Step 1: Get board list from hub page
    print("Fetching board list...")
    hub_html = fetch(HUB_URL)
    if not hub_html:
        print("ERROR: Could not load hub page.", file=sys.stderr)
        sys.exit(1)

    boards = _BOARD_RE.findall(hub_html)
    if not boards:
        print("WARNING: No boards found — page structure may have changed.",
              file=sys.stderr)
        sys.exit(1)

    boards = [(uuid, html_module.unescape(label.strip()))
              for uuid, label in boards]

    # Add Videos & Recording folder unless excluded
    if not args.no_video:
        boards.append((VIDEOS_DIR_ID, VIDEOS_DIR_NAME))

    print(f"Discovered {len(boards)} board(s)/folder(s).")

    if args.board:
        filt = args.board.lower()
        boards = [(uid, name) for uid, name in boards if filt in name.lower()]
        print(f"Filtered to {len(boards)} matching '{args.board}'.")

    print()
    candidates = []

    # Step 2: Traverse Board → DocType → Year → Files
    for board_uuid, board_name in boards:
        print(f"  Scanning: {board_name}")

        # Level 1: doc-type sub-folders
        doctype_html = fetch_dir(board_uuid)
        doctype_folders = parse_sub_folders(doctype_html)

        if not doctype_folders:
            # No sub-folders — check if files are directly here (unlikely but safe)
            for doc_id, title in parse_files(doctype_html):
                doc_date = parse_date(title)
                if doc_date and cutoff <= doc_date <= today:
                    candidates.append({
                        "board": board_name,
                        "doctype": "",
                        "year": "",
                        "doc_id": doc_id,
                        "title": title,
                        "date": doc_date,
                    })
            continue

        for doctype_uuid, doctype_label in doctype_folders:
            if should_skip_folder(doctype_label, args.no_minutes, args.no_agendas,
                                  args.no_packets, args.no_voting):
                continue

            # Level 2: year sub-folders
            year_html = fetch_dir(doctype_uuid)
            time.sleep(DELAY_SECONDS)
            year_folders = parse_sub_folders(year_html)

            # Also check for files directly at DocType level (rare)
            for doc_id, title in parse_files(year_html):
                doc_date = parse_date(title)
                if doc_date and cutoff <= doc_date <= today:
                    candidates.append({
                        "board": board_name,
                        "doctype": doctype_label,
                        "year": "",
                        "doc_id": doc_id,
                        "title": title,
                        "date": doc_date,
                    })

            if not year_folders:
                continue

            for year_uuid, year_label in year_folders:
                # Filter by year label
                year_label_clean = year_label.strip()
                if year_label_clean.isdigit():
                    if int(year_label_clean) not in years_needed:
                        continue

                # Level 3: files
                files_html = fetch_dir(year_uuid)
                time.sleep(DELAY_SECONDS)

                for doc_id, title in parse_files(files_html):
                    doc_date = parse_date(title)
                    if doc_date:
                        if cutoff <= doc_date <= today:
                            candidates.append({
                                "board": board_name,
                                "doctype": doctype_label,
                                "year": year_label_clean,
                                "doc_id": doc_id,
                                "title": title,
                                "date": doc_date,
                            })
                    else:
                        # No date in title; include if it's in the current year
                        if year_label_clean == str(today.year):
                            candidates.append({
                                "board": board_name,
                                "doctype": doctype_label,
                                "year": year_label_clean,
                                "doc_id": doc_id,
                                "title": title,
                                "date": None,
                            })

    candidates.sort(
        key=lambda x: (x["date"] or datetime.date.min, x["board"]),
        reverse=True,
    )

    total = len(candidates)
    board_names = sorted({c["board"] for c in candidates})
    print(f"\nDocuments in window : {total}")
    if board_names:
        print(f"Boards with matches : {len(board_names)}")
    print()

    if not candidates:
        print("No documents found within the date window.")
        sys.exit(0)

    if args.dry_run:
        print(f"{'Board':<42} {'Date':<12} {'Type':<18} {'Title'}")
        print("-" * 110)
        for c in candidates:
            date_s = c["date"].isoformat() if c["date"] else "unknown"
            dtype = c["doctype"][:17] if c["doctype"] else "-"
            print(
                f"{c['board'][:41]:<42} {date_s:<12} {dtype:<18} "
                f"{c['title'][:40]}"
            )
        print(f"\n{total} document(s). Re-run without --dry-run to download.")
        return

    os.makedirs(args.output_dir, exist_ok=True)
    log_path = os.path.join(args.output_dir, "download-log.txt")
    log_lines = []
    dl_ok = dl_skip = dl_fail = 0

    for c in candidates:
        ref_date = c["date"] or today
        month_dir = os.path.join(args.output_dir, ref_date.strftime("%Y-%m"))
        os.makedirs(month_dir, exist_ok=True)

        board_slug = slugify(c["board"])
        doctype_slug = slugify(c["doctype"]) if c["doctype"] else "doc"
        # Build filename from date + board + doctype + title (after stripping leading date)
        title_clean = re.sub(r"^\d{4}[-\s]\d{2}[-\s]\d{2}[_\s]*", "", c["title"]).strip()
        title_slug = slugify(title_clean)[:60]
        date_prefix = ref_date.isoformat()
        dest = os.path.join(
            month_dir,
            f"{date_prefix}-{board_slug}-{doctype_slug}-{title_slug}.pdf",
        )

        if os.path.exists(dest):
            print(f"  skip (exists)  {os.path.basename(dest)}")
            dl_skip += 1
            continue

        print(f"  [{c['board']}] {c['title'][:60]}")
        print(f"  downloading    {os.path.basename(dest)}")

        ok = download_doc(c["doc_id"], dest)
        time.sleep(DELAY_SECONDS)

        if ok:
            dl_ok += 1
            log_lines.append(
                f"{datetime.datetime.now().isoformat()}  OK      {dest}")
        else:
            dl_fail += 1
            log_lines.append(
                f"{datetime.datetime.now().isoformat()}  FAIL    "
                f"{DOWNLOAD_BASE}?docId={c['doc_id']}"
            )
            if os.path.exists(dest):
                os.remove(dest)

    if log_lines:
        with open(log_path, "a") as f:
            f.write("\n".join(log_lines) + "\n")

    print()
    print(f"Downloaded: {dl_ok}  Skipped: {dl_skip}  Failed: {dl_fail}")
    if dl_ok + dl_skip:
        print(f"Files in: {args.output_dir}")
    if log_lines:
        print(f"Log:      {log_path}")


if __name__ == "__main__":
    main()


# --- Tips ---
#
# 1. Preview without downloading:
#    python3 scripts/download-monroe-agendas.py --dry-run
#
# 2. Narrow to one board:
#    python3 scripts/download-monroe-agendas.py --board "Town Council"
#
# 3. Change the lookback window:
#    python3 scripts/download-monroe-agendas.py --days 60
#
# 4. Save files somewhere else:
#    python3 scripts/download-monroe-agendas.py --output-dir ~/Downloads/monroe
#
# 5. Agendas only:
#    python3 scripts/download-monroe-agendas.py --no-minutes --no-packets --no-voting
#
# 6. Minutes only:
#    python3 scripts/download-monroe-agendas.py --no-agendas --no-packets --no-voting
#
# 7. Skip recording PDFs:
#    python3 scripts/download-monroe-agendas.py --no-video
#
# 8. Run on a schedule (cron — 7 AM daily):
#    0 7 * * * cd /path/to/repo && python3 scripts/download-monroe-agendas.py
#
# SITE NOTES:
#   - No bot protection; plain urllib works.
#   - Documents API: GET /Home/Documents?dirId=UUID&mainDirId=MAIN_UUID
#   - Download: GET /Home/DownloadDocument?docId=UUID
#   - All file titles begin with YYYY-MM-DD (the meeting date), not the upload date.
#   - The "Videos & Recording" folder contains IWC, ZBA, and P&Z recordings
#     from 2019–2022 as PDF documents. More recent recordings (2023+) are not
#     stored in the documents system.
