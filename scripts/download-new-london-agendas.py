#!/usr/bin/env python3
# download-new-london-agendas.py
# Download municipal meeting agendas, minutes and audio recordings from the
# New London CT QScend document storage system.
#
# USAGE:
#   python3 scripts/download-new-london-agendas.py [options]
#
# REQUIREMENTS:
#   - Python 3.6+  (no third-party packages needed)
#   - Internet connection
#
# WHAT IT DOES:
#   1. For each board folder in FOLDER_REGISTRY, calls the QScend files API
#   2. Parses the meeting date from each document's title (MM-DD-YYYY format)
#   3. Filters to documents whose date falls within the configured window
#   4. Downloads PDFs and MP3 recordings to beat-archive/new-london-agendas/YYYY-MM/
#   5. Appends a download log to beat-archive/new-london-agendas/download-log.txt
#
# SITE STRUCTURE (QScend):
#   Base:    https://www.newlondonct.gov
#   API:     https://www.newlondonct.gov/controls/api/v1/files/get/?folder={N}
#   Files:   https://www.newlondonct.gov{href}
#
#   The API returns a JSON array. Each object includes at minimum:
#     title:  display/file name used for date parsing (e.g. "01-06-2025 City Council Agenda.1.pdf")
#     href:   relative URL to the PDF file (e.g. "/filestorage/4159/4161/4173/37355/…")
#
# NOTE: Meeting dates are embedded in filenames in MM-DD-YYYY or MM-DD-YY format.
# Documents whose titles don't contain a parseable date are skipped.
#
# NOTE: FOLDER_REGISTRY maps boards and doc-types to known numeric QScend folder IDs.
# The city creates new folders when it starts a new year of records.
# If recent documents are missing, check for new folder IDs and add them here.
# City Council folders 37355 (agendas) and 37507 (minutes) cover through Dec 2024;
# new folders will need to be added when they appear.
#
# NOTE: No authentication required. Standard HTTPS requests work.

import argparse
import datetime
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request

# --- Configuration ---
BASE_URL = "https://www.newlondonct.gov"
API_URL = f"{BASE_URL}/controls/api/v1/files/get/?folder="
OUTPUT_DIR = "beat-archive/new-london-agendas"
DAYS_BACK = 4
DAYS_AHEAD = 7
API_DELAY = 0.5
DOWNLOAD_DELAY = 0.8

UA = "New-London-CT-Agendas-Downloader/1.0 (journalism research)"

# Date patterns tried in order:
#   MM-DD-YYYY  (most common: "01-06-2025 City Council Agenda.1.pdf")
#   MM.DD.YYYY  (Board of Finance: "12.04.2025 Agd.pdf")
#   YYYY-MM-DD  (Historic District Commission: "2025-12-17 HDC Meeting Minutes.pdf")
#   MM-DD-YY    (WWPCA: "12-18-25 Approved WWPCA Meeting Minutes.pdf")
_DATE_RE_4   = re.compile(r"\b(\d{1,2})-(\d{1,2})-(\d{4})\b")
_DATE_RE_DOT = re.compile(r"\b(\d{1,2})\.(\d{1,2})\.(\d{4})\b")
_DATE_RE_ISO = re.compile(r"\b(\d{4})-(\d{2})-(\d{2})\b")
_DATE_RE_2   = re.compile(r"(?<!\d)(\d{1,2})-(\d{1,2})-(\d{2})(?!\d)")

# --- Folder registry ---
# Each entry: (board_name, doc_type, [folder_ids])
# Older-year folders are listed before newer ones within each entry.
# When the city creates new folders for a new record year, add them here.

FOLDER_REGISTRY = [
    # City Council
    ("City Council",                      "agenda",   [33119, 37355, 41840, 44635]),
    ("City Council",                      "minutes",  [33163, 37507, 42073, 44885]),
    # Finance Committee (City Council standing committee)
    ("Finance Committee",                 "agenda",   [33116, 41993, 44639]),
    ("Finance Committee",                 "minutes",  [42212]),
    # Board of Finance
    ("Board of Finance",                  "agenda",   [34498, 37397, 37759, 42130, 42208, 44470, 44708]),
    ("Board of Finance",                  "minutes",  [37447, 42431, 42450, 44366]),
    # Fiscal Review Committee
    ("Fiscal Review Committee",           "agenda",   [35312, 37512]),
    ("Fiscal Review Committee",           "minutes",  [37711]),
    # Planning and Zoning Commission
    ("Planning and Zoning Commission",    "agenda",   [36156, 41596, 44246]),
    ("Planning and Zoning Commission",    "minutes",  [37573, 42042, 44858]),
    # Zoning Board of Appeals
    ("Zoning Board of Appeals",           "agenda",   [36307, 41940, 44769]),
    ("Zoning Board of Appeals",           "minutes",  [37851, 38012, 45436]),
    # Inland Wetlands and Conservation Commission
    # IWCC stores each meeting in its own folder; all known agenda folders listed.
    ("IWCC",                              "agenda",   [33144, 37362, 38328, 40087, 40335,
                                                       40492, 40855, 41505, 41867, 42734,
                                                       42882, 43689, 44040]),
    ("IWCC",                              "minutes",  [40080, 41948]),
    # Harbor Management Commission / Port Authority
    ("Harbor Management Commission",      "agenda",   [33197, 37453, 41907, 44683]),
    ("Harbor Management Commission",      "minutes",  [37792, 41910, 45040]),
    # Parking Authority
    ("Parking Authority",                 "agenda",   [33289, 37473, 41897, 44688, 45873]),
    ("Parking Authority",                 "minutes",  [33365, 37876, 45035, 45645]),
    # Personnel Board
    ("Personnel Board",                   "agenda",   [33372, 37661, 42107, 45086]),
    ("Personnel Board",                   "minutes",  [33408, 37666, 44121, 44950]),
    # Citizens Advisory Committee
    ("Citizens Advisory Committee",       "agenda",   [33157, 37699]),
    ("Citizens Advisory Committee",       "minutes",  [33297]),
    # Historic District Commission
    ("Historic District Commission",      "agenda",   [33575, 33681, 41974, 44868]),
    ("Historic District Commission",      "minutes",  [38570, 45273, 45275]),
    # Sustainable Action Commission
    ("Sustainable Action Commission",     "agenda",   [33399, 37580, 42391]),
    ("Sustainable Action Commission",     "minutes",  [34315, 38251, 43051]),
    # Beautification Committee
    ("Beautification Committee",          "agenda",   [33992, 38110, 42542, 45286]),
    # City Council Development Committee
    ("City Council Development",          "agenda",   [33830]),
    ("City Council Development",          "minutes",  [33833, 34263]),
    # Veterans Advisory Committee
    ("Veterans Advisory Committee",       "agenda",   [33262, 37489, 41980, 44765]),
    # New London Housing Authority
    ("Housing Authority",                 "agenda",   [34127, 37613, 44920]),
    # Ethics Board
    ("Ethics Board",                      "agenda",   [38401, 42422]),
    ("Ethics Board",                      "minutes",  [38424, 42523]),
    # Board of Assessment Appeals
    ("Board of Assessment Appeals",       "agenda",   [34189, 38314, 42490, 45187]),
    ("Board of Assessment Appeals",       "minutes",  [45381]),
    # Public Welfare Committee
    ("Public Welfare Committee",          "agenda",   [37351]),
    ("Public Welfare Committee",          "minutes",  [44888]),
    # Public Safety Committee
    ("Public Safety Committee",           "agenda",   [37505]),
    ("Public Safety Committee",           "minutes",  [42159]),
    # Police Civilian Review Board
    ("Police Civilian Review Board",      "agenda",   [38105, 41962, 44664]),
    ("Police Civilian Review Board",      "minutes",  [44846]),
    # Pension Committee
    ("Pension Committee",                 "agenda",   [39781, 43075]),
    ("Pension Committee",                 "minutes",  [43201]),
    # Water Pollution Control Authority (WWPCA)
    ("WWPCA",                             "agenda",   [33376, 37534, 44750]),
    ("WWPCA",                             "minutes",  [33625, 37538, 45711]),
    # Permanent Revaluation Commission
    ("Permanent Revaluation Commission",  "agenda",   [33472]),
    # Foreign Trade Zone Commission
    ("Foreign Trade Zone",                "agenda",   [33997, 38309, 42924, 45182]),
    # Sustainability Commission
    ("Sustainability Commission",         "agenda",   [33981, 33984]),
    # Cultural District Commission
    ("Cultural District Commission",      "minutes",  [38140, 41904, 44637]),
    # Fair Rent Commission
    ("Fair Rent Commission",              "agenda",   [42114]),
    # Senior Affairs Commission
    ("Senior Affairs Commission",         "agenda",   [44877]),
    # Library Board
    ("Library Board",                     "agenda",   [44479]),

    # --- Audio recordings (MP3) ---
    # City Council: 2024 folder 37516, 2025 folder 42800 (no 2026 folder yet as of May 2026)
    ("City Council",                      "recording", [37516, 42800]),
    # Planning and Zoning Commission: 2024 folder 37549, 2025 folder 42027, 2026 folder 44852
    ("Planning and Zoning Commission",    "recording", [37549, 42027, 44852]),
    # Zoning Board of Appeals: 2024 folder 37853, 2025 folder 42175, 2026 folder 45520
    ("Zoning Board of Appeals",           "recording", [37853, 42175, 45520]),
]


# --- Helpers ---

def parse_date_from_title(title):
    """Extract meeting date from a QScend document title.

    Tries patterns in order: MM-DD-YYYY, MM.DD.YYYY, YYYY-MM-DD, MM-DD-YY.
    Returns None if no parseable date is found.
    """
    m = _DATE_RE_4.search(title)
    if m:
        try:
            return datetime.date(int(m.group(3)), int(m.group(1)), int(m.group(2)))
        except ValueError:
            pass
    m = _DATE_RE_DOT.search(title)
    if m:
        try:
            return datetime.date(int(m.group(3)), int(m.group(1)), int(m.group(2)))
        except ValueError:
            pass
    m = _DATE_RE_ISO.search(title)
    if m:
        try:
            return datetime.date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
        except ValueError:
            pass
    m = _DATE_RE_2.search(title)
    if m:
        yr = int(m.group(3))
        year = 2000 + yr
        try:
            return datetime.date(year, int(m.group(1)), int(m.group(2)))
        except ValueError:
            pass
    return None


def fetch_folder(folder_id):
    """Call the QScend files API for one folder. Returns a list of file dicts."""
    url = f"{API_URL}{folder_id}"
    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": UA,
            "Accept": "application/json",
            "Referer": BASE_URL,
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            charset = r.headers.get_content_charset() or "utf-8"
            raw = r.read().decode(charset, errors="replace")
        if raw.strip().startswith("["):
            return json.loads(raw)
        return []
    except urllib.error.HTTPError as e:
        print(f"  WARNING: folder {folder_id} — HTTP {e.code}", file=sys.stderr)
        return []
    except urllib.error.URLError as e:
        print(f"  WARNING: folder {folder_id} — {e}", file=sys.stderr)
        return []
    except (json.JSONDecodeError, ValueError) as e:
        print(f"  WARNING: folder {folder_id} — invalid JSON: {e}", file=sys.stderr)
        return []


def download_file(href, dest_path):
    """Download a file by its relative href. Returns True on success."""
    url = BASE_URL + href
    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": UA,
            "Accept": "*/*",
            "Referer": BASE_URL,
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            data = r.read()
        if not data:
            print(f"  WARNING: empty response for {href}", file=sys.stderr)
            return False
        with open(dest_path, "wb") as f:
            f.write(data)
        return True
    except Exception as e:
        print(f"  WARNING: {e}", file=sys.stderr)
        return False


def slugify(text, max_len=50):
    text = text.lower().strip()
    text = re.sub(r"[/\\&]", "-", text)
    text = re.sub(r"\s+-\s+", "-", text)
    text = re.sub(r"[^\w\s-]", "", text)
    text = re.sub(r"[\s_]+", "-", text)
    text = re.sub(r"-{2,}", "-", text)
    return text.strip("-")[:max_len]


def make_dest_path(board, doc_type, title, meeting_date, output_dir, ext=None):
    date_str = meeting_date.strftime("%Y-%m-%d")
    month_dir = os.path.join(output_dir, meeting_date.strftime("%Y-%m"))
    os.makedirs(month_dir, exist_ok=True)
    board_slug = slugify(board, max_len=35)
    title_slug = slugify(os.path.splitext(title)[0], max_len=45)
    if ext is None:
        ext = os.path.splitext(title)[1].lower() or ".pdf"
    fname = f"{date_str}-{board_slug}-{doc_type}-{title_slug}{ext}"
    return os.path.join(month_dir, fname)


# --- Main ---

def main():
    parser = argparse.ArgumentParser(
        description=(
            "Download New London CT municipal agendas and minutes "
            "from the QScend document system for meetings within the past N days."
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
        help="Only include boards whose name contains NAME (case-insensitive)",
    )
    parser.add_argument(
        "--no-minutes", action="store_true",
        help="Skip minutes",
    )
    parser.add_argument(
        "--no-agendas", action="store_true",
        help="Skip agendas",
    )
    parser.add_argument(
        "--no-recordings", action="store_true",
        help="Skip audio recordings",
    )
    parser.add_argument(
        "--list-boards", action="store_true",
        help="Print all known boards and exit",
    )
    args = parser.parse_args()

    if args.list_boards:
        seen = set()
        for board, doc_type, folder_ids in FOLDER_REGISTRY:
            if board not in seen:
                seen.add(board)
                entries = [(dt, fids) for b, dt, fids in FOLDER_REGISTRY if b == board]
                print(f"  {board}")
                for dt, fids in entries:
                    print(f"    {dt}: folders {fids}")
        return

    today = datetime.date.today()
    cutoff = today - datetime.timedelta(days=args.days)
    future_limit = today + datetime.timedelta(days=args.ahead)

    print(f"Date window : {cutoff} to {future_limit}")
    print(f"API base    : {BASE_URL}")
    if not args.dry_run:
        print(f"Output dir  : {args.output_dir}")
    print()

    # Build working registry from filters
    registry = FOLDER_REGISTRY
    if args.board:
        filter_str = args.board.lower()
        registry = [r for r in registry if filter_str in r[0].lower()]
        if not registry:
            print(
                f"No boards match '{args.board}'. "
                f"Use --list-boards to see all known boards.",
                file=sys.stderr,
            )
            sys.exit(1)
    if args.no_agendas:
        registry = [r for r in registry if r[1] != "agenda"]
    if args.no_minutes:
        registry = [r for r in registry if r[1] != "minutes"]
    if args.no_recordings:
        registry = [r for r in registry if r[1] != "recording"]

    total_folders = sum(len(fids) for _, _, fids in registry)
    print(
        f"Querying {total_folders} folder(s) across "
        f"{len(registry)} board/type combination(s)..."
    )
    print()

    docs = []
    seen_hrefs = set()

    for board, doc_type, folder_ids in registry:
        for folder_id in folder_ids:
            files = fetch_folder(folder_id)
            in_window = 0
            for f in files:
                title = f.get("title", "").strip()
                href = f.get("href", "").strip()
                if not title or not href:
                    continue
                if href in seen_hrefs:
                    continue
                meeting_date = parse_date_from_title(title)
                if meeting_date is None:
                    continue
                if not (cutoff <= meeting_date <= future_limit):
                    continue
                seen_hrefs.add(href)
                ext = os.path.splitext(href)[1].lower() or os.path.splitext(title)[1].lower() or ".pdf"
                docs.append({
                    "board": board,
                    "doc_type": doc_type,
                    "title": title,
                    "href": href,
                    "meeting_date": meeting_date,
                    "folder_id": folder_id,
                    "ext": ext,
                })
                in_window += 1
            print(
                f"  Folder {folder_id:<6} ({board} {doc_type}): "
                f"{len(files)} file(s), {in_window} in window"
            )
            time.sleep(API_DELAY)

    docs.sort(key=lambda x: (x["meeting_date"], x["board"]), reverse=True)

    print()
    print(
        f"Found {len(docs)} document(s) across "
        f"{len({d['board'] for d in docs})} board(s) in date window."
    )
    print()

    if not docs:
        print("No documents found within the date window.")
        return

    if args.dry_run:
        print(f"{'Board':<38} {'Date':<12} {'Type':<10} Title")
        print("-" * 95)
        for d in docs:
            print(
                f"{d['board'][:37]:<38} "
                f"{d['meeting_date']!s:<12} "
                f"{d['doc_type']:<10} "
                f"{d['title'][:35]}  [{d.get('ext', '.pdf')}]"
            )
        print(f"\n{len(docs)} item(s). Re-run without --dry-run to download.")
        return

    # Download
    os.makedirs(args.output_dir, exist_ok=True)
    log_path = os.path.join(args.output_dir, "download-log.txt")
    log_lines = []
    downloaded = skipped = failed = 0

    for d in docs:
        dest = make_dest_path(
            d["board"], d["doc_type"], d["title"], d["meeting_date"], args.output_dir,
            ext=d.get("ext"),
        )
        label = os.path.basename(dest)

        if os.path.exists(dest):
            print(f"  skip (exists)  {label}")
            skipped += 1
            continue

        print(f"  [{d['meeting_date']}] {d['board']} — {d['doc_type']}: {d['title']}")
        print(f"  downloading    {label}")

        if download_file(d["href"], dest):
            downloaded += 1
            log_lines.append(f"{datetime.datetime.now().isoformat()}  OK       {dest}")
        else:
            failed += 1
            log_lines.append(
                f"{datetime.datetime.now().isoformat()}  FAILED   {d['href']}"
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
#    python3 scripts/download-new-london-agendas.py --dry-run
#
# 2. Narrow to one board:
#    python3 scripts/download-new-london-agendas.py --board "City Council"
#
# 3. Agendas only (skip minutes and recordings):
#    python3 scripts/download-new-london-agendas.py --no-minutes --no-recordings
#
# 4. List all known boards:
#    python3 scripts/download-new-london-agendas.py --list-boards
#
# 5. Change the lookback window:
#    python3 scripts/download-new-london-agendas.py --days 7
#
# 6. Save files somewhere else:
#    python3 scripts/download-new-london-agendas.py --output-dir ~/Downloads/new-london
#
# 7. Run on a schedule (cron — 8 AM daily):
#    0 8 * * * cd /path/to/repo && python3 scripts/download-new-london-agendas.py
#
# NOTE: Meeting dates are parsed from the MM-DD-YYYY (or MM-DD-YY) pattern in
# each document's title. Documents with non-standard naming such as text months
# ("Nov 2024") will be skipped.
#
# NOTE: City Council Agendas 2025 are in folder 41840; Minutes 2025 in 42073.
# The 2026 City Council agendas folder had not yet appeared as of May 2026;
# add it to FOLDER_REGISTRY when it does. City Council Minutes 2026: folder 44885.
#
# NOTE: WWPCA agendas 2025/earlier are in folder 37534; 2026 agendas in 44750.
# WWPCA minutes 2025/earlier are in folder 37538; 2026 minutes in 45711.
#
# NOTE: The --ahead flag (default: 7 days) captures agendas for upcoming
# meetings already published. Run daily to stay current.
