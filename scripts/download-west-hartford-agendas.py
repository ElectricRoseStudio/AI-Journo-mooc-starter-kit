#!/usr/bin/env python3
# download-west-hartford-agendas.py
# Download municipal meeting agendas, minutes, and media links from West Hartford
# CT's CivicClerk portal for meetings whose date falls within a date window.
#
# USAGE:
#   python3 scripts/download-west-hartford-agendas.py [options]
#
# REQUIREMENTS:
#   - Python 3.6+  (no third-party packages needed)
#   - Internet connection
#   - Optional: yt-dlp (for downloading YouTube recordings)
#
# WHAT IT DOES:
#   1. Fetches the CivicClerk REST API to discover events in the date window
#   2. For each event with an agenda, fetches the meeting's published files
#   3. Downloads each file (agenda, agenda packet, minutes, attachments) by
#      resolving the CivicClerk blob storage URL and fetching the PDF
#   4. Records YouTube/external recording URLs in a media-log.txt file
#   5. Optionally downloads recordings via yt-dlp (--download-media flag)
#   6. Appends a download log to beat-archive/west-hartford-agendas/download-log.txt
#
# SITE STRUCTURE (CivicClerk):
#   Portal:   https://westhartfordct.portal.civicclerk.com/
#   API base: https://westhartfordct.api.civicclerk.com/v1
#
#   Events endpoint (OData):
#     GET /Events?$filter=eventDate ge {start}Z and eventDate le {end}Z
#     Returns: id, eventName, eventDate, categoryName, agendaId, externalMediaUrl
#
#   Categories endpoint:
#     GET /EventCategories
#
#   Meeting files:
#     GET /Meetings/{agendaId}
#     Returns: publishedFiles[{fileId, type, name, url}]
#
#   File download (two-step):
#     GET /Meetings/GetMeetingFile(fileId={id},plainText=false)
#     → returns {"blobUri": "https://civicclerk.blob.core.windows.net/..."}
#     GET {blobUri} → PDF content
#
#   Attachment download:
#     GET /Meetings/GetAttachmentFile(fileId={id})
#     → returns {"blobUri": "..."} → PDF content
#
#   Media/recordings:
#     externalMediaUrl field on Events (typically a YouTube URL)
#     Download requires yt-dlp (not bundled): https://github.com/yt-dlp/yt-dlp
#
# NOTE: The CivicClerk API requires no authentication — it is a public REST API.
# NOTE: Blob URIs contain time-limited SAS tokens (typically 7-day expiry).
# NOTE: The API uses OData v4 syntax for filtering and ordering.

import argparse
import datetime
import json
import os
import re
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

# --- Configuration ---
API_BASE = "https://westhartfordct.api.civicclerk.com/v1"
OUTPUT_DIR = "beat-archive/west-hartford-agendas"
DAYS_BACK = 4
DAYS_AHEAD = 7
PAGE_DELAY = 0.3
DOWNLOAD_DELAY = 0.8

UA = "WestHartford-Agendas-Downloader/1.0 (journalism research)"


# --- HTTP helpers ---

def _request(url, retries=2):
    req = urllib.request.Request(
        url,
        headers={"User-Agent": UA, "Accept": "application/json, */*"},
    )
    for attempt in range(retries + 1):
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                return r.read().decode("utf-8", errors="replace")
        except urllib.error.HTTPError as e:
            if e.code == 404:
                return None
            print(f"  HTTP {e.code} for {url}", file=sys.stderr)
            if attempt < retries:
                time.sleep(1.5)
        except urllib.error.URLError as e:
            print(f"  ERROR fetching {url}: {e}", file=sys.stderr)
            if attempt < retries:
                time.sleep(1.5)
    return None


def fetch_json(path, retries=2):
    """GET {API_BASE}/{path} and return parsed JSON dict, or None."""
    url = f"{API_BASE}/{path}" if not path.startswith("http") else path
    raw = _request(url, retries=retries)
    if raw is None:
        return None
    try:
        return json.loads(raw)
    except json.JSONDecodeError as e:
        print(f"  JSON decode error for {url}: {e}", file=sys.stderr)
        return None


def download_bytes(url, dest_path, retries=2):
    """Download binary content from url to dest_path. Returns True on success."""
    req = urllib.request.Request(
        url,
        headers={"User-Agent": UA, "Accept": "application/octet-stream, application/pdf, */*"},
    )
    for attempt in range(retries + 1):
        try:
            with urllib.request.urlopen(req, timeout=60) as r:
                data = r.read()
            with open(dest_path, "wb") as f:
                f.write(data)
            return True
        except Exception as e:
            print(f"  WARNING (attempt {attempt+1}): {e}", file=sys.stderr)
            if attempt < retries:
                time.sleep(1.5)
    return False


# --- CivicClerk API ---

def fetch_events(start_date, end_date):
    """
    Return list of event dicts from the CivicClerk API within [start_date, end_date].
    Uses OData date range filter.
    """
    start_str = start_date.strftime("%Y-%m-%dT00:00:00Z")
    end_str = end_date.strftime("%Y-%m-%dT23:59:59Z")
    filter_expr = f"eventDate ge {start_str} and eventDate le {end_str}"
    path = (
        "Events?"
        + urllib.parse.urlencode({
            "$filter": filter_expr,
            "$orderby": "eventDate desc",
            "$top": "500",
        })
    )
    data = fetch_json(path)
    if data is None:
        return []
    return data.get("value", [])


def fetch_categories():
    """Return dict mapping categoryId → categoryDesc."""
    data = fetch_json("EventCategories")
    if not data:
        return {}
    return {cat["id"]: cat["categoryDesc"] for cat in data.get("value", [])}


def fetch_meeting_files(agenda_id):
    """
    Return list of published file dicts for the given meeting agendaId.
    Each dict has: fileId, type, name, url
    """
    data = fetch_json(f"Meetings/{agenda_id}")
    if data is None:
        return []
    return data.get("publishedFiles", [])


def resolve_blob_uri(file_url):
    """
    Call the CivicClerk file URL and return the Azure Blob SAS URI for download.
    Returns None on failure.
    """
    raw = _request(file_url)
    if not raw:
        return None
    try:
        obj = json.loads(raw)
        return obj.get("blobUri")
    except json.JSONDecodeError:
        return None


# --- File naming ---

def slugify(text, max_len=55):
    text = re.sub(r"[&/\\]", "-", text.lower().strip())
    text = re.sub(r"\s+-\s+", "-", text)
    text = re.sub(r"[^\w\s-]", "", text)
    text = re.sub(r"[\s_]+", "-", text)
    text = re.sub(r"-{2,}", "-", text)
    return text.strip("-")[:max_len]


def make_dest_path(event, file_info, output_dir):
    """Return the full destination path for a downloaded file."""
    ev_date = datetime.datetime.fromisoformat(
        event["eventDate"].replace("Z", "+00:00")
    ).date()
    date_prefix = ev_date.strftime("%Y-%m-%d")
    month_dir = os.path.join(output_dir, ev_date.strftime("%Y-%m"))
    os.makedirs(month_dir, exist_ok=True)

    board_slug = slugify(event.get("categoryName", "unknown"))
    doc_type_slug = slugify(file_info.get("type", "doc"))
    # Determine extension from URL or default to pdf
    url = file_info.get("url", "")
    ext = ".pdf"
    return os.path.join(month_dir, f"{date_prefix}-{board_slug}-{doc_type_slug}{ext}")


def dedupe_dest(dest, seen_dests):
    """Append a counter suffix if dest already exists in seen_dests."""
    if dest not in seen_dests:
        seen_dests.add(dest)
        return dest
    stem, ext = os.path.splitext(dest)
    counter = 2
    while True:
        candidate = f"{stem}-{counter}{ext}"
        if candidate not in seen_dests:
            seen_dests.add(candidate)
            return candidate
        counter += 1


# --- Media handling ---

def download_with_ytdlp(url, output_dir, event_date, board_slug):
    """
    Attempt to download a video URL using yt-dlp.
    Returns True if successful, False otherwise.
    """
    if not shutil.which("yt-dlp"):
        return False
    date_str = event_date.strftime("%Y-%m-%d")
    month_dir = os.path.join(output_dir, event_date.strftime("%Y-%m"))
    os.makedirs(month_dir, exist_ok=True)
    out_template = os.path.join(month_dir, f"{date_str}-{board_slug}-recording.%(ext)s")
    cmd = [
        "yt-dlp",
        "--quiet",
        "--no-warnings",
        "--output", out_template,
        url,
    ]
    try:
        result = subprocess.run(cmd, timeout=300, capture_output=True)
        return result.returncode == 0
    except (subprocess.TimeoutExpired, FileNotFoundError):
        return False


# --- Main ---

def main():
    parser = argparse.ArgumentParser(
        description="Download West Hartford CT municipal agendas and minutes via CivicClerk API.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
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
        help="List matching events and files without downloading",
    )
    parser.add_argument(
        "--board", metavar="NAME",
        help="Only process boards whose name contains NAME (case-insensitive)",
    )
    parser.add_argument(
        "--no-minutes", action="store_true",
        help="Skip minutes files",
    )
    parser.add_argument(
        "--no-agendas", action="store_true",
        help="Skip agenda and agenda packet files",
    )
    parser.add_argument(
        "--download-media", action="store_true",
        help="Download video recordings via yt-dlp (requires yt-dlp in PATH)",
    )
    parser.add_argument(
        "--all", action="store_true",
        help="Download all events regardless of date (overrides --days/--ahead)",
    )
    args = parser.parse_args()

    if datetime.date.today().weekday() in (6, 0):  # Sunday, Monday
        print("Skipping — no downloads on Sunday or Monday.")
        sys.exit(0)

    today = datetime.date.today()
    if args.all:
        start_date = datetime.date(2011, 1, 1)
        end_date = today + datetime.timedelta(days=365)
    else:
        start_date = today - datetime.timedelta(days=args.days)
        end_date = today + datetime.timedelta(days=args.ahead)

    print(f"Date window : {start_date} to {end_date}")
    print(f"API base    : {API_BASE}")
    if not args.dry_run:
        print(f"Output dir  : {args.output_dir}")
    if args.download_media:
        ytdlp_found = bool(shutil.which("yt-dlp"))
        print(f"yt-dlp      : {'found' if ytdlp_found else 'NOT FOUND — media download disabled'}")
    print()

    # --- Step 1: fetch events ---
    print("Fetching events from CivicClerk API...")
    events = fetch_events(start_date, end_date)
    if not events:
        print("No events found in the date window.")
        sys.exit(0)

    # Apply board filter
    if args.board:
        filter_str = args.board.lower()
        events = [e for e in events if filter_str in e.get("categoryName", "").lower()]
        if not events:
            print(f"No events match board filter: '{args.board}'")
            sys.exit(0)

    print(f"  Found {len(events)} event(s).")
    print()

    # --- Step 2: collect files from each event ---
    all_tasks = []   # list of (event, file_info)
    media_records = []  # list of (event, url)

    print(f"Fetching meeting file lists ({len(events)} event(s))...")
    for i, event in enumerate(events, 1):
        ev_name = event.get("eventName", "?")
        ev_date = event.get("eventDate", "")[:10]
        ev_board = event.get("categoryName", "")
        agenda_id = event.get("agendaId", 0)

        print(f"  [{i:>3}/{len(events)}] {ev_date} {ev_board[:40]:<40}", end=" ")

        # Collect video/recording URL
        media_url = event.get("externalMediaUrl", "").strip()
        if media_url:
            media_records.append((event, media_url))

        if agenda_id <= 0:
            print("(no agenda)")
            time.sleep(PAGE_DELAY)
            continue

        files = fetch_meeting_files(agenda_id)
        if not files:
            print("0 files")
            time.sleep(PAGE_DELAY)
            continue

        # Filter by doc type
        filtered = []
        for f in files:
            ftype = f.get("type", "").lower()
            if args.no_agendas and ("agenda" in ftype):
                continue
            if args.no_minutes and ftype == "minutes":
                continue
            filtered.append(f)

        print(f"{len(filtered)} file(s) of {len(files)}")
        for f in filtered:
            all_tasks.append((event, f))

        time.sleep(PAGE_DELAY)

    print()
    print(f"Total: {len(all_tasks)} file(s) to download, {len(media_records)} media URL(s).")
    print()

    # --- Dry run ---
    if args.dry_run:
        print(f"{'Date':<12} {'Board':<38} {'Type'}")
        print("-" * 70)
        for event, finfo in all_tasks:
            ev_date = event.get("eventDate", "")[:10]
            board = event.get("categoryName", "")[:37]
            ftype = finfo.get("type", "")
            fname = finfo.get("name", "")
            print(f"{ev_date:<12} {board:<38} {ftype} ({fname})")
        if media_records:
            print()
            print(f"{'Date':<12} {'Board':<38} Media URL")
            print("-" * 70)
            for event, url in media_records:
                ev_date = event.get("eventDate", "")[:10]
                board = event.get("categoryName", "")[:37]
                print(f"{ev_date:<12} {board:<38} {url}")
        print(f"\n{len(all_tasks)} file(s), {len(media_records)} media URL(s). "
              "Re-run without --dry-run to download.")
        return

    # --- Step 3: download files ---
    os.makedirs(args.output_dir, exist_ok=True)
    log_path = os.path.join(args.output_dir, "download-log.txt")
    media_log_path = os.path.join(args.output_dir, "media-log.txt")
    log_lines = []
    media_log_lines = []
    downloaded = skipped = failed = 0
    seen_dests = set()

    for event, finfo in all_tasks:
        dest = make_dest_path(event, finfo, args.output_dir)
        dest = dedupe_dest(dest, seen_dests)
        label = os.path.relpath(dest, args.output_dir)
        ev_date = event.get("eventDate", "")[:10]
        board = event.get("categoryName", "")
        ftype = finfo.get("type", "")
        fname = finfo.get("name", "")

        if os.path.exists(dest):
            print(f"  skip (exists)  {label}")
            skipped += 1
            continue

        print(f"  [{ev_date}] {board} — {ftype} ({fname})")

        # Resolve the Azure Blob URI
        file_url = finfo.get("url", "")
        if not file_url:
            print(f"    WARNING: no url for fileId={finfo.get('fileId')}", file=sys.stderr)
            failed += 1
            continue

        blob_uri = resolve_blob_uri(file_url)
        if not blob_uri:
            print(f"    WARNING: could not resolve blob URI for {fname}", file=sys.stderr)
            failed += 1
            log_lines.append(
                f"{datetime.datetime.now().isoformat()}  FAILED   {file_url}"
            )
            time.sleep(DOWNLOAD_DELAY)
            continue

        print(f"  → {label}")
        if download_bytes(blob_uri, dest):
            downloaded += 1
            log_lines.append(
                f"{datetime.datetime.now().isoformat()}  OK       {dest}"
            )
        else:
            failed += 1
            log_lines.append(
                f"{datetime.datetime.now().isoformat()}  FAILED   {blob_uri[:120]}"
            )
            if os.path.exists(dest):
                os.remove(dest)

        time.sleep(DOWNLOAD_DELAY)

    # --- Step 4: handle media ---
    if media_records:
        print()
        print(f"Recording URLs ({len(media_records)}):")
        for event, media_url in media_records:
            ev_date = event.get("eventDate", "")[:10]
            board = event.get("categoryName", "")
            ev_name = event.get("eventName", "")
            print(f"  {ev_date} {board}: {media_url}")
            media_log_lines.append(f"{ev_date}\t{board}\t{ev_name}\t{media_url}")

            if args.download_media and shutil.which("yt-dlp"):
                ev_date_obj = datetime.datetime.fromisoformat(
                    event["eventDate"].replace("Z", "+00:00")
                ).date()
                board_slug = slugify(board)
                ok = download_with_ytdlp(media_url, args.output_dir, ev_date_obj, board_slug)
                status = "downloaded" if ok else "FAILED"
                media_log_lines[-1] += f"\t{status}"
                print(f"    yt-dlp: {status}")

        # Write media log
        if media_log_lines:
            with open(media_log_path, "a") as f:
                f.write("\n".join(media_log_lines) + "\n")
            print(f"Media log : {media_log_path}")

    # Write download log
    if log_lines:
        with open(log_path, "a") as f:
            f.write("\n".join(log_lines) + "\n")

    print()
    print(f"Done — downloaded: {downloaded}  skipped: {skipped}  failed: {failed}")
    if downloaded + skipped:
        print(f"Files in  : {args.output_dir}")
    if log_lines:
        print(f"Log       : {log_path}")


if __name__ == "__main__":
    main()


# --- Tips ---
#
# 1. Preview without downloading (past 30 days):
#    python3 scripts/download-west-hartford-agendas.py --dry-run
#
# 2. Extend the lookback window (e.g., 1 year):
#    python3 scripts/download-west-hartford-agendas.py --days 365 --dry-run
#
# 3. Filter to a specific board:
#    python3 scripts/download-west-hartford-agendas.py --board "town council"
#    python3 scripts/download-west-hartford-agendas.py --board "planning"
#
# 4. Agendas only (skip minutes):
#    python3 scripts/download-west-hartford-agendas.py --no-minutes
#
# 5. Save to a custom directory:
#    python3 scripts/download-west-hartford-agendas.py --output-dir ~/Downloads/wh-meetings
#
# 6. Download video recordings (requires yt-dlp):
#    python3 scripts/download-west-hartford-agendas.py --download-media
#
# 7. Download everything in the archive (all years):
#    python3 scripts/download-west-hartford-agendas.py --all --dry-run
#
# 8. Run on a schedule (cron — 8 AM daily):
#    0 8 * * * cd /path/to/repo && python3 scripts/download-west-hartford-agendas.py
#
# 9. Process downloaded files with Claude afterward:
#    python3 scripts/download-west-hartford-agendas.py && \
#    bash scripts/batch-process.sh beat-archive/west-hartford-agendas/
#
# BOARDS (as of May 2026):
#   Town Council, Public Hearing, Community Planning & Economic Development,
#   Finance & Administration Committee, Human & Community Services Committee,
#   Public Safety Committee, Public Works Facilities and Sustainability Committee,
#   Board of Assessment Appeals, Board of Assessors, Civilian Police Review Board,
#   Commission on the Arts, Commission on Veterans' Affairs,
#   Design Review Advisory Committee, Fair Rent Commission,
#   Historic District Commission, Human Rights Commission, Library Board,
#   Mayor's Youth Council, Parks & Recreation Advisory Board,
#   Pedestrian and Bicycle Commission, Pension Board, Prevention Council,
#   Senior Citizens Advisory Commission, Special Services District,
#   Sustainable West Hartford Commission, Town Plan and Zoning Commission,
#   Vision Zero Task Force, Zoning Board of Appeals
#
# SITE NOTES:
#   - CivicClerk is a pure SPA; all data comes from an unauthenticated OData REST API.
#   - File download is a two-step process: the API returns a time-limited Azure Blob
#     SAS URI (typically valid 7 days); the script resolves and downloads it.
#   - Video recordings are linked as YouTube URLs in the externalMediaUrl field.
#     Pass --download-media and install yt-dlp to download them automatically.
#   - The media-log.txt file records all recording URLs even without --download-media.
#   - OData $filter uses ISO 8601 UTC timestamps (YYYY-MM-DDTHH:MM:SSZ).
#   - The archive goes back to 2011-01-11; use --all to fetch everything.
