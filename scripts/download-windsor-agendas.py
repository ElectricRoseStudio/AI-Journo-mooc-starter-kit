#!/usr/bin/env python3
# download-windsor-agendas.py
# Download Windsor CT municipal meeting agendas, minutes, and Granicus MP4
# recordings for meetings whose date falls within the past N days (and up to
# 7 days ahead).
#
# USAGE:
#   python3 scripts/download-windsor-agendas.py [options]
#
# REQUIREMENTS:
#   - Python 3.6+  (no third-party packages needed)
#   - Internet connection
#   - Disk space: Granicus MP4 recordings can be 1–3 GB each
#
# WHAT IT DOES:
#   1. Calls the CivicPlus AgendaCenter Search endpoint with CIDs=all and the
#      date window — returns all matching rows in a single HTML response
#   2. Parses meeting rows for agenda and minutes ViewFile URLs
#   3. Downloads matching PDFs to beat-archive/windsor-agendas/YYYY-MM/
#   4. Fetches both Granicus ViewPublisher pages:
#        windsorct.granicus.com  view_id=1  Board of Education (96 clips, 2022+)
#        windsor-ct.granicus.com view_id=2  Town Council + Planning & Zoning, etc.
#   5. For each clip in the date window, fetches MediaPlayer.php to extract the
#      archive UUID, then downloads the MP4 from archive-video.granicus.com
#   6. Appends a download log to beat-archive/windsor-agendas/download-log.txt
#
# SITE STRUCTURE:
#   AgendaCenter (CivicPlus CivicEngage):
#     Base:    https://www.windsorct.gov
#     Search:  GET /AgendaCenter/Search/
#                ?term=&CIDs=all&startDate=MM%2FDD%2FYYYY
#                &endDate=MM%2FDD%2FYYYY&dateRange=custom&dateSelector=between
#     Agenda:  /AgendaCenter/ViewFile/Agenda/_{MMDDYYYY}-{meetingID}
#     Minutes: /AgendaCenter/ViewFile/Minutes/_{MMDDYYYY}-{meetingID}
#
#   Granicus video archive:
#     BOE listing:    https://windsorct.granicus.com/ViewPublisher.php?view_id=1
#       channel "windsorct" — Board of Education only (2022+)
#     All-boards:     https://windsor-ct.granicus.com/ViewPublisher.php?view_id=2
#       channel "windsor-ct" — Town Council, TPZ, and others (2010+)
#     MediaPlayer:    https://{subdomain}/MediaPlayer.php?view_id=N&clip_id=N
#       Contains HLS URL with UUID:
#         archive-stream.granicus.com/.../mp4:archive/{channel}/{channel}_{UUID}.mp4/...
#     Direct MP4:     https://archive-video.granicus.com/{channel}/{channel}_{UUID}.mp4
#
# BOARDS (26, as of 2026-05):
#   Arts Commission, Board of Assessment Appeals, Board of Ethics,
#   Capital Improvements Committee, Commission on Aging & Persons with Disabilities,
#   Conservation Commission, Economic Development Commission, Fair Rent Commission,
#   Finance Committee, Health & Safety Committee, Historic District Commission,
#   Human Relations Commission, Inland Wetlands & Watercourses Commission,
#   Joint Town Council / Board of Education Committee, Library Advisory Board,
#   Personnel Committee, POCD Advisory Committee, Public Building Commission,
#   Special Town Meeting, Town Council, Town Improvements Committee,
#   Town Planning & Zoning Commission, Wilson / Deerfield Advisory Committee,
#   Windsor Housing Authority, Youth Commission, Zoning Board of Appeals

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
BASE_URL = "https://www.windsorct.gov"
SEARCH_URL = f"{BASE_URL}/AgendaCenter/Search/"
OUTPUT_DIR = "beat-archive/windsor-agendas"
DAYS_BACK = 4
DAYS_AHEAD = 7
DELAY_SECONDS = 0.5
CHUNK_SIZE = 1024 * 1024   # 1 MB chunks for video downloads

UA = "Mozilla/5.0 (X11; Linux x86_64; rv:120.0) Gecko/20100101 Firefox/120.0"

# Each entry: subdomain, view_id, archive channel slug, human label
GRANICUS_SOURCES = [
    ("windsorct.granicus.com",  1, "windsorct",  "BOE"),
    ("windsor-ct.granicus.com", 2, "windsor-ct", "All boards"),
]

# Parses _MMDDYYYY-meetingID from /AgendaCenter/ViewFile/... paths
_DATE_ID_RE = re.compile(r'_(\d{2})(\d{2})(\d{4})-(\d+)$')

# Extracts UUID from Granicus HLS/archive URL embedded in MediaPlayer.php
_UUID_RE = re.compile(
    r'archive[^"\']*mp4:archive/[^/]+/[^_/]+_'
    r'([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})\.mp4',
    re.IGNORECASE,
)


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
    """Chunked download for large MP4 files. Returns True on success."""
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


# --- AgendaCenter parsing ---

def parse_meetings(search_html):
    """
    Parse meeting rows from the AgendaCenter Search results page.

    Returns a list of dicts:
      {board, meeting_date, meeting_id, agenda_url, minutes_url}
    """
    # Build board name map from id="catN" sections
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


# --- Granicus recording parsing ---

def parse_granicus_recordings(listing_html, cutoff, future_limit, channel, subdomain, view_id):
    """
    Parse meeting recordings from a Granicus ViewPublisher page.

    Returns a list of dicts:
      {name, meeting_date, clip_id, channel, subdomain, view_id}
    filtered to the date window. Rows without clip_ids (no recording yet) are skipped.
    """
    recordings = []

    for row_m in re.finditer(
        r'<tr[^>]+listingRow[^>]*>(.*?)</tr>', listing_html, re.DOTALL
    ):
        row = row_m.group(1)

        clip_m = re.search(r'clip_id=(\d+)', row)
        if not clip_m:
            continue
        clip_id = clip_m.group(1)

        # Meeting name from scope="row" td
        name_m = re.search(r'scope="row"[^>]*>\s*(.*?)\s*</td>', row, re.DOTALL)
        if not name_m:
            continue
        name = html_module.unescape(
            re.sub(r'<[^>]+>', '', name_m.group(1))
        ).strip()

        # Date from headers="Date ..." td
        date_m = re.search(r'headers="Date[^"]*"[^>]*>(.*?)</td>', row, re.DOTALL)
        if not date_m:
            continue
        raw = html_module.unescape(re.sub(r'<[^>]+>', '', date_m.group(1)))
        raw = raw.replace('\xa0', ' ')
        raw = re.sub(r'\s+', ' ', raw).strip()
        date_str = raw.split('-')[0].strip()
        try:
            meeting_date = datetime.datetime.strptime(date_str, "%b %d, %Y").date()
        except ValueError:
            try:
                meeting_date = datetime.datetime.strptime(date_str, "%B %d, %Y").date()
            except ValueError:
                continue

        if not (cutoff <= meeting_date <= future_limit):
            continue

        recordings.append({
            "name": name,
            "meeting_date": meeting_date,
            "clip_id": clip_id,
            "channel": channel,
            "subdomain": subdomain,
            "view_id": view_id,
        })

    return recordings


def resolve_granicus_mp4(subdomain, view_id, clip_id, channel):
    """
    Fetch MediaPlayer.php for a clip and return the direct MP4 URL, or None.
    The MediaPlayer page embeds an HLS URL containing the archive UUID:
      archive-stream.granicus.com/.../mp4:archive/{channel}/{channel}_{UUID}.mp4/...
    We extract the UUID and return the direct download URL:
      https://archive-video.granicus.com/{channel}/{channel}_{UUID}.mp4
    """
    url = f"https://{subdomain}/MediaPlayer.php?view_id={view_id}&clip_id={clip_id}"
    html = fetch_html(url)
    if not html:
        return None
    m = _UUID_RE.search(html)
    if not m:
        return None
    uuid = m.group(1)
    return f"https://archive-video.granicus.com/{channel}/{channel}_{uuid}.mp4"


# --- Utilities ---

def slugify(text, max_len=55):
    text = text.lower().strip()
    text = re.sub(r"[/\\&]", "-", text)
    text = re.sub(r"[^\w\s-]", "", text)
    text = re.sub(r"[\s_]+", "-", text)
    text = re.sub(r"-{2,}", "-", text)
    return text.strip("-")[:max_len]


def make_doc_path(board, doc_type, meeting_date, meeting_id, output_dir):
    month_dir = os.path.join(output_dir, meeting_date.strftime("%Y-%m"))
    os.makedirs(month_dir, exist_ok=True)
    date_str = meeting_date.strftime("%Y-%m-%d")
    board_slug = slugify(board, max_len=40)
    return os.path.join(month_dir, f"{date_str}-{board_slug}-{doc_type}-{meeting_id}.pdf")


def make_recording_path(name, meeting_date, clip_id, output_dir):
    month_dir = os.path.join(output_dir, meeting_date.strftime("%Y-%m"))
    os.makedirs(month_dir, exist_ok=True)
    date_str = meeting_date.strftime("%Y-%m-%d")
    board_slug = slugify(name, max_len=40)
    return os.path.join(month_dir, f"{date_str}-{board_slug}-{clip_id}.mp4")


# --- Media archive helpers ---

def is_in_archive(archive_path, clip_id):
    if not os.path.exists(archive_path):
        return False
    with open(archive_path) as f:
        return str(clip_id) in {line.strip() for line in f}


def add_to_archive(archive_path, clip_id):
    with open(archive_path, "a") as f:
        f.write(f"{clip_id}\n")


# --- Main ---

def main():
    parser = argparse.ArgumentParser(
        description=(
            "Download Windsor CT municipal agendas, minutes, and Granicus "
            "recordings for meetings within the past N days (and up to 7 ahead)."
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
    parser.add_argument(
        "--docs-only", action="store_true",
        help="Download PDFs only, skip recordings",
    )
    parser.add_argument(
        "--recordings-only", action="store_true",
        help="Download recordings only, skip PDFs",
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
    print(f"Site        : {BASE_URL}")
    if not args.dry_run:
        print(f"Output dir  : {args.output_dir}")
    print()

    # --- Collect documents ---
    all_docs = []

    if not args.recordings_only:
        print("Fetching AgendaCenter Search results...")
        search_html = fetch_html(SEARCH_URL, params={
            "term": "",
            "CIDs": "all",
            "startDate": cutoff.strftime("%m/%d/%Y"),
            "endDate": future_limit.strftime("%m/%d/%Y"),
            "dateRange": "custom",
            "dateSelector": "between",
        })
        if not search_html:
            print("FATAL: Could not fetch AgendaCenter Search.", file=sys.stderr)
            sys.exit(1)

        all_meetings = parse_meetings(search_html)
        if board_filter:
            all_meetings = [m for m in all_meetings if board_filter in m["board"].lower()]

        for m in all_meetings:
            if not args.no_agendas and m["agenda_url"]:
                all_docs.append({
                    "board": m["board"],
                    "meeting_date": m["meeting_date"],
                    "doc_type": "agenda",
                    "url": m["agenda_url"],
                    "meeting_id": m["meeting_id"],
                })
            if not args.no_minutes and m["minutes_url"]:
                all_docs.append({
                    "board": m["board"],
                    "meeting_date": m["meeting_date"],
                    "doc_type": "minutes",
                    "url": m["minutes_url"],
                    "meeting_id": m["meeting_id"],
                })

        print(f"  Found {len(all_meetings)} meeting(s), {len(all_docs)} document(s).\n")

    # --- Collect recordings ---
    all_recordings = []

    if not args.docs_only:
        for subdomain, view_id, channel, label in GRANICUS_SOURCES:
            listing_url = f"https://{subdomain}/ViewPublisher.php?view_id={view_id}"
            print(f"Fetching Granicus listing ({label}): {listing_url}")
            listing_html = fetch_html(listing_url)
            if not listing_html:
                print(f"  WARNING: Could not fetch {listing_url}", file=sys.stderr)
                continue

            clips = parse_granicus_recordings(
                listing_html, cutoff, future_limit, channel, subdomain, view_id,
            )
            if board_filter:
                clips = [c for c in clips if board_filter in c["name"].lower()]
            print(f"  {len(clips)} recording(s) in date window.")
            all_recordings.extend(clips)
            time.sleep(DELAY_SECONDS)

        print(f"\n  Total recordings: {len(all_recordings)}\n")

    if not all_docs and not all_recordings:
        print("No items found in the date window.")
        return

    # Sort both lists
    all_docs.sort(key=lambda x: (x["meeting_date"], x["board"]))
    all_recordings.sort(key=lambda x: (x["meeting_date"], x["name"]))

    # --- Dry-run listing ---
    if args.dry_run:
        if all_docs:
            print(f"{'Board':<42} {'Date':<12} {'Type':<8} ID")
            print("-" * 80)
            for d in all_docs:
                print(
                    f"{d['board'][:41]:<42} "
                    f"{d['meeting_date']!s:<12} "
                    f"{d['doc_type']:<8} "
                    f"{d['meeting_id']}"
                )
            print()

        if all_recordings:
            print(f"{'Board':<42} {'Date':<12} {'Clip':<8} Source")
            print("-" * 80)
            for v in all_recordings:
                src = "BOE" if v["view_id"] == 1 else "All"
                print(
                    f"{v['name'][:41]:<42} "
                    f"{v['meeting_date']!s:<12} "
                    f"{v['clip_id']:<8} "
                    f"{src}"
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
            dest = make_doc_path(
                d["board"], d["doc_type"], d["meeting_date"], d["meeting_id"], args.output_dir
            )
            label = os.path.basename(dest)

            if os.path.exists(dest):
                print(f"  skip (exists)  {label}")
                skipped += 1
                continue

            print(f"  [{d['meeting_date']}] {d['board'][:45]} — {d['doc_type']}")
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

    # --- Download Granicus recordings ---
    if all_recordings:
        archive_path = os.path.join(args.output_dir, "media-archive.txt")
        print(f"Downloading {len(all_recordings)} recording(s)...")

        for v in all_recordings:
            clip_id = v["clip_id"]

            if is_in_archive(archive_path, clip_id):
                print(f"  skip (archive) clip {clip_id}  {v['name'][:45]}")
                skipped += 1
                continue

            dest = make_recording_path(v["name"], v["meeting_date"], clip_id, args.output_dir)

            if os.path.exists(dest):
                print(f"  skip (exists)  {os.path.basename(dest)}")
                skipped += 1
                add_to_archive(archive_path, clip_id)
                continue

            print(f"  [{v['meeting_date']}] {v['name'][:45]} (clip {clip_id})")
            print(f"  resolving      MediaPlayer.php ...", end=" ", flush=True)

            mp4_url = resolve_granicus_mp4(v["subdomain"], v["view_id"], clip_id, v["channel"])
            if not mp4_url:
                print("no URL found", file=sys.stderr)
                failed += 1
                log_lines.append(
                    f"{datetime.datetime.now().isoformat()}  FAILED   clip {clip_id} {v['name']}"
                )
                time.sleep(DELAY_SECONDS)
                continue
            print("OK")
            print(f"  downloading    {os.path.basename(dest)}")

            if download_video(mp4_url, dest):
                downloaded += 1
                add_to_archive(archive_path, clip_id)
                log_lines.append(
                    f"{datetime.datetime.now().isoformat()}  OK       {dest}"
                )
            else:
                failed += 1
                log_lines.append(
                    f"{datetime.datetime.now().isoformat()}  FAILED   {mp4_url}"
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
#    python3 scripts/download-windsor-agendas.py --dry-run
#
# 2. Narrow to one board:
#    python3 scripts/download-windsor-agendas.py --board "Town Council"
#
# 3. PDFs only (no recording downloads):
#    python3 scripts/download-windsor-agendas.py --docs-only
#
# 4. Recordings only:
#    python3 scripts/download-windsor-agendas.py --recordings-only
#
# 5. Agendas only (skip minutes):
#    python3 scripts/download-windsor-agendas.py --no-minutes
#
# 6. Change the lookback window:
#    python3 scripts/download-windsor-agendas.py --days 14
#
# 7. Run on a schedule (cron — 7 AM daily):
#    0 7 * * * cd /path/to/repo && python3 scripts/download-windsor-agendas.py
#
# NOTES:
#   - Windsor uses CivicPlus CivicEngage at windsorct.gov. The Search endpoint
#     with CIDs=all returns all boards' documents for the date window in a single
#     HTML response — no per-board pagination needed.
#   - Windsor has two Granicus video subdomains:
#       windsorct.granicus.com  view_id=1 — Board of Education only, 96+ clips
#         since Feb 2022; all regular BOE meetings are recorded.
#       windsor-ct.granicus.com view_id=2 — Town Council and Town Planning &
#         Zoning (and occasional others), 798+ clips from Oct 2010 onward; both
#         boards have been actively recorded throughout 2025-2026.
#   - Granicus does not expose direct MP4 links in ViewPublisher. Instead, each
#     clip's MediaPlayer.php page embeds an HLS URL that contains the archive UUID:
#       archive-stream.granicus.com/.../mp4:archive/{channel}/{channel}_{UUID}.mp4/...
#     The UUID is extracted and used to build the direct MP4 download URL:
#       https://archive-video.granicus.com/{channel}/{channel}_{UUID}.mp4
#   - A media-archive.txt file tracks downloaded clips by clip_id to prevent
#     re-downloading on subsequent runs.
#   - Recording files are 1–3 GB each. The script downloads in 1 MB chunks and
#     prints progress dots. Use --docs-only to skip recordings.
