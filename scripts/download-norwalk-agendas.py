#!/usr/bin/env python3
# download-norwalk-agendas.py
# Download municipal meeting agendas and minutes from Norwalk CT for meetings
# whose date falls within the past N days (and up to 7 days ahead, to catch
# agendas posted early for upcoming meetings).
#
# USAGE:
#   python3 scripts/download-norwalk-agendas.py [options]
#
# REQUIREMENTS:
#   - Python 3.6+  (no third-party packages needed)
#   - Internet connection
#
# WHAT IT DOES:
#   1. Queries the Norwalk CivicClerk public REST API for events in the date window,
#      following @odata.nextLink pagination to get all events
#   2. For each event, collects published files (agendas, agenda packets, minutes,
#      actions) and optionally saves the Zoom meeting link as a .url shortcut
#   3. Downloads PDFs to beat-archive/norwalk-agendas/YYYY-MM/
#   4. Appends a download log to beat-archive/norwalk-agendas/download-log.txt
#
# SITE STRUCTURE (CivicClerk public portal, moved Jan 1 2025):
#   Portal:   https://norwalkct.portal.civicclerk.com/
#   API base: https://norwalkct.api.civicclerk.com/v1
#
#   Events list:
#     GET /Events?$filter=eventDate ge {ISO} and eventDate le {ISO}&$orderby=eventDate desc
#   The API returns 15 events per page; follow @odata.nextLink for subsequent pages.
#   Each event has a publishedFiles[] array; each file entry has:
#     fileId   — integer, used to download
#     type     — "Agenda", "Agenda Packet", "Minutes", "Actions", "Other"
#     name     — human-readable document name
#   Download a file:
#     GET /Meetings/GetMeetingFileStream(fileId={id},plainText=false)
#     Returns the PDF with Content-Type: application/pdf
#   Each event also has:
#     externalMediaUrl — Zoom meeting room link (e.g. zoom.us/j/...) for
#                        meetings that were held on Zoom.  Not a recording URL,
#                        but identifies the Zoom room where recordings may be
#                        archived by the city clerk.
#
# RECORDINGS NOTE:
#   Norwalk meeting recordings are hosted on a separate Blazor Server app:
#     https://apps.norwalkct.org/meetingboard/recordings
#   That app uses SignalR (WebSockets) and has no public REST API — it must be
#   accessed with a browser. This script cannot scrape it automatically.
#   Use --include-meeting-links to save each event's Zoom room link as a .url
#   file for quick reference.
#
# NOTE: The old CivicEngage Agenda Center (norwalkct.gov/AgendaCenter) is still
# live but dynamically loaded and no longer the primary source after Jan 1, 2025.
# This script uses the new CivicClerk API exclusively.
#
# NOTE: The API is unauthenticated. No cookies, tokens, or headers beyond a
# User-Agent are required.

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
API_BASE = "https://norwalkct.api.civicclerk.com/v1"
OUTPUT_DIR = "beat-archive/norwalk-agendas"
DAYS_BACK = 4
DAYS_AHEAD = 7
DELAY_SECONDS = 0.8

# File types to download (by the 'type' field in publishedFiles)
WANTED_TYPES = {"Agenda", "Agenda Packet", "Minutes", "Actions", "Other"}

UA = "Mozilla/5.0"


# --- HTTP helpers ---

import json as _json

def _api_fetch(url):
    """GET a CivicClerk API URL; returns parsed JSON or None on error."""
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return _json.loads(r.read().decode("utf-8", errors="replace"))
    except urllib.error.HTTPError as e:
        print(f"  HTTP {e.code} — {url}", file=sys.stderr)
        return None
    except Exception as e:
        print(f"  ERROR: {e} — {url}", file=sys.stderr)
        return None


def api_get_events(params, max_pages=200):
    """Fetch all events matching params, following @odata.nextLink pagination."""
    url = f"{API_BASE}/Events?" + urllib.parse.urlencode(params)
    all_events = []
    page = 0
    while url and page < max_pages:
        data = _api_fetch(url)
        if not data:
            break
        all_events.extend(data.get("value", []))
        url = data.get("@odata.nextLink", "")
        page += 1
    return all_events


def download_file(file_id, dest_path):
    """Download a file via GetMeetingFileStream. Returns True on success."""
    url = f"{API_BASE}/Meetings/GetMeetingFileStream(fileId={file_id},plainText=false)"
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


def save_url_shortcut(url, dest_path):
    """Save a URL as a Windows Internet Shortcut (.url) file. Returns True."""
    with open(dest_path, "w") as f:
        f.write(f"[InternetShortcut]\nURL={url}\n")
    return True


# --- Helpers ---

def slugify(text, max_len=55):
    text = text.lower().strip()
    text = re.sub(r"[/\\&]", "-", text)
    text = re.sub(r"\s+-\s+", "-", text)
    text = re.sub(r"[^\w\s-]", "", text)
    text = re.sub(r"[\s_]+", "-", text)
    text = re.sub(r"-{2,}", "-", text)
    return text.strip("-")[:max_len]


def make_dest_path(event_name, doc_type, meeting_date, file_id, output_dir, ext=".pdf"):
    date_str = meeting_date.strftime("%Y-%m-%d")
    month_dir = os.path.join(output_dir, meeting_date.strftime("%Y-%m"))
    os.makedirs(month_dir, exist_ok=True)
    name_slug = slugify(event_name, max_len=45)
    type_slug = slugify(doc_type, max_len=15)
    fname = f"{date_str}-{name_slug}-{type_slug}-{file_id}{ext}"
    return os.path.join(month_dir, fname)


def iso_date(d):
    return d.strftime("%Y-%m-%dT00:00:00Z")


# --- Main ---

def main():
    parser = argparse.ArgumentParser(
        description=(
            "Download Norwalk CT municipal agendas and minutes "
            "from the CivicClerk API for meetings within the past N days."
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
        help="Only include events whose name contains NAME (case-insensitive)",
    )
    parser.add_argument(
        "--doc-type", metavar="TYPE",
        help="Only include files of this type, e.g. 'Agenda' or 'Minutes' (case-insensitive)",
    )
    parser.add_argument(
        "--include-meeting-links", action="store_true",
        help="Save each event's Zoom meeting room URL as a .url shortcut file",
    )
    args = parser.parse_args()

    if datetime.date.today().weekday() in (6, 0):  # Sunday, Monday
        print("Skipping — no downloads on Sunday or Monday.")
        sys.exit(0)

    today = datetime.date.today()
    cutoff = today - datetime.timedelta(days=args.days)
    future_limit = today + datetime.timedelta(days=args.ahead)

    print(f"Date window : {cutoff} to {future_limit}")
    print(f"API base    : {API_BASE}")
    if not args.dry_run:
        print(f"Output dir  : {args.output_dir}")
    print()

    # --- Step 1: fetch events (follow @odata.nextLink pagination) ---
    odata_filter = (
        f"eventDate ge {iso_date(cutoff)} and eventDate le {iso_date(future_limit)}"
    )
    params = {
        "$filter": odata_filter,
        "$orderby": "eventDate desc",
    }

    print("Fetching events from CivicClerk API...")
    events = api_get_events(params)
    print(f"  API returned {len(events)} event(s) in date window.")

    if not events:
        print("No events found in the date window.")
        return

    # --- Step 2: extract downloadable files ---
    docs = []
    seen_file_ids = set()
    seen_zoom_keys = set()  # (event_name, meeting_date) dedup for zoom links

    for event in events:
        event_name = event.get("eventName", "Unknown").strip()
        event_date_str = event.get("eventDate", "")

        try:
            meeting_date = datetime.datetime.strptime(
                event_date_str[:10], "%Y-%m-%d"
            ).date()
        except ValueError:
            continue

        published_files = event.get("publishedFiles", []) or []
        for pf in published_files:
            doc_type = (pf.get("type") or "").strip()
            file_id = pf.get("fileId", 0)
            file_name = (pf.get("name") or "").strip()

            if not file_id or file_id in seen_file_ids:
                continue
            if doc_type not in WANTED_TYPES:
                continue

            seen_file_ids.add(file_id)
            docs.append({
                "event_name": event_name,
                "meeting_date": meeting_date,
                "doc_type": doc_type,
                "file_id": file_id,
                "file_name": file_name,
                "is_zoom_link": False,
                "zoom_url": "",
            })

        # Optionally include the Zoom meeting room link as a .url shortcut
        if args.include_meeting_links:
            zoom_url = (event.get("externalMediaUrl") or "").strip()
            zoom_key = (event_name, meeting_date)
            if zoom_url and "zoom.us" in zoom_url and zoom_key not in seen_zoom_keys:
                seen_zoom_keys.add(zoom_key)
                docs.append({
                    "event_name": event_name,
                    "meeting_date": meeting_date,
                    "doc_type": "Meeting Link",
                    "file_id": 0,
                    "file_name": zoom_url,
                    "is_zoom_link": True,
                    "zoom_url": zoom_url,
                })

    # Apply filters (never filter out zoom link entries by doc-type)
    if args.board:
        filter_str = args.board.lower()
        docs = [d for d in docs if filter_str in d["event_name"].lower()]

    if args.doc_type:
        filter_str = args.doc_type.lower()
        docs = [d for d in docs if d["is_zoom_link"] or filter_str in d["doc_type"].lower()]

    docs.sort(key=lambda x: (x["meeting_date"], x["event_name"]), reverse=True)

    print(
        f"Found {len(docs)} document(s) across "
        f"{len({d['event_name'] for d in docs})} unique event(s)."
    )
    print()

    if not docs:
        return

    if args.dry_run:
        print(f"{'Event':<48} {'Date':<12} {'Type':<14} {'File Name'}")
        print("-" * 100)
        for d in docs:
            fname = d["zoom_url"][:35] if d["is_zoom_link"] else d["file_name"][:35]
            print(
                f"{d['event_name'][:47]:<48} "
                f"{d['meeting_date']!s:<12} "
                f"{d['doc_type']:<14} "
                f"{fname}"
            )
        print(f"\n{len(docs)} document(s). Re-run without --dry-run to download.")
        return

    # --- Step 3: download ---
    os.makedirs(args.output_dir, exist_ok=True)
    log_path = os.path.join(args.output_dir, "download-log.txt")
    log_lines = []
    downloaded = skipped = failed = 0

    for d in docs:
        ext = ".url" if d["is_zoom_link"] else ".pdf"
        dest = make_dest_path(
            d["event_name"], d["doc_type"], d["meeting_date"],
            d["file_id"], args.output_dir, ext=ext,
        )
        label = os.path.basename(dest)

        if os.path.exists(dest):
            print(f"  skip (exists)  {label}")
            skipped += 1
            continue

        print(f"  [{d['meeting_date']}] {d['event_name']} — {d['doc_type']}")
        print(f"  saving         {label}")

        if d["is_zoom_link"]:
            ok = save_url_shortcut(d["zoom_url"], dest)
        else:
            ok = download_file(d["file_id"], dest)

        if ok:
            downloaded += 1
            log_lines.append(
                f"{datetime.datetime.now().isoformat()}  OK       {dest}"
            )
        else:
            failed += 1
            log_lines.append(
                f"{datetime.datetime.now().isoformat()}  FAILED   fileId={d['file_id']}"
            )
            if os.path.exists(dest):
                os.remove(dest)

        if not d["is_zoom_link"]:
            time.sleep(DELAY_SECONDS)

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
#    python3 scripts/download-norwalk-agendas.py --dry-run
#
# 2. Narrow to one board:
#    python3 scripts/download-norwalk-agendas.py --board "Common Council"
#
# 3. Agendas only (no packets or minutes):
#    python3 scripts/download-norwalk-agendas.py --doc-type "Agenda"
#
# 4. Change the lookback window:
#    python3 scripts/download-norwalk-agendas.py --days 7
#
# 5. Save files somewhere else:
#    python3 scripts/download-norwalk-agendas.py --output-dir ~/Downloads/norwalk
#
# 6. Also save each event's Zoom meeting room link as a .url shortcut:
#    python3 scripts/download-norwalk-agendas.py --include-meeting-links
#
# 7. Run on a schedule (cron — 8 AM daily):
#    0 8 * * * cd /path/to/repo && python3 scripts/download-norwalk-agendas.py
#
# 8. Process downloaded files with Claude afterward:
#    python3 scripts/download-norwalk-agendas.py && bash scripts/batch-process.sh beat-archive/norwalk-agendas/
#
# NOTES:
#   - Norwalk moved from CivicEngage to CivicClerk on January 1, 2025.
#     This script covers the new portal only. Pre-2025 agendas are in the
#     old CivicEngage archive at norwalkct.gov/Archive.aspx?AMID=NNN.
#   - The CivicClerk API is public and requires no authentication.
#   - The API returns 15 events per page; this script follows @odata.nextLink
#     to retrieve all events in the date window.
#   - Document types available: Agenda, Agenda Packet, Minutes, Actions, Other.
#     The WANTED_TYPES set at the top of the script controls which are downloaded.
#   - The --ahead flag (default: 7 days) captures agendas for upcoming meetings
#     that have already been published. Run daily to stay current.
#   - RECORDINGS: Norwalk's meeting video archive is at
#     https://apps.norwalkct.org/meetingboard/recordings
#     That site uses Blazor Server (SignalR/WebSockets) and has no public REST API.
#     It must be accessed manually in a browser. Use --include-meeting-links to
#     save Zoom room links alongside agenda/minutes files.
