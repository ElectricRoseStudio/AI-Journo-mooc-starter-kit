#!/usr/bin/env python3
# download-enfield-agendas.py
# Download municipal meeting agendas, minutes, and A/V recordings from Enfield CT
# for meetings whose date falls within the past N days (and up to 7 days ahead).
#
# USAGE:
#   python3 scripts/download-enfield-agendas.py [options]
#
# REQUIREMENTS:
#   - Python 3.6+  (no third-party packages needed)
#   - Internet connection
#
# WHAT IT DOES:
#   1. Calls the CivicClerk OData API to fetch all events in the date window
#      (automatically follows @odata.nextLink pagination, 15 events per page)
#   2. For each event, collects publishedFiles (Agenda, Agenda Packet, Minutes,
#      Notice, etc.) and any associated audio/video recording
#   3. Downloads PDFs via the Meetings/GetMeetingFileStream endpoint
#   4. Downloads audio (MP3) and video (MP4) recordings from the CDN
#   5. Appends a download log to beat-archive/enfield-agendas/download-log.txt
#
# SITE STRUCTURE:
#   CMS: CivicClerk (enfieldct.portal.civicclerk.com)
#
#   Public portal: https://enfieldct.portal.civicclerk.com
#   OData API:     https://enfieldct.api.civicclerk.com/v1
#
#   Events endpoint:
#     GET /Events
#       ?$filter=eventDate ge {ISO_DATE}Z and eventDate le {ISO_DATE}Z
#       &$orderby=eventDate asc
#     Returns 15 events per page; follow @odata.nextLink until absent.
#
#   Each event contains:
#     - id, eventName, eventDate, categoryName
#     - publishedFiles: [{fileId, type, name, url, streamUrl, fileType}]
#       type values: "Agenda", "Agenda Packet", "Minutes", "Notice"
#     - mediaStreamPath: "stream/ENFIELDCT/{uuid}.{ext}" or full URL
#       (empty string if no recording)
#     - mediaTypeId: 1=video (mp4), 2=audio (mp3)
#     - mediaOrigFileName: original filename
#     - hasMedia: bool
#
#   Document download:
#     GET /v1/Meetings/GetMeetingFileStream(fileId={id},plainText=false)
#     (no authentication required)
#
#   Recording download:
#     https://cpmedia.azureedge.net/enfieldct/{uuid}.{ext}
#     (extract {uuid}.{ext} from the last path component of mediaStreamPath;
#      if mediaStreamPath is already a full URL, use it directly)
#
# BOARDS (51 categories):
#   Agricultural Commission, America250 Coordinating Committee,
#   Aquifer Protection Agency, Area 25 Cable TV Advisory Committee,
#   Blight Review Committee, Board of Assessment Appeals, Board of Education,
#   Charter Revision Commission, Commission on Aging, Council of Chairs,
#   Development Services Subcommittee, Diversity Equity and Inclusion Committee,
#   DPW Subcommittee, Economic Development Commission,
#   Enfield Athletic Hall of Fame, Enfield Beautification Committee,
#   Enfield Culture and Arts Commission, Ethics Commission,
#   Fair Rent Commission, General Government Subcommittee,
#   Historic District Commission, Housing Authority,
#   Inland Wetlands & Watercourses Agency, JFK Renovation Building Committee,
#   Joint Facilities Committee, Leisure Subcommittee, Library Board of Trustees,
#   Loan Review Committee, Patriot Award Subcommittee,
#   Plan of Conservation Development Steering Committee,
#   Planning & Zoning Commission, Planning and Zoning Public Hearings,
#   PK-5 Elementary Schools & Eagle Academy Pre-Referendum Committee,
#   Policy and Procedure Subcommittee, Prison / Town Liaison Committee,
#   Public Safety Subcommittee, Senior Tax Relief Committee,
#   Shaker Pines Fire Board of Commissioners, Social Services Subcommittee,
#   Tax Increment Financing Advisory Committee, TC/BOE Joint Insurance Subcommittee,
#   TC/BOE Joint IT Strategy Subcommittee, TC/BOE Security Committee,
#   Thompsonville Fire Commission, Town Council, Town Facilities Committee,
#   Water Pollution Control Authority, WPCA Subcommittee,
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
API_BASE = "https://enfieldct.api.civicclerk.com/v1"
CDN_BASE = "https://cpmedia.azureedge.net/enfieldct"
OUTPUT_DIR = "beat-archive/enfield-agendas"
DAYS_BACK = 4
DAYS_AHEAD = 7
API_DELAY = 0.25   # seconds between paginated API calls

UA = "Mozilla/5.0 (X11; Linux x86_64; rv:120.0) Gecko/20100101 Firefox/120.0"

# File types to download (case-sensitive, matches the API's "type" field).
# All known types are included; add new ones here if CivicClerk adds more.
DOWNLOAD_TYPES = {"Agenda", "Agenda Packet", "Minutes", "Notice"}


# --- HTTP helpers ---

def fetch_json(url):
    """GET url and return parsed JSON dict, or raise on error."""
    req = urllib.request.Request(
        url,
        headers={"User-Agent": UA, "Accept": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            import json
            return json.load(r)
    except urllib.error.HTTPError as e:
        print(f"  HTTP {e.code} — {url}", file=sys.stderr)
        raise
    except Exception as e:
        print(f"  ERROR fetching {url}: {e}", file=sys.stderr)
        raise


def download_file(url, dest_path):
    """Download url to dest_path. Returns True on success."""
    req = urllib.request.Request(
        url,
        headers={"User-Agent": UA, "Accept": "*/*"},
    )
    try:
        with urllib.request.urlopen(req, timeout=120) as r:
            with open(dest_path, "wb") as f:
                f.write(r.read())
        return True
    except Exception as e:
        print(f"  WARNING: {e}", file=sys.stderr)
        return False


# --- API helpers ---

def fetch_events(cutoff, future_limit):
    """
    Fetch all events in the date window from the CivicClerk OData API.
    Follows @odata.nextLink pagination (15 events per page).
    """
    start_iso = cutoff.strftime("%Y-%m-%dT00:00:00Z")
    end_iso = future_limit.strftime("%Y-%m-%dT23:59:59Z")
    filter_expr = f"eventDate ge {start_iso} and eventDate le {end_iso}"
    params = urllib.parse.urlencode({
        "$filter": filter_expr,
        "$orderby": "eventDate asc",
    })
    url = f"{API_BASE}/Events?{params}"
    events = []
    page = 0
    while url:
        data = fetch_json(url)
        page_events = data.get("value", [])
        events.extend(page_events)
        url = data.get("@odata.nextLink")
        page += 1
        if url:
            time.sleep(API_DELAY)
    return events


def make_doc_url(file_id):
    """Return the document download URL for the given CivicClerk file ID."""
    return f"{API_BASE}/Meetings/GetMeetingFileStream(fileId={file_id},plainText=false)"


def make_media_url(stream_path):
    """
    Convert a mediaStreamPath to a downloadable CDN URL.

    Newer format: "stream/ENFIELDCT/{uuid}.ext"  → CDN_BASE/{uuid}.ext
    Older format: full "https://..." URL          → use as-is
    """
    if stream_path.startswith("http"):
        return stream_path
    filename = stream_path.split("/")[-1]
    return f"{CDN_BASE}/{filename}"


def parse_event_date(event):
    """Extract a datetime.date from an event's eventDate field (ISO 8601 UTC)."""
    date_str = event.get("eventDate", "")[:10]  # "YYYY-MM-DD"
    try:
        return datetime.datetime.strptime(date_str, "%Y-%m-%d").date()
    except ValueError:
        return None


# --- Utilities ---

def slugify(text, max_len=55):
    text = str(text).lower().strip()
    text = re.sub(r"[/\\&]", "-", text)
    text = re.sub(r"[^\w\s-]", "", text)
    text = re.sub(r"[\s_]+", "-", text)
    text = re.sub(r"-{2,}", "-", text)
    return text.strip("-")[:max_len]


def make_pdf_path(board, doc_type, event_date, event_name, output_dir):
    date_str = event_date.strftime("%Y-%m-%d")
    month_dir = os.path.join(output_dir, event_date.strftime("%Y-%m"))
    os.makedirs(month_dir, exist_ok=True)
    board_slug = slugify(board, max_len=35)
    name_slug = slugify(event_name, max_len=30)
    type_slug = slugify(doc_type, max_len=20)
    fname = f"{date_str}-{board_slug}-{name_slug}-{type_slug}.pdf"
    return os.path.join(month_dir, fname)


def make_media_path(board, event_date, event_name, stream_path, output_dir):
    date_str = event_date.strftime("%Y-%m-%d")
    month_dir = os.path.join(output_dir, event_date.strftime("%Y-%m"))
    os.makedirs(month_dir, exist_ok=True)
    board_slug = slugify(board, max_len=35)
    name_slug = slugify(event_name, max_len=25)
    # Preserve the original extension; extract UUID prefix for uniqueness
    filename = stream_path.split("/")[-1]
    ext = filename.rsplit(".", 1)[-1].lower() if "." in filename else "mp4"
    uuid_short = filename.split(".")[0][:12]
    fname = f"{date_str}-{board_slug}-{name_slug}-{uuid_short}.{ext}"
    return os.path.join(month_dir, fname)


def is_in_archive(archive_path, event_id):
    """Return True if event_id is already in the media download archive."""
    if not os.path.exists(archive_path):
        return False
    needle = str(event_id)
    with open(archive_path) as f:
        return any(needle == line.strip() for line in f)


def add_to_archive(archive_path, event_id):
    with open(archive_path, "a") as f:
        f.write(f"{event_id}\n")


# --- Main ---

def main():
    parser = argparse.ArgumentParser(
        description=(
            "Download Enfield CT municipal agendas, minutes, and A/V recordings "
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
        help="Only include boards/categories containing NAME (case-insensitive)",
    )
    parser.add_argument(
        "--no-minutes", action="store_true",
        help="Skip minutes, download agendas only",
    )
    parser.add_argument(
        "--no-agendas", action="store_true",
        help="Skip agendas (and packets), download minutes only",
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

    today = datetime.date.today()
    cutoff = today - datetime.timedelta(days=args.days)
    future_limit = today + datetime.timedelta(days=args.ahead)
    board_filter = args.board.lower() if args.board else None

    print(f"Date window : {cutoff} to {future_limit}")
    if not args.dry_run:
        print(f"Output dir  : {args.output_dir}")
    print()

    # --- Fetch events ---
    print("Fetching events from CivicClerk API...")
    try:
        all_events = fetch_events(cutoff, future_limit)
    except Exception as e:
        print(f"FATAL: Could not fetch events: {e}", file=sys.stderr)
        sys.exit(1)
    print(f"  {len(all_events)} event(s) in window\n")

    # --- Collect downloadable items ---
    all_docs = []    # {board, event_date, event_name, doc_type, file_id, file_name}
    all_media = []   # {board, event_date, event_name, event_id, stream_path, media_type}

    for event in all_events:
        board = event.get("categoryName", "Unknown Board")
        if board_filter and board_filter not in board.lower():
            continue

        event_date = parse_event_date(event)
        if not event_date:
            continue

        event_name = event.get("eventName", "Meeting")

        # Collect documents
        if not args.recordings_only:
            for f in event.get("publishedFiles", []):
                doc_type = f.get("type", "")
                if doc_type not in DOWNLOAD_TYPES:
                    continue
                if args.no_minutes and doc_type == "Minutes":
                    continue
                if args.no_agendas and doc_type in {"Agenda", "Agenda Packet"}:
                    continue
                file_id = f.get("fileId")
                if not file_id:
                    continue
                all_docs.append({
                    "board": board,
                    "event_date": event_date,
                    "event_name": event_name,
                    "doc_type": doc_type,
                    "file_id": file_id,
                    "file_name": f.get("name", ""),
                })

        # Collect media recordings
        if not args.docs_only:
            stream_path = event.get("mediaStreamPath", "").strip()
            if stream_path:
                all_media.append({
                    "board": board,
                    "event_date": event_date,
                    "event_name": event_name,
                    "event_id": event.get("id"),
                    "stream_path": stream_path,
                    "media_type": "audio" if event.get("mediaTypeId") == 2 else "video",
                    "orig_name": event.get("mediaOrigFileName", ""),
                })

    # Sort by date desc for display
    all_docs.sort(key=lambda x: (x["event_date"], x["board"]), reverse=True)
    all_media.sort(key=lambda x: (x["event_date"], x["board"]), reverse=True)

    print(f"  {len(all_docs)} document(s) matched")
    print(f"  {len(all_media)} recording(s) matched")
    print()

    if not all_docs and not all_media:
        print("No items found in the date window.")
        return

    # --- Dry-run listing ---
    if args.dry_run:
        if all_docs:
            print(f"{'Board':<40} {'Date':<12} {'Meeting':<30} Type")
            print("-" * 95)
            for d in all_docs:
                print(
                    f"{d['board'][:39]:<40} "
                    f"{d['event_date']!s:<12} "
                    f"{d['event_name'][:29]:<30} "
                    f"{d['doc_type']}"
                )
            print()
        if all_media:
            print(f"{'Board':<40} {'Date':<12} {'Meeting':<30} Format")
            print("-" * 95)
            for m in all_media:
                fmt = m["media_type"]
                orig = m["orig_name"][:30] if m["orig_name"] else ""
                print(
                    f"{m['board'][:39]:<40} "
                    f"{m['event_date']!s:<12} "
                    f"{m['event_name'][:29]:<30} "
                    f"{fmt}  {orig}"
                )
            print()
        total = len(all_docs) + len(all_media)
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
            dest = make_pdf_path(
                d["board"], d["doc_type"], d["event_date"],
                d["event_name"], args.output_dir,
            )
            label = os.path.basename(dest)

            if os.path.exists(dest):
                print(f"  skip (exists)  {label}")
                skipped += 1
                continue

            url = make_doc_url(d["file_id"])
            print(f"  [{d['event_date']}] {d['board'][:45]} — {d['doc_type']}")
            print(f"  downloading    {label}")
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
            time.sleep(API_DELAY)
        print()

    # --- Download recordings ---
    if all_media:
        archive_path = os.path.join(args.output_dir, "media-archive.txt")
        print(f"Downloading {len(all_media)} recording(s)...")
        for m in all_media:
            eid = m["event_id"]
            if is_in_archive(archive_path, eid):
                print(f"  skip (archive) event {eid}  {m['board'][:45]}")
                skipped += 1
                continue

            url = make_media_url(m["stream_path"])
            dest = make_media_path(
                m["board"], m["event_date"], m["event_name"],
                m["stream_path"], args.output_dir,
            )
            label = os.path.basename(dest)
            ext = label.rsplit(".", 1)[-1].upper() if "." in label else "?"
            print(
                f"  [{m['event_date']}] {m['board'][:45]} — "
                f"{m['event_name'][:40]}  [{ext}]"
            )
            print(f"  downloading    {label}")

            if download_file(url, dest):
                downloaded += 1
                add_to_archive(archive_path, eid)
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
#    python3 scripts/download-enfield-agendas.py --dry-run
#
# 2. Narrow to one board:
#    python3 scripts/download-enfield-agendas.py --board "Town Council"
#
# 3. PDFs only (no recording downloads):
#    python3 scripts/download-enfield-agendas.py --docs-only
#
# 4. Recordings only:
#    python3 scripts/download-enfield-agendas.py --recordings-only
#
# 5. Agendas only (skip minutes):
#    python3 scripts/download-enfield-agendas.py --no-minutes
#
# 6. Change the lookback window:
#    python3 scripts/download-enfield-agendas.py --days 14
#
# 7. Run on a schedule (cron — 7 AM daily):
#    0 7 * * * cd /path/to/repo && python3 scripts/download-enfield-agendas.py
#
# NOTES:
#   - Enfield uses CivicClerk (not the town website's CivicPlus AgendaCenter,
#     which is empty). The CivicClerk portal is at:
#       https://enfieldct.portal.civicclerk.com
#     The API is public and requires no authentication token.
#   - The API returns 15 events per page. The script follows @odata.nextLink
#     automatically until all pages are fetched.
#   - File types in publishedFiles: "Agenda", "Agenda Packet", "Minutes",
#     "Notice". All are downloaded by default.
#   - Recordings are audio (MP3) or video (MP4) hosted on Azure CDN at
#       https://cpmedia.azureedge.net/enfieldct/{uuid}.{ext}
#     The {uuid}.{ext} is extracted from the last component of mediaStreamPath.
#   - A media-archive.txt file tracks downloaded recordings by CivicClerk
#     event ID so they are not re-downloaded on subsequent runs.
#   - The Town Council also posts agendas at a separate CivicPlus page
#     (https://www.enfield-ct.gov/1378/Town-Council-Agendas-Minutes), but
#     those same agendas appear in CivicClerk too, so no separate scrape needed.
