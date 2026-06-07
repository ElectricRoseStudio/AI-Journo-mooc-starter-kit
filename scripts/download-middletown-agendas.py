#!/usr/bin/env python3
# download-middletown-agendas.py
# Download municipal meeting agendas and minutes from Middletown CT for meetings
# whose date falls within the past N days (and up to 7 days ahead, to catch
# agendas posted early for upcoming meetings), plus Granicus MP4 recordings.
#
# USAGE:
#   python3 scripts/download-middletown-agendas.py [options]
#
# REQUIREMENTS:
#   - Python 3.6+  (no third-party packages needed)
#   - Internet connection
#   - Disk space: Granicus recordings can be 1–3 GB each
#
# WHAT IT DOES:
#   1. Fetches the Middletown CT Agenda Center hub page to discover all board
#      category IDs (~97 boards)
#   2. Calls the AgendaCenter Search endpoint with those IDs and a date range
#      — this returns all matching rows in a single inline HTML response
#   3. Parses meeting rows for agenda and minutes ViewFile URLs
#   4. Downloads PDFs to beat-archive/middletown-agendas/YYYY-MM/
#   5. Fetches the Granicus ViewPublisher page (all recorded meetings in one page)
#   6. Filters recordings to the date window and downloads MP4s to YYYY-MM/
#   7. Appends a download log to beat-archive/middletown-agendas/download-log.txt
#
# SITE STRUCTURE:
#   AgendaCenter (CivicPlus CivicEngage):
#     Hub:    https://www.middletownct.gov/agendacenter
#     Search: GET /AgendaCenter/Search/
#               ?term=&CIDs={cat1,...}&startDate=MM%2FDD%2FYYYY
#               &endDate=MM%2FDD%2FYYYY&dateRange=custom&dateSelector=range
#     Agenda: /AgendaCenter/ViewFile/Agenda/_{MMDDYYYY}-{meetingID}
#     Minutes:/AgendaCenter/ViewFile/Minutes/_{MMDDYYYY}-{meetingID}
#
#   Granicus video archive:
#     Listing:  https://middletown.granicus.com/ViewPublisher.php?view_id=2
#     MP4s:     https://archive-video.granicus.com/middletown/middletown_{UUID}.mp4
#     The listing page is ~10 MB and contains all recordings since the channel
#     launched. It is fetched once per run and parsed in memory.

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

# --- Configuration ---
BASE_URL = "https://www.middletownct.gov"
HUB_URL = f"{BASE_URL}/agendacenter"
SEARCH_URL = f"{BASE_URL}/AgendaCenter/Search/"
GRANICUS_LISTING = "https://middletown.granicus.com/ViewPublisher.php?view_id=2"
OUTPUT_DIR = "beat-archive/middletown-agendas"
DAYS_BACK = 4
DAYS_AHEAD = 7
DELAY_SECONDS = 0.8
CHUNK_SIZE = 1024 * 1024  # 1 MB chunks for large video files

UA = "Mozilla/5.0"

# Parses _MMDDYYYY-meetingID from ViewFile paths
_DATE_ID_RE = re.compile(r'_(\d{2})(\d{2})(\d{4})-(\d+)$')


# --- HTTP helpers ---

def fetch_html(url, params=None):
    """GET url (with optional query params dict) and return decoded HTML, or None."""
    full_url = url
    if params:
        full_url += "?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(
        full_url,
        headers={"User-Agent": UA, "Accept": "text/html,*/*"},
    )
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            raw = r.read()
            charset = r.headers.get_content_charset() or "utf-8"
            return raw.decode(charset, errors="replace")
    except urllib.error.HTTPError as e:
        print(f"  HTTP {e.code} — {full_url}", file=sys.stderr)
        return None
    except Exception as e:
        print(f"  ERROR fetching {full_url}: {e}", file=sys.stderr)
        return None


def download_file(url, dest_path):
    """Download url to dest_path. Returns True on success."""
    req = urllib.request.Request(
        url,
        headers={"User-Agent": UA, "Accept": "application/pdf, */*"},
    )
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            with open(dest_path, "wb") as f:
                f.write(r.read())
        return True
    except Exception as e:
        print(f"  WARNING: {e}", file=sys.stderr)
        return False


def download_video(url, dest_path):
    """
    Chunked download for large MP4 files (1–3 GB).
    Prints progress dots. Returns True on success.
    """
    req = urllib.request.Request(
        url,
        headers={"User-Agent": UA, "Accept": "video/mp4, */*"},
    )
    try:
        with urllib.request.urlopen(req, timeout=120) as r:
            total = r.headers.get("Content-Length")
            total_mb = f"{int(total)/1024/1024:.0f} MB" if total else "unknown size"
            print(f"  size: {total_mb}", end="", flush=True)
            written = 0
            with open(dest_path, "wb") as f:
                while True:
                    chunk = r.read(CHUNK_SIZE)
                    if not chunk:
                        break
                    f.write(chunk)
                    written += len(chunk)
                    if total:
                        pct = written / int(total) * 100
                        if pct % 10 < (CHUNK_SIZE / int(total) * 100):
                            print(".", end="", flush=True)
            print()
        return True
    except Exception as e:
        print(f"\n  WARNING: {e}", file=sys.stderr)
        return False


# --- PDF parsing (CivicEngage) ---

def parse_category_ids(hub_html):
    """Extract all board category IDs from the hub page."""
    return list(dict.fromkeys(re.findall(r'id="cat(\d+)"', hub_html)))


def parse_meetings(search_html):
    """
    Parse meeting rows from the Search results page.

    Returns a list of dicts:
      {board, meeting_date, meeting_id, agenda_url, minutes_url}
    """
    board_names = {}
    for m in re.finditer(
        r'id="cat(\d+)"[^>]*>.*?<h2[^>]*>(.*?)</h2>', search_html, re.DOTALL
    ):
        cat_id = m.group(1)
        name = html_module.unescape(re.sub(r'<[^>]+>', '', m.group(2)).strip())
        board_names[cat_id] = name

    meetings = []

    for pan_m in re.finditer(
        r'<div\s+id="category-panel-(\d+)"[^>]*>(.*?)</div>\s*</span>',
        search_html, re.DOTALL,
    ):
        cat_id = pan_m.group(1)
        panel_html = pan_m.group(2)
        board = board_names.get(cat_id, f"cat{cat_id}")

        for row_m in re.finditer(
            r'<tr[^>]+class="catAgendaRow"[^>]*>(.*?)</tr>',
            panel_html, re.DOTALL,
        ):
            row_html = row_m.group(1)

            agenda_m = re.search(
                r'href="(/AgendaCenter/ViewFile/Agenda/(_\d{8}-\d+))"',
                row_html,
            )
            if not agenda_m:
                continue
            agenda_path = agenda_m.group(1)
            date_id_str = agenda_m.group(2)

            dm = _DATE_ID_RE.match(date_id_str)
            if not dm:
                continue
            mm, dd, yyyy, meeting_id = dm.groups()
            try:
                meeting_date = datetime.date(int(yyyy), int(mm), int(dd))
            except ValueError:
                continue

            minutes_path = None
            minutes_td = re.search(
                r'<td[^>]+class="minutes"[^>]*>(.*?)</td>', row_html, re.DOTALL
            )
            if minutes_td and 'ViewFile/Minutes' in minutes_td.group(1):
                min_m = re.search(
                    r'href="(/AgendaCenter/ViewFile/Minutes/[^"]+)"',
                    minutes_td.group(1),
                )
                if min_m:
                    minutes_path = min_m.group(1)

            meetings.append({
                "board": board,
                "meeting_date": meeting_date,
                "meeting_id": meeting_id,
                "agenda_url": BASE_URL + agenda_path,
                "minutes_url": BASE_URL + minutes_path if minutes_path else None,
            })

    return meetings


# --- Granicus video parsing ---

def parse_granicus_recordings(listing_html, cutoff, future_limit):
    """
    Parse meeting recordings from the Granicus ViewPublisher page.

    Returns a list of dicts: {name, meeting_date, mp4_url, clip_id}
    filtered to the date window. Entries without an MP4 URL are skipped.
    """
    recordings = []

    for row_m in re.finditer(
        r'<tr[^>]+class="listingRow"[^>]*>(.*?)</tr>', listing_html, re.DOTALL
    ):
        row = row_m.group(1)

        # Meeting name
        name_m = re.search(
            r'headers="Name"[^>]*scope="row"[^>]*>\s*(.*?)\s*</td>', row, re.DOTALL
        )
        if not name_m:
            continue
        name = html_module.unescape(
            re.sub(r'<[^>]+>', '', name_m.group(1))
        ).strip()

        # Date — "May&nbsp; 4,&nbsp;2026 - 07:01&nbsp;PM"
        date_m = re.search(
            r'headers="Date\s[^"]*"[^>]*>(.*?)</td>', row, re.DOTALL
        )
        if not date_m:
            continue
        raw = html_module.unescape(
            re.sub(r'<[^>]+>', '', date_m.group(1))
        )
        raw = raw.replace('\xa0', ' ')           # non-breaking spaces → regular
        raw = re.sub(r'\s+', ' ', raw).strip()   # collapse whitespace
        date_str = raw.split('-')[0].strip()      # drop time component
        try:
            meeting_date = datetime.datetime.strptime(date_str, "%B %d, %Y").date()
        except ValueError:
            continue

        if not (cutoff <= meeting_date <= future_limit):
            continue

        # MP4 download URL
        mp4_m = re.search(
            r'href="(https://archive-video\.granicus\.com/middletown/[^"]+\.mp4)"',
            row,
        )
        if not mp4_m:
            continue  # live-only or no recording available

        # Clip ID (for filename uniqueness)
        clip_m = re.search(r'clip_id=(\d+)', row)
        clip_id = clip_m.group(1) if clip_m else "0"

        recordings.append({
            "name": name,
            "meeting_date": meeting_date,
            "mp4_url": mp4_m.group(1),
            "clip_id": clip_id,
        })

    return recordings


# --- Utilities ---

def slugify(text, max_len=55):
    text = text.lower().strip()
    text = re.sub(r"[/\\&]", "-", text)
    text = re.sub(r"[^\w\s-]", "", text)
    text = re.sub(r"[\s_]+", "-", text)
    text = re.sub(r"-{2,}", "-", text)
    return text.strip("-")[:max_len]


def make_dest_path(board, doc_type, meeting_date, meeting_id, output_dir):
    date_str = meeting_date.strftime("%Y-%m-%d")
    month_dir = os.path.join(output_dir, meeting_date.strftime("%Y-%m"))
    os.makedirs(month_dir, exist_ok=True)
    board_slug = slugify(board, max_len=45)
    fname = f"{date_str}-{board_slug}-{meeting_id}-{doc_type}.pdf"
    return os.path.join(month_dir, fname)


def make_video_path(name, meeting_date, clip_id, output_dir):
    date_str = meeting_date.strftime("%Y-%m-%d")
    month_dir = os.path.join(output_dir, meeting_date.strftime("%Y-%m"))
    os.makedirs(month_dir, exist_ok=True)
    name_slug = slugify(name, max_len=50)
    fname = f"{date_str}-{name_slug}-{clip_id}.mp4"
    return os.path.join(month_dir, fname)


# --- Main ---

def main():
    parser = argparse.ArgumentParser(
        description=(
            "Download Middletown CT municipal agendas, minutes, and Granicus "
            "meeting recordings for meetings within the past N days."
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
        help="Only include boards/meeting names containing NAME (case-insensitive)",
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
        "--docs-only", action="store_true",
        help="Download PDFs only, skip video recordings",
    )
    parser.add_argument(
        "--videos-only", action="store_true",
        help="Download video recordings only, skip PDFs",
    )
    args = parser.parse_args()

    today = datetime.date.today()
    cutoff = today - datetime.timedelta(days=args.days)
    future_limit = today + datetime.timedelta(days=args.ahead)

    # CivicEngage Search uses MM/DD/YYYY without zero-padding (Linux)
    start_str = cutoff.strftime("%-m/%-d/%Y")
    end_str = future_limit.strftime("%-m/%-d/%Y")

    print(f"Date window : {cutoff} to {future_limit}")
    print(f"Hub page    : {HUB_URL}")
    if not args.dry_run:
        print(f"Output dir  : {args.output_dir}")
    print()

    all_docs: list = []
    recordings: list = []

    # --- Step 1: PDFs from AgendaCenter ---
    if not args.videos_only:
        print("Fetching hub page to discover board categories...")
        hub_html = fetch_html(HUB_URL)
        if not hub_html:
            print("ERROR: Could not fetch the hub page.", file=sys.stderr)
            sys.exit(1)
        cat_ids = parse_category_ids(hub_html)
        if not cat_ids:
            print(
                "ERROR: No category IDs found — page structure may have changed.",
                file=sys.stderr,
            )
            sys.exit(1)
        print(f"  Found {len(cat_ids)} board category/categories.")

        print("Searching for meetings in date window...")
        search_params = {
            "term": "",
            "CIDs": ",".join(cat_ids),
            "startDate": start_str,
            "endDate": end_str,
            "dateRange": "custom",
            "dateSelector": "range",
        }
        search_html = fetch_html(SEARCH_URL, search_params)
        if not search_html:
            print("ERROR: Could not fetch search results.", file=sys.stderr)
            sys.exit(1)
        meetings = parse_meetings(search_html)
        print(f"  Found {len(meetings)} meeting(s) with documents in date window.")
        print()

        if args.board:
            filter_str = args.board.lower()
            meetings = [m for m in meetings if filter_str in m["board"].lower()]
            print(f"Filtered to {len(meetings)} meeting(s) matching '{args.board}'.")
            print()

        for mtg in meetings:
            if not args.no_agendas and mtg["agenda_url"]:
                all_docs.append({**mtg, "doc_type": "agenda", "url": mtg["agenda_url"]})
            if not args.no_minutes and mtg["minutes_url"]:
                all_docs.append({**mtg, "doc_type": "minutes", "url": mtg["minutes_url"]})
        all_docs.sort(key=lambda x: (x["meeting_date"], x["board"]), reverse=True)

    # --- Step 2: Granicus video recordings ---
    if not args.docs_only:
        print("Fetching Granicus recording list (this page is ~10 MB)...")
        listing_html = fetch_html(GRANICUS_LISTING)
        if not listing_html:
            print("WARNING: Could not fetch Granicus listing.", file=sys.stderr)
        else:
            recordings = parse_granicus_recordings(listing_html, cutoff, future_limit)
            if args.board:
                filter_str = args.board.lower()
                recordings = [r for r in recordings if filter_str in r["name"].lower()]
            print(f"  Found {len(recordings)} recording(s) in window.")
        print()

    if not all_docs and not recordings:
        print("No documents or recordings found in the date window.")
        return

    # --- Dry-run listing ---
    if args.dry_run:
        if all_docs:
            print(f"{'Board':<48} {'Date':<12} {'ID':<8} Type")
            print("-" * 80)
            for d in all_docs:
                print(
                    f"{d['board'][:47]:<48} "
                    f"{d['meeting_date']!s:<12} "
                    f"{d['meeting_id']:<8} "
                    f"{d['doc_type']}"
                )
            print()
        if recordings:
            print(f"{'Date':<12} {'Clip':<6} Meeting")
            print("-" * 72)
            for rec in recordings:
                print(f"{rec['meeting_date']!s:<12} {rec['clip_id']:<6} {rec['name']}")
            print()
        total = len(all_docs) + len(recordings)
        print(f"{total} item(s) matched. Re-run without --dry-run to download.")
        return

    # --- Step 3: Download PDFs ---
    os.makedirs(args.output_dir, exist_ok=True)
    log_path = os.path.join(args.output_dir, "download-log.txt")
    log_lines = []
    downloaded = skipped = failed = 0

    if all_docs:
        for d in all_docs:
            dest = make_dest_path(
                d["board"], d["doc_type"], d["meeting_date"],
                d["meeting_id"], args.output_dir,
            )
            label = os.path.basename(dest)

            if os.path.exists(dest):
                print(f"  skip (exists)  {label}")
                skipped += 1
                continue

            print(f"  [{d['meeting_date']}] {d['board'][:50]} — {d['doc_type']}")
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

    # --- Step 4: Download Granicus recordings ---
    if recordings:
        print(f"Downloading {len(recordings)} Granicus recording(s)...")
        print("  NOTE: Files are 1–3 GB each. Progress: one dot per 10%.\n")

        for rec in recordings:
            dest = make_video_path(
                rec["name"], rec["meeting_date"], rec["clip_id"], args.output_dir
            )
            label = os.path.basename(dest)

            if os.path.exists(dest):
                print(f"  skip (exists)  {label}")
                skipped += 1
                continue

            print(f"  [{rec['meeting_date']}] {rec['name']} (clip {rec['clip_id']})")
            print(f"  downloading    {label}")

            if download_video(rec["mp4_url"], dest):
                downloaded += 1
                log_lines.append(
                    f"{datetime.datetime.now().isoformat()}  OK       {dest}"
                )
            else:
                failed += 1
                log_lines.append(
                    f"{datetime.datetime.now().isoformat()}  FAILED   {rec['mp4_url']}"
                )
                if os.path.exists(dest):
                    os.remove(dest)

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
#    python3 scripts/download-middletown-agendas.py --dry-run
#
# 2. Narrow to one board:
#    python3 scripts/download-middletown-agendas.py --board "Common Council"
#
# 3. PDFs only (no video downloads):
#    python3 scripts/download-middletown-agendas.py --docs-only
#
# 4. Videos only:
#    python3 scripts/download-middletown-agendas.py --videos-only
#
# 5. Agendas only (skip minutes):
#    python3 scripts/download-middletown-agendas.py --no-minutes
#
# 6. Change the lookback window:
#    python3 scripts/download-middletown-agendas.py --days 7
#
# 7. Save files somewhere else:
#    python3 scripts/download-middletown-agendas.py --output-dir ~/Downloads/middletown
#
# 8. Run on a schedule (cron — 7 AM daily):
#    0 7 * * * cd /path/to/repo && python3 scripts/download-middletown-agendas.py
#
# NOTES:
#   - Middletown CT uses CivicPlus CivicEngage. All board rows are embedded
#     inline in the Search results page — no AJAX calls needed. Category IDs
#     are discovered dynamically from the hub page each run, so new boards
#     are picked up automatically.
#   - Meeting dates are encoded as MMDDYYYY in ViewFile URL paths, e.g.:
#       /AgendaCenter/ViewFile/Agenda/_05042026-17034 → May 4, 2026
#   - Granicus recordings are served as direct MP4 files from
#     archive-video.granicus.com with no authentication required.
#     The listing page (~10 MB) contains all ~200 archived recordings since
#     the channel launched — it is fetched once per run and parsed in memory.
#   - Common Council sessions typically run 3–4 hours and produce files
#     of 3+ GB. Plan for significant disk space and download time.
