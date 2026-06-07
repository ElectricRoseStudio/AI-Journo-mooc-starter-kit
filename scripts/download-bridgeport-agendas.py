#!/usr/bin/env python3
# download-bridgeport-agendas.py
# Download municipal meeting agendas and minutes from Bridgeport CT
# for meetings whose date falls within the past N days (and up to 7 days
# ahead, to catch agendas posted early for upcoming meetings).
#
# USAGE:
#   python3 scripts/download-bridgeport-agendas.py [options]
#
# REQUIREMENTS:
#   - Python 3.6+  (no third-party packages needed)
#   - Internet connection
#
# WHAT IT DOES:
#   1. Fetches all published meeting events from the Bridgeport CT Legistar
#      REST API (webapi.legistar.com/v1/bridgeportct/Events)
#   2. Filters events whose meeting date falls within the date window
#   3. Downloads Agenda and Minutes PDFs to beat-archive/bridgeport-agendas/YYYY-MM/
#   4. Appends a download log to beat-archive/bridgeport-agendas/download-log.txt
#
# SITE STRUCTURE:
#   Bridgeport CT uses Legistar (bridgeportct.legistar.com). The public
#   REST API is at https://webapi.legistar.com/v1/bridgeportct/. Each event
#   object includes direct PDF URLs in EventAgendaFile and EventMinutesFile
#   when documents have been published.
#
#   The API returns only events with published agenda documents; events with
#   no published agenda do not appear. OData date filters are not applied
#   server-side (the instance returns all published events regardless of
#   $filter), so date filtering is done in Python.
#
#   Currently Bridgeport uses Legistar only for City Council meetings.

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
BASE_URL = "https://bridgeportct.legistar.com"
API_BASE = "https://webapi.legistar.com/v1/bridgeportct"
OUTPUT_DIR = "beat-archive/bridgeport-agendas"
DAYS_BACK = 4
DAYS_AHEAD = 7
DELAY_SECONDS = 1

UA = "Bridgeport-Agendas-Downloader/1.0 (journalism research)"


# --- HTTP helpers ---

def fetch_json(url):
    """GET url and return parsed JSON, or None on error."""
    req = urllib.request.Request(
        url,
        headers={"User-Agent": UA, "Accept": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return json.loads(r.read())
    except urllib.error.URLError as e:
        print(f"  ERROR fetching {url}: {e}", file=sys.stderr)
        return None


def download_file(url, dest_path):
    """Download a PDF from url to dest_path. Returns True on success."""
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            with open(dest_path, "wb") as f:
                f.write(r.read())
        return True
    except Exception as e:
        print(f"  WARNING: {e}", file=sys.stderr)
        return False


# --- API helpers ---

def fetch_events():
    """
    Return all published events from the Legistar API.

    The Bridgeport instance does not honour OData $filter or $top parameters
    reliably, so we fetch all events and filter by date in Python.
    """
    url = f"{API_BASE}/Events?$orderby=EventDate+desc"
    data = fetch_json(url)
    if data is None:
        return []
    return data


# --- Utilities ---

def parse_event_date(event):
    """Return a date from EventDate ISO string, or None."""
    raw = event.get("EventDate", "")
    if not raw:
        return None
    try:
        return datetime.datetime.strptime(raw[:10], "%Y-%m-%d").date()
    except ValueError:
        return None


def slugify(text, max_len=60):
    text = text.lower().strip()
    text = re.sub(r"[/\\&]", "-", text)
    text = re.sub(r"\s+-\s+", "-", text)
    text = re.sub(r"[^\w\s-]", "", text)
    text = re.sub(r"[\s_]+", "-", text)
    text = re.sub(r"-{2,}", "-", text)
    return text.strip("-")[:max_len]


def make_dest_path(body, doc_type, meeting_date, output_dir, suffix=""):
    date_prefix = meeting_date.strftime("%Y-%m-%d")
    month_dir = meeting_date.strftime("%Y-%m")
    body_slug = slugify(body, max_len=45)
    month_path = os.path.join(output_dir, month_dir)
    os.makedirs(month_path, exist_ok=True)
    fname = f"{date_prefix}-{body_slug}-{doc_type}{suffix}.pdf"
    return os.path.join(month_path, fname)


# --- Main ---

def main():
    parser = argparse.ArgumentParser(
        description=(
            "Download Bridgeport CT municipal agendas and minutes "
            "for meetings within the past N days."
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
        help="Only include bodies whose name contains NAME (case-insensitive)",
    )
    args = parser.parse_args()

    if datetime.date.today().weekday() in (6, 0):  # Sunday, Monday
        print("Skipping — no downloads on Sunday or Monday.")
        sys.exit(0)

    today = datetime.date.today()
    cutoff = today - datetime.timedelta(days=args.days)
    future_limit = today + datetime.timedelta(days=args.ahead)

    print(f"Date window : {cutoff} to {future_limit}")
    print(f"Site        : {BASE_URL}")
    if not args.dry_run:
        print(f"Output dir  : {args.output_dir}")
    print()

    # --- Step 1: fetch all events from API ---
    print("Fetching events from Legistar API...")
    events = fetch_events()
    if events is None:
        print("ERROR: Could not fetch events from API.", file=sys.stderr)
        sys.exit(1)
    print(f"  API returned {len(events)} event(s) total.")
    print()

    # --- Step 2: filter by date window and collect documents ---
    matches = []
    seen_urls = set()

    for event in events:
        meeting_date = parse_event_date(event)
        if not meeting_date:
            continue

        if meeting_date < cutoff or meeting_date > future_limit:
            continue

        body = event.get("EventBodyName", "Unknown")

        if args.board and args.board.lower() not in body.lower():
            continue

        agenda_url = event.get("EventAgendaFile") or ""
        minutes_url = event.get("EventMinutesFile") or ""

        if agenda_url and agenda_url not in seen_urls:
            seen_urls.add(agenda_url)
            matches.append({
                "body": body,
                "date": meeting_date,
                "doc_type": "agenda",
                "url": agenda_url,
                "event_id": event.get("EventId"),
            })

        if minutes_url and minutes_url not in seen_urls:
            seen_urls.add(minutes_url)
            matches.append({
                "body": body,
                "date": meeting_date,
                "doc_type": "minutes",
                "url": minutes_url,
                "event_id": event.get("EventId"),
            })

    # Detect duplicate (body, doc_type, date) combos and assign suffixes
    seen_keys: dict = {}
    for m in matches:
        key = (m["body"], m["doc_type"], m["date"])
        seen_keys[key] = seen_keys.get(key, 0) + 1
    key_counter: dict = {}
    for m in matches:
        key = (m["body"], m["doc_type"], m["date"])
        if seen_keys[key] > 1:
            key_counter[key] = key_counter.get(key, 0) + 1
            m["suffix"] = f"-{key_counter[key]}"
        else:
            m["suffix"] = ""

    matches.sort(key=lambda x: (x["date"], x["body"]), reverse=True)

    print(
        f"Found {len(matches)} document(s) across "
        f"{len({m['body'] for m in matches})} body/bodies."
    )
    print()

    if not matches:
        return

    if args.dry_run:
        print(f"{'Body':<48} {'Date':<12} Type")
        print("-" * 72)
        for m in matches:
            print(f"{m['body'][:47]:<48} {m['date']!s:<12} {m['doc_type']}")
        print(f"\n{len(matches)} document(s). Re-run without --dry-run to download.")
        return

    # --- Step 3: download ---
    os.makedirs(args.output_dir, exist_ok=True)
    log_path = os.path.join(args.output_dir, "download-log.txt")
    log_lines = []
    downloaded = skipped = failed = 0

    for m in matches:
        dest = make_dest_path(
            m["body"], m["doc_type"], m["date"],
            args.output_dir, suffix=m.get("suffix", ""),
        )
        label = os.path.basename(dest)

        if os.path.exists(dest):
            print(f"  skip (exists)  {label}")
            skipped += 1
            continue

        print(f"  [{m['date']}] {m['body']} — {m['doc_type']}")
        print(f"  downloading    {label}")

        if download_file(m["url"], dest):
            downloaded += 1
            log_lines.append(
                f"{datetime.datetime.now().isoformat()}  OK       {dest}"
            )
        else:
            failed += 1
            log_lines.append(
                f"{datetime.datetime.now().isoformat()}  FAILED   {m['url']}"
            )
            if os.path.exists(dest):
                os.remove(dest)

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
#    python3 scripts/download-bridgeport-agendas.py --dry-run
#
# 2. Narrow to one body:
#    python3 scripts/download-bridgeport-agendas.py --board "City Council"
#
# 3. Change the lookback window:
#    python3 scripts/download-bridgeport-agendas.py --days 7
#
# 4. Save files somewhere else:
#    python3 scripts/download-bridgeport-agendas.py --output-dir ~/Downloads/bridgeport
#
# 5. Run on a schedule (cron — 8 AM daily):
#    0 8 * * * cd /path/to/repo && python3 scripts/download-bridgeport-agendas.py
#
# 6. Process downloaded files with Claude afterward:
#    python3 scripts/download-bridgeport-agendas.py && bash scripts/batch-process.sh beat-archive/bridgeport-agendas/
#
# NOTE: The --ahead flag (default: 7 days) captures agendas for upcoming meetings
# that have already been published. Run daily to stay current.
#
# NOTE: Bridgeport CT uses Legistar (bridgeportct.legistar.com) for City Council
# meetings. The Legistar REST API at webapi.legistar.com/v1/bridgeportct only
# returns events with published agenda documents; scheduled meetings without a
# published agenda do not appear in the API response.
#
# NOTE: The Bridgeport Legistar instance currently covers City Council meetings
# only. Other municipal bodies (boards, commissions, committees) do not appear
# to publish documents through Legistar at this time.
#
# NOTE: PDF filenames follow the pattern:
#   YYYY-MM-DD-{board-slug}-{agenda|minutes}.pdf
# e.g.: 2026-04-21-city-council-agenda.pdf
