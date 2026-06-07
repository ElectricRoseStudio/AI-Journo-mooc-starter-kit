#!/usr/bin/env python3
# download-suffield-agendas.py
# Download municipal meeting agendas and minutes from Suffield CT
# for meetings whose date falls within the past N days (and up to 7 days ahead).
#
# USAGE:
#   python3 scripts/download-suffield-agendas.py [options]
#
# REQUIREMENTS:
#   - Python 3.6+  (no third-party packages needed)
#   - Internet connection
#
# WHAT IT DOES:
#   1. Fetches the single Agendas & Minutes hub page, which embeds all
#      current documents with their download UUIDs and board IDs
#   2. Extracts the board JSON list to map board IDs to board names
#   3. For each document: parses the date (MM-DD-YYYY filename prefix),
#      infers the document type (agenda, minutes, notice, cancellation),
#      and associates it with its board via the fsBoard-{id} class
#   4. Filters to the configured date window, deduplicates by UUID,
#      and downloads each match via the resource-manager endpoint
#   5. Appends a download log to beat-archive/suffield-agendas/download-log.txt
#
# SITE STRUCTURE:
#   CMS: Finalsite at suffieldct.gov
#
#   Hub page (single request returns all current documents):
#     https://www.suffieldct.gov/government/agendas-minutes
#
#   Board list: embedded as JSON in data-boards attribute
#     Each board: {"id": N, "name": "Board Name"}  (34 boards as of 2026)
#
#   Article elements contain the board ID and document link:
#     <article class="fsBoard-{id}" data-post-id="...">
#       <a data-file-name="MM-DD-YYYYBoardNameDocType.pdf"
#          data-resource-uuid="xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx"
#          href="/fs/resource-manager/view/{uuid}">...</a>
#     </article>
#
#   Download URL:
#     https://www.suffieldct.gov/fs/resource-manager/view/{uuid}
#     Returns the PDF directly (Content-Type: application/pdf)
#
#   Recordings: None. The page title mentions "Recordings" but no audio
#   or video links are published on this page as of 2026.
#
#   Date window coverage: The hub page retains approximately 2-3 months
#   of recent documents for active boards. For lookbacks longer than 60
#   days, some older items may not appear; the default 30-day window is
#   fully covered.
#
# BOARDS (34, as of 2026-05):
#   350th Anniversary Committee, Advisory Commission on Capital Expenditures,
#   American Rescue Plan Commission, Board of Assessment Appeals,
#   Board of Education, Board of Finance, Board of Selectmen,
#   Building Code Board of Appeals, Charter Revision Commission,
#   Conservation Commission, Design Review Board,
#   Economic Development Commission, Emergency Management,
#   Environmental & Sustainability Task Force, Ethics Commission,
#   Fire Commission, Helena Bailey Spencer Tree Fund Committee,
#   Historic District Commission, Housing Authority, Library Commission,
#   Local Prevention Council, North Central District Health Department,
#   Parks & Recreation Commission,
#   Pedestrian and Traffic Safety and Infrastructure Committee,
#   Permanent Building Commission, Planning & Zoning Commission,
#   Police Commission, Retirement Commission, Social Services Commission,
#   Town Meetings, Veterans Appreciation Committee,
#   Veterans Memorial Expansion Committee, Water Pollution Control Authority,
#   Zoning Board of Appeals

import argparse
import datetime
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

# --- Configuration ---
BASE_URL = "https://www.suffieldct.gov"
HUB_URL = f"{BASE_URL}/government/agendas-minutes"
RESOURCE_URL = f"{BASE_URL}/fs/resource-manager/view"

OUTPUT_DIR = "beat-archive/suffield-agendas"
DAYS_BACK = 4
DAYS_AHEAD = 7
DELAY_SECONDS = 0.5

UA = "Mozilla/5.0 (X11; Linux x86_64; rv:120.0) Gecko/20100101 Firefox/120.0"

# Filename date prefix: MM-DD-YYYY (e.g. "05-12-2026BoardNameDocType.pdf")
_FNAME_DATE_RE = re.compile(r"^(\d{2})-(\d{2})-(\d{4})")


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


# --- Parsing ---

def parse_board_map(html):
    """Extract {board_id_str → board_name} from the JSON in data-boards."""
    m = re.search(r'data-boards="(\[.*?\])"', html)
    if not m:
        return {}
    try:
        raw = m.group(1).replace("&quot;", '"')
        return {str(b["id"]): b["name"] for b in json.loads(raw)}
    except Exception:
        return {}


def parse_articles(html, board_map):
    """
    Extract document items from <article class="fsBoard-{id}"> elements.

    Returns a list of dicts:
      {board, date, doc_type, uuid, filename}
    """
    articles = re.findall(
        r'<article[^>]+class="[^"]*fsBoard-(\d+)[^"]*"[^>]*>(.*?)</article>',
        html, re.DOTALL,
    )

    seen_uuids = set()
    items = []

    for board_id, article_html in articles:
        fm = re.search(
            r'data-file-name="([^"]+)"[^>]+data-resource-uuid="([a-f0-9-]+)"',
            article_html,
        )
        if not fm:
            continue

        filename = fm.group(1)
        uuid = fm.group(2)

        if uuid in seen_uuids:
            continue
        seen_uuids.add(uuid)

        date = parse_filename_date(filename)
        if not date:
            continue

        board = board_map.get(board_id, f"Board {board_id}")
        doc_type = infer_doc_type(filename)

        items.append({
            "board": board,
            "date": date,
            "doc_type": doc_type,
            "uuid": uuid,
            "filename": filename,
        })

    return items


def parse_filename_date(filename):
    """Parse MM-DD-YYYY date prefix from filename."""
    m = _FNAME_DATE_RE.match(filename)
    if not m:
        return None
    mm, dd, yyyy = m.groups()
    try:
        return datetime.date(int(yyyy), int(mm), int(dd))
    except ValueError:
        return None


def infer_doc_type(filename):
    """Infer document type from filename."""
    name = filename.lower()
    if "minutes" in name:
        return "minutes"
    if "agenda" in name:
        return "agenda"
    if "cancellation" in name:
        return "cancellation"
    if "legalnotice" in name or "legal-notice" in name:
        return "notice"
    return "document"


# --- Path utilities ---

def make_doc_path(item, output_dir):
    """Build output path for a document item."""
    date = item["date"]
    month_dir = os.path.join(output_dir, date.strftime("%Y-%m"))
    os.makedirs(month_dir, exist_ok=True)
    # Normalize filename: replace MM-DD-YYYY prefix with YYYY-MM-DD and add separator
    new_name = date.strftime("%Y-%m-%d") + "-" + item["filename"][10:]
    return os.path.join(month_dir, new_name)


# --- Main ---

def main():
    parser = argparse.ArgumentParser(
        description=(
            "Download Suffield CT municipal agendas and minutes "
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

    print(f"Date window : {cutoff} to {future_limit}")
    print(f"Hub page    : {HUB_URL}")
    if not args.dry_run:
        print(f"Output dir  : {args.output_dir}")
    print()

    # --- Fetch hub page ---
    print("Fetching hub page...")
    html = fetch_html(HUB_URL)
    if not html:
        print("FATAL: Could not fetch hub page.", file=sys.stderr)
        sys.exit(1)

    board_map = parse_board_map(html)
    if not board_map:
        print("FATAL: Could not parse board list from page.", file=sys.stderr)
        sys.exit(1)
    print(f"  Found {len(board_map)} boards.\n")

    # --- Parse and filter documents ---
    all_items = parse_articles(html, board_map)

    if board_filter:
        all_items = [i for i in all_items if board_filter in i["board"].lower()]

    if args.no_minutes:
        all_items = [i for i in all_items if i["doc_type"] != "minutes"]
    if args.no_agendas:
        all_items = [i for i in all_items if i["doc_type"] != "agenda"]

    matched = [
        i for i in all_items
        if cutoff <= i["date"] <= future_limit
    ]
    matched.sort(key=lambda x: (x["date"], x["board"]))

    print(f"  {len(all_items)} document(s) on hub page, "
          f"{len(matched)} within date window.\n")

    if not matched:
        print("No items found in the date window.")
        return

    # --- Dry-run listing ---
    if args.dry_run:
        print(f"{'Board':<42} {'Date':<12} {'Type':<14} Filename")
        print("-" * 90)
        for d in matched:
            print(
                f"{d['board'][:41]:<42} "
                f"{d['date']!s:<12} "
                f"{d['doc_type']:<14} "
                f"{d['filename']}"
            )
        print()
        print(f"{len(matched)} item(s) matched. Re-run without --dry-run to download.")
        return

    # --- Download PDFs ---
    os.makedirs(args.output_dir, exist_ok=True)
    log_path = os.path.join(args.output_dir, "download-log.txt")
    log_lines = []
    downloaded = skipped = failed = 0

    print(f"Downloading {len(matched)} document(s)...")
    for d in matched:
        dest = make_doc_path(d, args.output_dir)
        label = os.path.basename(dest)

        if os.path.exists(dest):
            print(f"  skip (exists)  {label}")
            skipped += 1
            continue

        print(f"  [{d['date']}] {d['board'][:45]} — {d['doc_type']}")
        print(f"  downloading    {label}")

        url = f"{RESOURCE_URL}/{d['uuid']}"
        if download_file(url, dest):
            downloaded += 1
            log_lines.append(
                f"{datetime.datetime.now().isoformat()}  OK       {dest}"
            )
        else:
            failed += 1
            log_lines.append(
                f"{datetime.datetime.now().isoformat()}  FAILED   {url}"
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
#    python3 scripts/download-suffield-agendas.py --dry-run
#
# 2. Narrow to one board:
#    python3 scripts/download-suffield-agendas.py --board "Planning"
#
# 3. Agendas only (skip minutes):
#    python3 scripts/download-suffield-agendas.py --no-minutes
#
# 4. Change the lookback window:
#    python3 scripts/download-suffield-agendas.py --days 14
#
# 5. Run on a schedule (cron — 7 AM daily):
#    0 7 * * * cd /path/to/repo && python3 scripts/download-suffield-agendas.py
#
# NOTES:
#   - Suffield uses the Finalsite CMS. All documents are embedded on a single
#     hub page — no separate per-board listing is needed.
#   - Each document is stored as a UUID-keyed resource. The UUID is extracted
#     from the data-resource-uuid attribute on each document link.
#   - Board association uses the fsBoard-{id} CSS class on each <article>
#     element, cross-referenced against the JSON board list embedded in the page.
#   - Documents appear on the hub page for approximately 2-3 months after
#     posting. For lookbacks longer than 60 days some older items may be absent.
#   - Joint meetings (e.g. Fire Commission + Permanent Building Commission)
#     appear as separate document entries under each board, sometimes with
#     different filenames (board order in title varies).
#   - The page title says "Recordings" but no audio/video links are published
#     on this page. Recordings are not available for download.
