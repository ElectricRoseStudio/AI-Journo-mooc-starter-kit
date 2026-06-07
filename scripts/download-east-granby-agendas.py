#!/usr/bin/env python3
# download-east-granby-agendas.py
# Download municipal meeting agendas and minutes from East Granby CT
# for meetings whose date falls within the past N days (and up to 7 days ahead).
#
# USAGE:
#   python3 scripts/download-east-granby-agendas.py [options]
#
# REQUIREMENTS:
#   - Python 3.6+  (no third-party packages needed)
#   - Internet connection
#
# WHAT IT DOES:
#   1. For each of 12 boards, checks the current and prior year's Minutes and
#      Agendas subfolders in the ee-simple-file-list-pro WordPress plugin
#   2. Parses the date from each filename (MM-DD-YY prefix)
#   3. Downloads PDFs whose dates fall within the configured date window
#   4. Appends a download log to beat-archive/east-granby-agendas/download-log.txt
#
# SITE STRUCTURE:
#   CMS: WordPress with ee-simple-file-list-pro plugin
#   Site: https://eastgranbyct.org
#
#   Folder listing URL:
#     https://eastgranbyct.org/{slug}/?eeFront=1&ee=1&eeFolder={encoded_path}&eeListID=1
#
#   Document URL pattern:
#     https://eastgranbyct.org/docs/{board-folder}/{year}/{Minutes|Agendas}/{filename}
#
#   Filename date prefix: MM-DD-YY  (e.g. "04-22-26-BOS-Minutes.pdf")
#
#   Boards with different or missing structures (not included):
#     - Building Board of Appeals: no document plugin on page
#     - Committees: organized by committee name, not year; mostly pre-2017 docs
#
# BOARDS (12):
#   Board of Assessment Appeals, Board of Education, Board of Finance,
#   Board of Selectmen, Commission on Aging,
#   Economic Development Commission,
#   Inland Wetlands & Conservation Commission,
#   Parks & Recreation Commission, Planning & Zoning Commission,
#   Water Pollution Control Authority, Youth Services Commission,
#   Zoning Board of Appeals

import argparse
import datetime
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

# --- Configuration ---
BASE_URL = "https://eastgranbyct.org"
OUTPUT_DIR = "beat-archive/east-granby-agendas"
DAYS_BACK = 4
DAYS_AHEAD = 7
DELAY_SECONDS = 0.5

UA = "Mozilla/5.0 (X11; Linux x86_64; rv:120.0) Gecko/20100101 Firefox/120.0"

# Each tuple: (display_name, wordpress_slug, docs_folder_name)
BOARDS = [
    ("Board of Selectmen",                   "board-of-selectmen",               "Board-of-Selectmen"),
    ("Board of Finance",                     "board-of-finance",                 "Board-of-Finance"),
    ("Board of Education",                   "board-of-education",               "Board-of-Education_1"),
    ("Board of Assessment Appeals",          "board-of-assessment-appeals",      "Board-of-Assessment-Appeals"),
    ("Planning & Zoning Commission",         "planning-zoning-commission",       "Planning-Zoning-Commission"),
    ("Zoning Board of Appeals",              "zoning-board-of-appeals",          "Zoning-Board-of-Appeals"),
    ("Inland Wetlands & Conservation Comm.", "inlands-wetlands-commission",      "Conservation-Commission"),
    ("Commission on Aging",                  "commission-on-aging",              "Commission-on-Aging"),
    ("Parks & Recreation Commission",        "parks-recreation-commission",      "Parks-and-Recreation"),
    ("Economic Development Commission",      "economic-development",             "Economic-Development-Commission"),
    ("Water Pollution Control Authority",    "water-pollution-control-authority","WPCA"),
    ("Youth Services Commission",            "youth-services",                   "Youth-Services"),
]

# Filename date prefixes: "MM-DD-YY-" (2-digit year) and "MM-DD-YYYY-" (4-digit year)
_FNAME_DATE2_RE = re.compile(r"^(\d{2})-(\d{2})-(\d{2})-")
_FNAME_DATE4_RE = re.compile(r"^(\d{2})-(\d{2})-(\d{4})-")


# --- HTTP helpers ---

def fetch_html(url):
    req = urllib.request.Request(
        url, headers={"User-Agent": UA, "Accept": "text/html,*/*"}
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            raw = r.read()
            charset = r.headers.get_content_charset() or "utf-8"
            return raw.decode(charset, errors="replace")
    except urllib.error.HTTPError as e:
        if e.code != 404:
            print(f"  HTTP {e.code} — {url}", file=sys.stderr)
        return None
    except Exception as e:
        print(f"  ERROR: {url}: {e}", file=sys.stderr)
        return None


def download_file(url, dest_path):
    req = urllib.request.Request(
        url, headers={"User-Agent": UA, "Accept": "application/pdf,*/*"}
    )
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            with open(dest_path, "wb") as f:
                f.write(r.read())
        return True
    except Exception as e:
        print(f"  WARNING: {e}", file=sys.stderr)
        return False


# --- ee-simple-file-list-pro helpers ---

def folder_listing_url(slug, folder_path):
    return (
        f"{BASE_URL}/{slug}/"
        f"?eeFront=1&ee=1&eeFolder={urllib.parse.quote(folder_path)}&eeListID=1"
    )


def find_subfolders(slug, folder_path, target_names):
    """
    Fetch the eeSFL listing for folder_path and return a dict mapping
    lowercase subfolder name → decoded folder path, limited to target_names.
    """
    html = fetch_html(folder_listing_url(slug, folder_path))
    if not html:
        return {}

    raw_folders = re.findall(r"eeFolder=([^&\"&#;]+)", html)
    result = {}
    for rf in raw_folders:
        decoded = urllib.parse.unquote(rf)
        if decoded == folder_path:
            continue
        sf_name = decoded.rsplit("/", 1)[-1].lower()
        if sf_name in target_names and sf_name not in result:
            result[sf_name] = decoded
    return result


def list_docs_in_folder(slug, folder_path, docs_folder):
    """
    Fetch the eeSFL listing for folder_path and return deduplicated document
    URLs that belong to docs_folder (filters out sidebar/header links).
    """
    html = fetch_html(folder_listing_url(slug, folder_path))
    if not html:
        return []

    expected_prefix = f"{BASE_URL}/docs/{docs_folder}/"
    all_links = re.findall(r'href="(https://eastgranbyct\.org/docs/[^"]+?)"', html)
    seen = set()
    result = []
    for link in all_links:
        if link.startswith(expected_prefix) and link not in seen:
            seen.add(link)
            result.append(link)
    return result


# --- Date and path utilities ---

def parse_filename_date(filename):
    """Parse MM-DD-YY or MM-DD-YYYY date prefix from a filename."""
    m4 = _FNAME_DATE4_RE.match(filename)
    if m4:
        mm, dd, yyyy = m4.groups()
        try:
            return datetime.date(int(yyyy), int(mm), int(dd))
        except ValueError:
            return None

    m2 = _FNAME_DATE2_RE.match(filename)
    if m2:
        mm, dd, yy = m2.groups()
        year = 2000 + int(yy)
        try:
            return datetime.date(year, int(mm), int(dd))
        except ValueError:
            return None

    return None


def slugify(text, max_len=55):
    text = str(text).lower().strip()
    text = re.sub(r"[/\\&]", "-", text)
    text = re.sub(r"[^\w\s-]", "", text)
    text = re.sub(r"[\s_]+", "-", text)
    text = re.sub(r"-{2,}", "-", text)
    return text.strip("-")[:max_len]


def make_doc_path(board, date, doc_type, src_filename, output_dir):
    """Return the local file path for a downloaded document."""
    month_dir = os.path.join(output_dir, date.strftime("%Y-%m"))
    os.makedirs(month_dir, exist_ok=True)
    board_slug = slugify(board, max_len=35)
    date_str = date.strftime("%Y-%m-%d")
    # Strip the leading date prefix from src_filename to avoid redundancy
    clean = re.sub(r"^\d{2}-\d{2}-\d{2,4}-", "", src_filename)
    clean = re.sub(r"\.pdf$", "", clean, flags=re.IGNORECASE)
    clean = slugify(clean, max_len=30)
    return os.path.join(month_dir, f"{date_str}-{board_slug}-{doc_type}-{clean}.pdf")


# --- Document collection ---

def collect_board_docs(board_name, slug, docs_folder, years, cutoff, future_limit):
    """
    For a board, check each year's Minutes and Agendas subfolders and return
    items within the date window.
    """
    items = []
    for year in years:
        year_path = f"{docs_folder}/{year}"
        subfolders = find_subfolders(slug, year_path, {"minutes", "agendas"})
        time.sleep(DELAY_SECONDS)

        for sf_name, sf_path in subfolders.items():
            doc_type = "minutes" if sf_name == "minutes" else "agenda"
            doc_links = list_docs_in_folder(slug, sf_path, docs_folder)
            time.sleep(DELAY_SECONDS)

            for url in doc_links:
                filename = url.rsplit("/", 1)[-1]
                date = parse_filename_date(filename)
                if date and cutoff <= date <= future_limit:
                    items.append({
                        "board": board_name,
                        "date": date,
                        "doc_type": doc_type,
                        "url": url,
                        "filename": filename,
                    })
    return items


# --- Main ---

def main():
    parser = argparse.ArgumentParser(
        description=(
            "Download East Granby CT municipal agendas and minutes "
            "for meetings within the past N days (and up to 7 ahead)."
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
        help="List matching items without downloading",
    )
    parser.add_argument(
        "--board", metavar="NAME",
        help="Only include boards whose name contains NAME (case-insensitive)",
    )
    parser.add_argument(
        "--no-minutes", action="store_true",
        help="Skip minutes, download agendas only",
    )
    parser.add_argument(
        "--no-agendas", action="store_true",
        help="Skip agendas, download minutes only",
    )
    args = parser.parse_args()

    if datetime.date.today().weekday() in (6, 0):  # Sunday, Monday
        print("Skipping — no downloads on Sunday or Monday.")
        sys.exit(0)

    today = datetime.date.today()
    cutoff = today - datetime.timedelta(days=args.days)
    future_limit = today + datetime.timedelta(days=args.ahead)
    board_filter = args.board.lower() if args.board else None

    # Determine which years the date window spans
    years = sorted({y for y in range(cutoff.year, future_limit.year + 1)})

    print(f"Date window : {cutoff} to {future_limit}")
    print(f"Years       : {', '.join(str(y) for y in years)}")
    print(f"Site        : {BASE_URL}")
    if not args.dry_run:
        print(f"Output dir  : {args.output_dir}")
    print()

    boards_to_check = [
        (name, slug, folder)
        for name, slug, folder in BOARDS
        if board_filter is None or board_filter in name.lower()
    ]

    # --- Collect matching documents ---
    all_docs = []
    print(f"Scanning {len(boards_to_check)} board(s) for documents...")
    for name, slug, folder in boards_to_check:
        items = collect_board_docs(name, slug, folder, years, cutoff, future_limit)
        if args.no_minutes:
            items = [i for i in items if i["doc_type"] != "minutes"]
        if args.no_agendas:
            items = [i for i in items if i["doc_type"] != "agenda"]
        if items:
            print(f"  {name}: {len(items)} item(s)")
        all_docs.extend(items)

    print()

    if not all_docs:
        print("No items found in the date window.")
        return

    # --- Dry-run listing ---
    if args.dry_run:
        print(f"{'Board':<42} {'Date':<12} {'Type':<8} Filename")
        print("-" * 85)
        for d in sorted(all_docs, key=lambda x: (x["date"], x["board"])):
            print(
                f"{d['board'][:41]:<42} "
                f"{d['date']!s:<12} "
                f"{d['doc_type']:<8} "
                f"{d['filename']}"
            )
        print()
        print(f"{len(all_docs)} item(s) matched. Re-run without --dry-run to download.")
        return

    # --- Download PDFs ---
    os.makedirs(args.output_dir, exist_ok=True)
    log_path = os.path.join(args.output_dir, "download-log.txt")
    log_lines = []
    downloaded = skipped = failed = 0

    print(f"Downloading {len(all_docs)} document(s)...")
    for d in sorted(all_docs, key=lambda x: (x["date"], x["board"])):
        dest = make_doc_path(
            d["board"], d["date"], d["doc_type"], d["filename"], args.output_dir
        )
        label = os.path.basename(dest)

        if os.path.exists(dest):
            print(f"  skip (exists)  {label}")
            skipped += 1
            continue

        print(f"  [{d['date']}] {d['board'][:45]} — {d['doc_type']}")
        print(f"  downloading    {label}")

        if download_file(d["url"], dest):
            downloaded += 1
            log_lines.append(
                f"{datetime.datetime.now().isoformat()}  OK       {dest}"
            )
        else:
            failed += 1
            log_lines.append(
                f"{datetime.datetime.now().isoformat()}  FAILED   {d['url']}"
            )
            if os.path.exists(dest):
                os.remove(dest)
        time.sleep(DELAY_SECONDS)
    print()

    if log_lines:
        with open(log_path, "a") as f:
            f.write("\n".join(log_lines) + "\n")

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
#    python3 scripts/download-east-granby-agendas.py --dry-run
#
# 2. Narrow to one board:
#    python3 scripts/download-east-granby-agendas.py --board "Planning"
#
# 3. Agendas only (skip minutes):
#    python3 scripts/download-east-granby-agendas.py --no-minutes
#
# 4. Change the lookback window:
#    python3 scripts/download-east-granby-agendas.py --days 14
#
# 5. Run on a schedule (cron — 7 AM daily):
#    0 7 * * * cd /path/to/repo && python3 scripts/download-east-granby-agendas.py
#
# NOTES:
#   - East Granby uses a WordPress site (eastgranbyct.org) with the
#     ee-simple-file-list-pro plugin for document management. There is no
#     CivicPlus AgendaCenter or similar portal.
#   - Documents are stored at /docs/{board-folder}/{year}/{Minutes|Agendas}/
#     with filenames prefixed MM-DD-YY (e.g. "04-22-26-BOS-Minutes.pdf").
#   - No meeting recordings are published on the site. The Tri-Town Cable TV
#     committee folder predates 2017 and contains only administrative documents.
#   - The Building Board of Appeals page has no document management plugin and
#     is not included. The Committees page uses a different folder structure
#     (organized by committee name, not year) and is not included.
#   - Each board page query adds 2 HTTP requests per year (year folder + each
#     subfolder). With 12 boards and 1 year, expect ~36 requests total.
