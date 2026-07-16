#!/usr/bin/env python3
# download-hamden-agendas.py
# Download municipal meeting agendas, minutes, and video recordings from
# Hamden CT's CivicClerk portal for meetings within a date window.
#
# USAGE:
#   python3 scripts/download-hamden-agendas.py [options]
#
# REQUIREMENTS:
#   - Python 3.6+  (no third-party packages needed)
#   - Internet connection
#
# WHAT IT DOES:
#   1. Queries the CivicClerk OData API for events in the date window
#      (follows @odata.nextLink pagination, 15 events per page)
#   2. For each event, collects publishedFiles (Agenda, Agenda Packet,
#      Minutes, Notice) and any associated video/audio recording
#   3. Downloads PDFs via the Meetings/GetMeetingFileStream endpoint
#   4. Optionally downloads recordings from the CivicClerk Azure CDN
#   5. Appends a download log to beat-archive/hamden-agendas/download-log.txt
#
# SITE STRUCTURE:
#   CMS: CivicClerk (hamdenct.portal.civicclerk.com)
#
#   Hamden's previous site — the CivicPlus AgendaCenter at
#   hamden.com/AgendaCenter, which this script used to scrape — was
#   decommissioned around April 2025. It now shows a banner reading "This
#   page has been decommissioned and replaced with the Public Meeting
#   Center" and hasn't listed a new meeting since. This script targets the
#   replacement (CivicClerk) directly via its public API instead.
#
#   Public portal: https://hamdenct.portal.civicclerk.com
#   OData API:     https://hamdenct.api.civicclerk.com/v1
#
#   Events endpoint:
#     GET /Events
#       ?$filter=eventDate ge {ISO_DATE}Z and eventDate le {ISO_DATE}Z
#       &$orderby=eventDate asc
#     Returns 15 events per page; follow @odata.nextLink until absent.
#
#   Each event contains:
#     - id, eventName, eventDate, categoryName
#     - publishedFiles: [{fileId, type, name, url, sort, fileType}]
#       type values seen: "Agenda", "Agenda Packet", "Minutes", "Notice", "Other"
#     - mediaStreamPath: "stream/HAMDENCT/{uuid}.{ext}" (empty if no recording)
#     - mediaTypeId: 1=video, 2=audio
#     - hasMedia: bool
#
#   Document download:
#     GET /v1/Meetings/GetMeetingFileStream(fileId={id},plainText=false)
#     (no authentication required)
#
#   Recording download:
#     https://cpmedia.azureedge.net/hamdenct/{uuid}.{ext}
#     (extract {uuid}.{ext} from the last path component of mediaStreamPath;
#      plain HTTPS GET, no auth/session dance needed — unlike the old
#      Zoom-hosted recordings this script previously fetched)
#     NOTE: these files run 300 MB - 1 GB+; downloaded in streamed chunks.
#
# NOTE: The CivicClerk API is public and requires no authentication.

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
API_BASE   = "https://hamdenct.api.civicclerk.com/v1"
CDN_BASE   = "https://cpmedia.azureedge.net/hamdenct"
OUTPUT_DIR = "beat-archive/hamden-agendas"
DAYS_BACK  = 4
DAYS_AHEAD = 7
API_DELAY  = 0.25   # seconds between paginated API calls / downloads

UA = "Mozilla/5.0 (X11; Linux x86_64; rv:120.0) Gecko/20100101 Firefox/120.0"

# File types to download (case-sensitive, matches the API's "type" field).
# "Other" is also seen on some events but is too ambiguous to auto-download.
DOWNLOAD_TYPES = {"Agenda", "Agenda Packet", "Minutes", "Notice"}


# --- HTTP helpers ---

def fetch_json(url):
    """GET url and return parsed JSON dict, or raise on error."""
    req = urllib.request.Request(
        url, headers={"User-Agent": UA, "Accept": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return json.load(r)
    except urllib.error.HTTPError as e:
        print(f"  HTTP {e.code} — {url}", file=sys.stderr)
        raise
    except Exception as e:
        print(f"  ERROR fetching {url}: {e}", file=sys.stderr)
        raise


def download_file(url, dest_path, timeout=120):
    """Download a small file (PDF) to dest_path. Returns True on success."""
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "*/*"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            data = r.read()
        with open(dest_path, "wb") as f:
            f.write(data)
        return True
    except Exception as e:
        print(f"  WARNING: {e}", file=sys.stderr)
        if os.path.exists(dest_path):
            os.remove(dest_path)
        return False


def download_media(url, dest_path, timeout=120):
    """
    Stream-download a large recording (300 MB - 1 GB+) to dest_path in
    chunks, so we never hold the whole file in memory. urlopen's timeout
    applies per socket read, not to the total transfer, so a slow-but-
    steady connection still completes; a genuinely stalled one raises and
    the partial file is cleaned up. Returns True on success.
    """
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "*/*"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            total = int(r.headers.get("Content-Length", 0))
            chunk_size = 1024 * 1024  # 1 MB
            downloaded = 0
            with open(dest_path, "wb") as f:
                while True:
                    chunk = r.read(chunk_size)
                    if not chunk:
                        break
                    f.write(chunk)
                    downloaded += len(chunk)
                    if total:
                        pct = 100 * downloaded // total
                        mb, total_mb = downloaded // (1024 * 1024), total // (1024 * 1024)
                        print(f"\r    {pct}%  {mb}/{total_mb} MB", end="", flush=True)
            print()
        return True
    except Exception as e:
        print(f"\n  WARNING: {e}", file=sys.stderr)
        if os.path.exists(dest_path):
            os.remove(dest_path)
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
    while url:
        data = fetch_json(url)
        events.extend(data.get("value", []))
        url = data.get("@odata.nextLink")
        if url:
            time.sleep(API_DELAY)
    return events


def make_doc_url(file_id):
    """Return the document download URL for the given CivicClerk file ID."""
    return f"{API_BASE}/Meetings/GetMeetingFileStream(fileId={file_id},plainText=false)"


def make_media_url(stream_path):
    """
    Convert a mediaStreamPath to a downloadable CDN URL.
      "stream/HAMDENCT/{uuid}.ext" → CDN_BASE/{uuid}.ext
      already a full "https://..." URL → used as-is
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
    month_dir = os.path.join(output_dir, event_date.strftime("%Y-%m"))
    os.makedirs(month_dir, exist_ok=True)
    date_str = event_date.strftime("%Y-%m-%d")
    board_slug = slugify(board, max_len=35)
    name_slug = slugify(event_name, max_len=30)
    type_slug = slugify(doc_type, max_len=20)
    fname = f"{date_str}-{board_slug}-{name_slug}-{type_slug}.pdf"
    return os.path.join(month_dir, fname)


def make_media_path(board, event_date, event_name, stream_path, output_dir):
    month_dir = os.path.join(output_dir, event_date.strftime("%Y-%m"))
    os.makedirs(month_dir, exist_ok=True)
    date_str = event_date.strftime("%Y-%m-%d")
    board_slug = slugify(board, max_len=35)
    name_slug = slugify(event_name, max_len=25)
    filename = stream_path.split("/")[-1]
    ext = filename.rsplit(".", 1)[-1].lower() if "." in filename else "mp4"
    uuid_short = filename.split(".")[0][:12]
    fname = f"{date_str}-{board_slug}-{name_slug}-{uuid_short}.{ext}"
    return os.path.join(month_dir, fname)


# --- Main ---

def main():
    parser = argparse.ArgumentParser(
        description=(
            "Download Hamden CT municipal agendas, minutes, and video "
            "recordings via the CivicClerk API."
        )
    )
    parser.add_argument("--days", type=int, default=DAYS_BACK, metavar="N",
                        help=f"Look back N days by meeting date (default: {DAYS_BACK})")
    parser.add_argument("--ahead", type=int, default=DAYS_AHEAD, metavar="N",
                        help=f"Also include meetings up to N days ahead (default: {DAYS_AHEAD})")
    parser.add_argument("--output-dir", default=OUTPUT_DIR, metavar="DIR",
                        help=f"Destination directory (default: {OUTPUT_DIR})")
    parser.add_argument("--dry-run", action="store_true",
                        help="List matching items without downloading")
    parser.add_argument("--board", metavar="NAME",
                        help="Only include boards/categories containing NAME (case-insensitive)")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--include-video", action="store_true",
                      help="Also download video/audio recordings (300 MB - 1 GB+ each)")
    mode.add_argument("--video-only", action="store_true",
                      help="Download recordings only, skip PDFs")
    args = parser.parse_args()

    now = datetime.datetime.now()
    if (now.weekday() == 5 and now.hour >= 18) or (now.weekday() == 6 and now.hour < 12):  # Saturday night, Sunday morning
        print("Skipping — no downloads on Saturday nights or Sunday mornings.")
        sys.exit(0)

    do_docs  = not args.video_only
    do_video = args.include_video or args.video_only

    today        = datetime.date.today()
    cutoff       = today - datetime.timedelta(days=args.days)
    future_limit = today + datetime.timedelta(days=args.ahead)
    board_filter = args.board.lower() if args.board else None

    print(f"Date window : {cutoff} to {future_limit}")
    print(f"Site        : {API_BASE}")
    if do_video:
        print("Video       : enabled (CivicClerk CDN)")
    if not args.dry_run:
        print(f"Output dir  : {args.output_dir}")
    print()

    print("Fetching events from CivicClerk API...")
    try:
        all_events = fetch_events(cutoff, future_limit)
    except Exception as e:
        print(f"FATAL: Could not fetch events: {e}", file=sys.stderr)
        sys.exit(1)
    print(f"  {len(all_events)} event(s) in window\n")

    all_docs  = []   # {board, event_date, event_name, doc_type, file_id}
    all_media = []   # {board, event_date, event_name, event_id, stream_path, media_type}

    for event in all_events:
        board = event.get("categoryName", "Unknown Board")
        if board_filter and board_filter not in board.lower():
            continue

        event_date = parse_event_date(event)
        if not event_date:
            continue
        event_name = event.get("eventName", "Meeting")

        if do_docs:
            for f in event.get("publishedFiles") or []:
                doc_type = f.get("type", "")
                if doc_type not in DOWNLOAD_TYPES:
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
                })

        if do_video:
            stream_path = (event.get("mediaStreamPath") or "").strip()
            if stream_path:
                all_media.append({
                    "board": board,
                    "event_date": event_date,
                    "event_name": event_name,
                    "event_id": event.get("id"),
                    "stream_path": stream_path,
                    "media_type": "audio" if event.get("mediaTypeId") == 2 else "video",
                })

    all_docs.sort(key=lambda x: (x["event_date"], x["board"]), reverse=True)
    all_media.sort(key=lambda x: (x["event_date"], x["board"]), reverse=True)

    if do_docs:
        print(f"  {len(all_docs)} document(s) matched")
    if do_video:
        print(f"  {len(all_media)} recording(s) matched")
    print()

    if not all_docs and not all_media:
        print("No items found in the date window.")
        return

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
                print(
                    f"{m['board'][:39]:<40} "
                    f"{m['event_date']!s:<12} "
                    f"{m['event_name'][:29]:<30} "
                    f"{m['media_type']}"
                )
            print()
        total = len(all_docs) + len(all_media)
        print(f"{total} item(s) matched. Re-run without --dry-run to download.")
        return

    os.makedirs(args.output_dir, exist_ok=True)
    log_path = os.path.join(args.output_dir, "download-log.txt")
    log_lines = []
    downloaded = skipped = failed = 0

    # --- Documents ---
    if all_docs:
        print(f"Downloading {len(all_docs)} document(s)...")
        for d in all_docs:
            dest = make_pdf_path(
                d["board"], d["doc_type"], d["event_date"], d["event_name"], args.output_dir,
            )
            label = os.path.basename(dest)

            if os.path.exists(dest) and os.path.getsize(dest) > 0:
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
            time.sleep(API_DELAY)
        print()

    # --- Recordings ---
    if all_media:
        print(f"Downloading {len(all_media)} recording(s)...")
        for m in all_media:
            dest = make_media_path(
                m["board"], m["event_date"], m["event_name"], m["stream_path"], args.output_dir,
            )
            label = os.path.basename(dest)

            if os.path.exists(dest) and os.path.getsize(dest) > 0:
                print(f"  skip (exists)  {label}")
                skipped += 1
                continue

            url = make_media_url(m["stream_path"])
            print(f"  [{m['event_date']}] {m['board'][:45]} — {m['event_name'][:40]}  [{m['media_type']}]")
            print(f"  downloading    {label}")
            print(f"  source         {url}")
            if download_media(url, dest):
                downloaded += 1
                log_lines.append(
                    f"{datetime.datetime.now().isoformat()}  OK       {dest}"
                )
            else:
                failed += 1
                log_lines.append(
                    f"{datetime.datetime.now().isoformat()}  FAILED   {url}"
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
#    python3 scripts/download-hamden-agendas.py --dry-run
#
# 2. Download docs + video/audio recordings:
#    python3 scripts/download-hamden-agendas.py --include-video
#
# 3. Narrow to one board:
#    python3 scripts/download-hamden-agendas.py --board "Legislative Council"
#
# 4. Change the lookback window:
#    python3 scripts/download-hamden-agendas.py --days 7
#
# 5. Save files somewhere else:
#    python3 scripts/download-hamden-agendas.py --output-dir ~/Downloads/hamden
#
# 6. Run on a schedule (cron — 8 AM daily):
#    0 8 * * * cd /path/to/repo && python3 scripts/download-hamden-agendas.py
#
# NOTE: Recordings are typically 300 MB - 1 GB+ each and are skipped unless
# --include-video or --video-only is passed. Files that already exist on
# disk (and are non-empty) are skipped, so re-runs are safe.
#
# NOTE: The CivicClerk API returns 15 events per page; the script follows
# @odata.nextLink automatically until all pages are fetched.
