#!/usr/bin/env python3
# download-granby-agendas.py
# Download municipal meeting agendas, minutes, and GCTV16 recordings from Granby CT
# for meetings whose date falls within the past N days (and up to 7 days ahead).
#
# USAGE:
#   python3 scripts/download-granby-agendas.py [options]
#
# REQUIREMENTS:
#   - Python 3.6+  (no third-party packages needed)
#   - yt-dlp installed (for recording downloads only): pip install yt-dlp
#   - Internet connection
#
# WHAT IT DOES:
#   1. Fetches the Granby CT AgendaCenter hub page to discover all board
#      category IDs (25 boards as of 2026)
#   2. Calls the AgendaCenter Search endpoint (GET) with those IDs and a date
#      range — returns all matching rows in a single HTML response
#   3. For each row: downloads the agenda PDF, agenda packet (if present),
#      and minutes PDF (if present)
#   4. For rows with a GCTV16 recording link: downloads the recording with
#      yt-dlp (GCTV16 pages embed YouTube videos, which yt-dlp extracts)
#   5. Appends a download log to beat-archive/granby-agendas/download-log.txt
#
# SITE STRUCTURE:
#   CMS: CivicPlus CivicEngage at granby-ct.gov
#
#   Hub:
#     https://www.granby-ct.gov/agendacenter
#     Category IDs in id="cat{N}" elements; 25 boards (cat2–cat28).
#
#   Search (GET):
#     https://www.granby-ct.gov/AgendaCenter/Search/
#       ?term=&CIDs={cat1,cat2,...}&startDate=M/D/YYYY
#       &endDate=M/D/YYYY&dateRange=custom&dateSelector=0
#     Returns all matching rows inline (no pagination needed).
#
#   Row structure per <tr class="catAgendaRow">:
#     Agenda:  href="/AgendaCenter/ViewFile/Agenda/_{MMDDYYYY}-{id}?html=true"
#              Plain PDF:   /AgendaCenter/ViewFile/Agenda/_{MMDDYYYY}-{id}
#              Packet PDF:  /AgendaCenter/ViewFile/Agenda/_{MMDDYYYY}-{id}?packet=true
#     Minutes: href="/AgendaCenter/ViewFile/Minutes/_{MMDDYYYY}-{id}"
#              (inside <td class="minutes">; absent if not yet posted)
#     Media:   href="https://gctv16.org/viewshows/view/{slug}/"
#              (inside <td class="media">; absent if no recording)
#
#   Recordings (GCTV16 / Granby Community Television):
#     https://gctv16.org hosts recordings for most public meetings.
#     Each GCTV16 "viewshows" page embeds a YouTube video; yt-dlp
#     detects and downloads the embedded YouTube video automatically.
#     Archive file (media-archive.txt) uses yt-dlp's native format
#     ("youtube {video_id}") to prevent re-downloading on subsequent runs.
#
#   NOTE: The Town of Granby also has a YouTube channel (@TownofGranby,
#   channel ID UC0DA5cjynK1oLOeTNLtwVoQ) but it is not consistently used
#   for meeting recordings (mostly graduation ceremonies). GCTV16 is the
#   authoritative recording source for municipal meetings.
#
# BOARDS (25, as of 2026-05):
#   Affordable Housing Plan Committee, Agricultural Commission,
#   Board of Assessment Appeals, Board of Finance, Board of Selectmen,
#   Capital Program Priorities Advisory Committee, Charter Revision Commission,
#   Commission on Aging, Conservation Commission,
#   Development Commission, Employee Health Benefits Fund Advisory Committee,
#   Granby America 250 Committee, Granby Center Advisory Committee,
#   Granby Water Pollution Control Authority,
#   Inland Wetlands & Watercourses Commission, Intra-Board Advisory Committee,
#   Library Board, Park & Recreation Board,
#   Plan of Conservation & Development Implementation Committee,
#   Plan of Conservation and Development 2026 Committee,
#   Planning & Zoning Commission, School Projects Building Committee,
#   Town Bridges Building Committee, Youth Service Bureau Advisory Board,
#   Zoning Board of Appeals

import argparse
import datetime
import html as html_module
import os
import re
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

# --- Configuration ---
BASE_URL = "https://www.granby-ct.gov"
HUB_URL = f"{BASE_URL}/agendacenter"
SEARCH_URL = f"{BASE_URL}/AgendaCenter/Search/"

OUTPUT_DIR = "beat-archive/granby-agendas"
DAYS_BACK = 4
DAYS_AHEAD = 7
DELAY_SECONDS = 0.5

UA = "Mozilla/5.0 (X11; Linux x86_64; rv:120.0) Gecko/20100101 Firefox/120.0"

# Parses _MMDDYYYY-meetingID from ViewFile paths (stops before any query string)
_DATE_ID_RE = re.compile(r"_(\d{2})(\d{2})(\d{4})-(\d+)$")


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
        with urllib.request.urlopen(req, timeout=30) as r:
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


# --- CivicPlus AgendaCenter parsing ---

def parse_category_ids(hub_html):
    """Extract all board category IDs from the AgendaCenter hub page."""
    return list(dict.fromkeys(re.findall(r'id="cat(\d+)"', hub_html)))


def parse_meetings(search_html):
    """
    Parse meeting rows from the AgendaCenter Search results page.

    Returns a list of dicts:
      {board, meeting_date, meeting_id, agenda_url, packet_url,
       minutes_url, gctv_url}
    packet_url, minutes_url, and gctv_url are None if absent.
    """
    # Build board name map from id="cat{id}" headings
    board_names = {}
    for m in re.finditer(
        r'id="cat(\d+)"[^>]*>.*?<h2[^>]*>(.*?)</h2>', search_html, re.DOTALL
    ):
        cat_id = m.group(1)
        name = html_module.unescape(re.sub(r"<[^>]+>", "", m.group(2)).strip())
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

            # Granby agenda hrefs have "?html=true" suffix; allow optional query string
            agenda_m = re.search(
                r'href="(/AgendaCenter/ViewFile/Agenda/(_\d{8}-\d+))[^"]*"',
                row_html,
            )
            if not agenda_m:
                continue
            agenda_base = agenda_m.group(1)   # /AgendaCenter/ViewFile/Agenda/_MMDDYYYY-id
            date_id_str = agenda_m.group(2)   # _MMDDYYYY-id

            dm = _DATE_ID_RE.match(date_id_str)
            if not dm:
                continue
            mm, dd, yyyy, meeting_id = dm.groups()
            try:
                meeting_date = datetime.date(int(yyyy), int(mm), int(dd))
            except ValueError:
                continue

            # Agenda packet — present when "?packet=true" link exists in the row
            packet_url = None
            if "packet=true" in row_html:
                packet_url = BASE_URL + agenda_base + "?packet=true"

            # Minutes — present when <td class="minutes"> contains a ViewFile link
            minutes_url = None
            minutes_td = re.search(
                r'<td[^>]+class="minutes"[^>]*>(.*?)</td>', row_html, re.DOTALL
            )
            if minutes_td and "ViewFile/Minutes" in minutes_td.group(1):
                min_m = re.search(
                    r'href="(/AgendaCenter/ViewFile/Minutes/[^"]+)"',
                    minutes_td.group(1),
                )
                if min_m:
                    minutes_url = BASE_URL + min_m.group(1)

            # GCTV16 recording — present in <td class="media"> when posted
            gctv_url = None
            media_td = re.search(
                r'<td[^>]+class="media"[^>]*>(.*?)</td>', row_html, re.DOTALL
            )
            if media_td:
                gctv_m = re.search(
                    r'href="(https://gctv16\.org/[^"]+)"', media_td.group(1)
                )
                if gctv_m:
                    gctv_url = gctv_m.group(1)

            meetings.append({
                "board": board,
                "meeting_date": meeting_date,
                "meeting_id": meeting_id,
                "agenda_url": BASE_URL + agenda_base,
                "packet_url": packet_url,
                "minutes_url": minutes_url,
                "gctv_url": gctv_url,
            })

    return meetings


# --- Utilities ---

def slugify(text, max_len=55):
    text = str(text).lower().strip()
    text = re.sub(r"[/\\&]", "-", text)
    text = re.sub(r"[^\w\s-]", "", text)
    text = re.sub(r"[\s_]+", "-", text)
    text = re.sub(r"-{2,}", "-", text)
    return text.strip("-")[:max_len]


def make_doc_path(board, doc_type, meeting_date, meeting_id, output_dir):
    """Return the local file path for a downloaded agenda/minutes/packet PDF."""
    date_str = meeting_date.strftime("%Y-%m-%d")
    month_dir = os.path.join(output_dir, meeting_date.strftime("%Y-%m"))
    os.makedirs(month_dir, exist_ok=True)
    board_slug = slugify(board, max_len=40)
    type_slug = doc_type       # "agenda", "packet", or "minutes"
    fname = f"{date_str}-{board_slug}-{type_slug}-{meeting_id}.pdf"
    return os.path.join(month_dir, fname)


def make_recording_path(board, meeting_date, gctv_slug, output_dir):
    """Return the yt-dlp output template for a GCTV16 recording."""
    date_str = meeting_date.strftime("%Y-%m-%d")
    month_dir = os.path.join(output_dir, meeting_date.strftime("%Y-%m"))
    os.makedirs(month_dir, exist_ok=True)
    board_slug = slugify(board, max_len=40)
    slug = slugify(gctv_slug, max_len=30)
    return os.path.join(month_dir, f"{date_str}-{board_slug}-{slug}.%(ext)s")


def download_recording(gctv_url, board, meeting_date, output_dir, archive_path):
    """
    Download a GCTV16 recording via yt-dlp.
    GCTV16 pages embed YouTube videos; yt-dlp extracts the YouTube video
    and records it in archive_path as "youtube {video_id}".
    Returns 'downloaded', 'skipped', or 'failed'.
    """
    gctv_slug = gctv_url.rstrip("/").split("/")[-1]
    outtmpl = make_recording_path(board, meeting_date, gctv_slug, output_dir)

    cmd = [
        "yt-dlp", "--js-runtimes", "node",
        "--no-playlist",
        "--merge-output-format", "mp4",
        "--download-archive", archive_path,
        "-o", outtmpl,
        "-q", "--no-warnings",
        gctv_url,
    ]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=3600)
        if result.returncode == 0:
            stderr = result.stderr.strip()
            if "has already been recorded" in stderr:
                return "skipped"
            return "downloaded"
        stderr = result.stderr.strip()
        if "has already been recorded" in stderr:
            return "skipped"
        return "failed"
    except subprocess.TimeoutExpired:
        print(f"  WARNING: yt-dlp timed out for {gctv_url}", file=sys.stderr)
        return "failed"
    except FileNotFoundError:
        print(
            "  ERROR: yt-dlp not found. Install with: pip install yt-dlp",
            file=sys.stderr,
        )
        return "failed"


# --- Main ---

def main():
    parser = argparse.ArgumentParser(
        description=(
            "Download Granby CT municipal agendas, minutes, and GCTV16 recordings "
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
        help="Skip minutes, download agendas (and packets) only",
    )
    parser.add_argument(
        "--no-agendas", action="store_true",
        help="Skip agendas and packets, download minutes only",
    )
    parser.add_argument(
        "--docs-only", action="store_true",
        help="Download PDFs only, skip recordings",
    )
    parser.add_argument(
        "--recordings-only", action="store_true",
        help="Download recordings only, skip PDFs",
    )
    args = parser.parse_args()

    now = datetime.datetime.now()
    if (now.weekday() == 5 and now.hour >= 18) or (now.weekday() == 6 and now.hour < 12):  # Saturday night, Sunday morning
        print("Skipping — no downloads on Saturday nights or Sunday mornings.")
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

    # --- Discover category IDs ---
    print("Fetching hub page to discover board categories...")
    hub_html = fetch_html(HUB_URL)
    if not hub_html:
        print("FATAL: Could not fetch hub page.", file=sys.stderr)
        sys.exit(1)
    cat_ids = parse_category_ids(hub_html)
    print(f"  Found {len(cat_ids)} board category/categories.\n")

    # --- Search for meetings in date window ---
    print("Searching for meetings in date window...")
    search_params = {
        "term": "",
        "CIDs": ",".join(cat_ids),
        "startDate": cutoff.strftime("%-m/%-d/%Y"),
        "endDate": future_limit.strftime("%-m/%-d/%Y"),
        "dateRange": "custom",
        "dateSelector": "0",
    }
    search_html = fetch_html(SEARCH_URL, params=search_params)
    if not search_html:
        print("FATAL: Could not fetch search results.", file=sys.stderr)
        sys.exit(1)

    meetings = parse_meetings(search_html)

    if board_filter:
        meetings = [m for m in meetings if board_filter in m["board"].lower()]

    print(f"  Found {len(meetings)} meeting(s) with documents in date window.\n")

    # --- Build downloadable items ---
    all_docs = []     # {meeting, doc_type, url}
    all_recordings = []   # {meeting, gctv_url}

    for m in meetings:
        if not args.recordings_only:
            if not args.no_agendas:
                all_docs.append({"meeting": m, "doc_type": "agenda", "url": m["agenda_url"]})
                if m["packet_url"]:
                    all_docs.append({"meeting": m, "doc_type": "packet", "url": m["packet_url"]})
            if not args.no_minutes and m["minutes_url"]:
                all_docs.append({"meeting": m, "doc_type": "minutes", "url": m["minutes_url"]})

        if not args.docs_only and m["gctv_url"]:
            all_recordings.append({"meeting": m, "gctv_url": m["gctv_url"]})

    # Deduplicate recordings by URL
    seen_gctv = set()
    unique_recordings = []
    for r in all_recordings:
        if r["gctv_url"] not in seen_gctv:
            seen_gctv.add(r["gctv_url"])
            unique_recordings.append(r)
    all_recordings = unique_recordings

    if not all_docs and not all_recordings:
        print("No items found in the date window.")
        return

    # --- Dry-run listing ---
    if args.dry_run:
        if all_docs:
            print(f"{'Board':<40} {'Date':<12} {'Meeting ID':<11} Type")
            print("-" * 80)
            for d in all_docs:
                m = d["meeting"]
                print(
                    f"{m['board'][:39]:<40} "
                    f"{m['meeting_date']!s:<12} "
                    f"{m['meeting_id']:<11} "
                    f"{d['doc_type']}"
                )
            print()

        if all_recordings:
            print(f"{'Board':<40} {'Date':<12} GCTV16 URL")
            print("-" * 80)
            for r in all_recordings:
                m = r["meeting"]
                slug = r["gctv_url"].rstrip("/").split("/")[-1][:40]
                print(
                    f"{m['board'][:39]:<40} "
                    f"{m['meeting_date']!s:<12} "
                    f"{slug}"
                )
            print()

        total = len(all_docs) + len(all_recordings)
        print(f"{total} item(s) matched. Re-run without --dry-run to download.")
        return

    # --- Download PDFs ---
    os.makedirs(args.output_dir, exist_ok=True)
    log_path = os.path.join(args.output_dir, "download-log.txt")
    log_lines = []
    downloaded = skipped = failed = 0

    if all_docs:
        print(f"Downloading {len(all_docs)} document(s)...")
        for d in all_docs:
            m = d["meeting"]
            dest = make_doc_path(
                m["board"], d["doc_type"], m["meeting_date"],
                m["meeting_id"], args.output_dir,
            )
            label = os.path.basename(dest)

            if os.path.exists(dest):
                print(f"  skip (exists)  {label}")
                skipped += 1
                continue

            print(f"  [{m['meeting_date']}] {m['board'][:45]} — {d['doc_type']}")
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

    # --- Download recordings ---
    if all_recordings:
        archive_path = os.path.join(args.output_dir, "media-archive.txt")
        print(f"Downloading {len(all_recordings)} recording(s) via yt-dlp...")
        for r in all_recordings:
            m = r["meeting"]
            slug = r["gctv_url"].rstrip("/").split("/")[-1][:45]
            print(f"  [{m['meeting_date']}] {m['board'][:45]}")
            print(f"  gctv16         {slug}")

            status = download_recording(
                r["gctv_url"], m["board"], m["meeting_date"],
                args.output_dir, archive_path,
            )
            if status == "downloaded":
                downloaded += 1
                log_lines.append(
                    f"{datetime.datetime.now().isoformat()}  OK       {r['gctv_url']}"
                )
            elif status == "skipped":
                skipped += 1
            else:
                failed += 1
                log_lines.append(
                    f"{datetime.datetime.now().isoformat()}  FAILED   {r['gctv_url']}"
                )
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
#    python3 scripts/download-granby-agendas.py --dry-run
#
# 2. Narrow to one board:
#    python3 scripts/download-granby-agendas.py --board "Board of Selectmen"
#
# 3. PDFs only (no recording downloads):
#    python3 scripts/download-granby-agendas.py --docs-only
#
# 4. Recordings only:
#    python3 scripts/download-granby-agendas.py --recordings-only
#
# 5. Agendas and packets only (skip minutes):
#    python3 scripts/download-granby-agendas.py --no-minutes
#
# 6. Change the lookback window:
#    python3 scripts/download-granby-agendas.py --days 14
#
# 7. Run on a schedule (cron — 7 AM daily):
#    0 7 * * * cd /path/to/repo && python3 scripts/download-granby-agendas.py
#
# NOTES:
#   - The Granby AgendaCenter uses a GET search (not POST). The script uses
#     "CIDs=all" equivalent by dynamically discovering category IDs from the
#     hub page and passing them to the search endpoint.
#   - Agenda rows may include an agenda packet link (?packet=true). These are
#     downloaded as separate files with "-packet-" in the filename.
#   - GCTV16 recording links appear inline in meeting rows when the recording
#     has been posted. yt-dlp detects the embedded YouTube video automatically.
#     The media-archive.txt file uses yt-dlp's native "youtube {id}" format to
#     prevent re-downloading on subsequent runs.
#   - The Town also has a YouTube channel (@TownofGranby, channel ID
#     UC0DA5cjynK1oLOeTNLtwVoQ) but it is used sporadically — mostly for
#     graduation ceremonies and town events, not meeting recordings. GCTV16
#     is the authoritative source for meeting recordings.
#   - The search endpoint returns up to ~100 rows in a single response.
#     No pagination has been observed for typical date windows.
