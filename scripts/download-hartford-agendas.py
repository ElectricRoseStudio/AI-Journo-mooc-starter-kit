#!/usr/bin/env python3
"""Download municipal meeting agendas and minutes from Hartford, CT (CivicWeb/iCompass).

Documents are fetched via the CivicWeb REST API:
  /Services/MeetingsService.svc/meetings/{meetingId}/meetingDocuments

Agenda PDFs (DocumentType 4) are downloaded as .pdf files.
Minutes (DocumentType 9) are saved as .html files (no PDF version exists in the system).

Meeting recordings are not stored in CivicWeb; Hartford's channel is on YouTube:
  https://youtube.com/channel/UCfNNyG4keplIyA11WcOXbqw
"""

import argparse
import gzip
import json
import os
import re
import time
import urllib.error
import urllib.request
import zlib
from datetime import date, datetime, timedelta

BASE_URL = "https://hartford.civicweb.net"
PORTAL_URL = f"{BASE_URL}/Portal"
MEETINGS_API = f"{BASE_URL}/Services/MeetingsService.svc/meetings"
OUTPUT_DIR = "beat-archive/hartford-agendas"
DAYS_BACK = 4
MIN_DATE = date(2020, 1, 1)

DOCTYPE_AGENDA_PDF = 4
DOCTYPE_MINUTES_HTML = 9

_HEADERS = {
    "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
    "Accept-Encoding": "gzip, deflate",
    "Referer": "https://hartford.civicweb.net/Portal/MeetingTypeList.aspx",
    "Sec-Fetch-Dest": "document",
    "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-Site": "same-origin",
    "Upgrade-Insecure-Requests": "1",
}

_JSON_HEADERS = {
    "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Accept": "application/json",
    "Accept-Encoding": "gzip, deflate",
    "Referer": "https://hartford.civicweb.net/Portal/MeetingTypeList.aspx",
    "X-Requested-With": "XMLHttpRequest",
}

# Meeting type ID → (slug, display name)
MEETING_TYPES = {
    10: ("city-council", "City Council"),
    16: ("public-hearing", "Public Hearing"),
    17: ("quality-of-life-committee", "Quality of Life & Public Safety Committee"),
    18: ("budget-hearing", "Budget Hearing"),
    19: ("health-human-services-committee", "Health & Human Services Committee"),
    20: ("public-works-committee", "Public Works, Parks, Recreation & Environment Committee"),
    22: ("planning-economic-development-committee", "Planning, Economic Development & Housing Committee"),
    23: ("operations-budget-committee", "Operations, Management, Budget & Government Accountability"),
    24: ("labor-education-committee", "Labor, Education, Workforce & Youth Development Committee"),
    27: ("planning-zoning-commission", "Planning & Zoning and Inland Wetlands Commission"),
    30: ("solid-waste-taskforce", "Solid Waste TaskForce"),
    31: ("human-relations-commission", "Human Relations Commission"),
    32: ("lgbtq-commission", "LGBTQ+ Commission"),
    33: ("civilian-police-review-board", "Civilian Police Review Board"),
    34: ("commission-disability-issues", "Commission on Disability Issues"),
    36: ("pcshw", "Permanent Commission on the Status of Hartford Women"),
    37: ("advisory-commission-food-policy", "Advisory Commission on Food Policy"),
    40: ("board-of-education", "Board of Education"),
    43: ("dedication-committee", "Dedication Committee"),
    46: ("ethics-commission", "Ethics Commission"),
    47: ("fair-rent-commission", "Fair Rent Commission"),
    49: ("golf-course-oversight-commission", "Golf Course Oversight Commission"),
    51: ("greater-hartford-flood-commission", "Greater Hartford Flood Commission"),
    55: ("film-media-commission", "Film & Media Commission"),
    56: ("housing-authority", "Housing Authority"),
    59: ("historic-properties-commission", "Historic Properties/Preservation Commission"),
    61: ("internal-audit-commission", "Internal Audit Commission"),
    63: ("parks-recreation-advisory-commission", "Parks and Recreation Advisory Commission"),
    65: ("charter-revision-commission", "Charter Revision Commission"),
    67: ("police-accountability-review-board", "Police Accountability Review Board"),
    68: ("redevelopment-agency", "Redevelopment Agency"),
    69: ("commission-refugee-immigrant-affairs", "Commission on Refugee and Immigrant Affairs"),
    70: ("zoning-board-of-appeals", "Zoning Board of Appeals"),
    71: ("tree-advisory-commission", "Tree Advisory Commission"),
    72: ("hartford-school-building-committee", "Hartford School Building Committee"),
    73: ("complete-streets-taskforce", "Complete Streets Task Force"),
}

# Parses "May 11 2026" or "Dec 08 2025" style dates from meeting button HTML
_BUTTON_RE = re.compile(
    r'MeetingButton(\d+)[^>]+>.*?'
    r'<div[^>]*meeting-list-item-button-date[^>]*>([^<]+)</div>',
    re.DOTALL | re.IGNORECASE,
)

# Extracts type ID → most recent meeting ID from MeetingTypeList
_TYPE_MEETING_RE = re.compile(
    r"MeetingInformation\.aspx\?type=(\d+)[^<]*<.*?"
    r"MeetingInformation\.aspx\?Id=(\d+)",
    re.DOTALL | re.IGNORECASE,
)


def _decompress(raw, enc):
    enc = enc.lower()
    if enc == "gzip":
        return gzip.decompress(raw)
    if enc == "deflate":
        return zlib.decompress(raw)
    return raw


def fetch_html(url, retries=3):
    req = urllib.request.Request(url, headers=_HEADERS)
    for attempt in range(retries + 1):
        try:
            with urllib.request.urlopen(req, timeout=60) as r:
                raw = _decompress(r.read(), r.headers.get("Content-Encoding", ""))
                return raw.decode("utf-8", errors="replace")
        except (urllib.error.URLError, TimeoutError, OSError) as e:
            if attempt < retries:
                time.sleep(3 * (attempt + 1))
            else:
                print(f"    Error fetching {url}: {e}")
    return None


def fetch_json(url, retries=3):
    req = urllib.request.Request(url, headers=_JSON_HEADERS)
    for attempt in range(retries + 1):
        try:
            with urllib.request.urlopen(req, timeout=60) as r:
                raw = _decompress(r.read(), r.headers.get("Content-Encoding", ""))
                return json.loads(raw.decode("utf-8-sig", errors="replace"))
        except (urllib.error.URLError, TimeoutError, OSError) as e:
            if attempt < retries:
                time.sleep(3 * (attempt + 1))
            else:
                print(f"    Error fetching {url}: {e}")
        except json.JSONDecodeError as e:
            print(f"    JSON parse error for {url}: {e}")
            return None
    return None


def download_file(url, out_path, retries=3):
    req = urllib.request.Request(url, headers=_HEADERS)
    for attempt in range(retries + 1):
        try:
            with urllib.request.urlopen(req, timeout=60) as r:
                data = r.read()
            os.makedirs(os.path.dirname(out_path), exist_ok=True)
            with open(out_path, "wb") as f:
                f.write(data)
            return True
        except (urllib.error.URLError, TimeoutError, OSError) as e:
            if attempt < retries:
                time.sleep(3 * (attempt + 1))
            else:
                print(f"    Error downloading {url}: {e}")
    return False


def get_type_latest_meeting_ids():
    """Return {typeId: latest_meeting_id} from the MeetingTypeList page."""
    html = fetch_html(f"{PORTAL_URL}/MeetingTypeList.aspx")
    if not html:
        return {}
    result = {}
    for type_id, meeting_id in _TYPE_MEETING_RE.findall(html):
        tid = int(type_id)
        mid = int(meeting_id)
        if tid not in result:
            result[tid] = mid
    return result


def get_meetings_for_type(latest_meeting_id):
    """Fetch the full meeting list page and return [(meeting_id, meeting_date)] sorted newest first."""
    url = f"{PORTAL_URL}/MeetingInformation.aspx?Id={latest_meeting_id}"
    html = fetch_html(url)
    if not html:
        return []

    meetings = []
    for mid_str, date_str in _BUTTON_RE.findall(html):
        try:
            mtg_date = datetime.strptime(date_str.strip(), "%b %d %Y").date()
            meetings.append((int(mid_str), mtg_date))
        except ValueError:
            pass  # skip unparseable dates

    # Deduplicate by meeting ID, preserve order (newest first in HTML)
    seen = set()
    deduped = []
    for mid, d in meetings:
        if mid not in seen:
            seen.add(mid)
            deduped.append((mid, d))
    return deduped


def get_meeting_documents(meeting_id):
    """Return list of document dicts from the CivicWeb REST API."""
    url = f"{MEETINGS_API}/{meeting_id}/meetingDocuments"
    return fetch_json(url) or []


def process_type(type_id, slug, display_name, latest_meeting_id, cutoff_back, cutoff_ahead,
                 no_agendas=False, no_minutes=False, dry_run=False, verbose=False,
                 output_dir=OUTPUT_DIR):
    meetings = get_meetings_for_type(latest_meeting_id)
    if not meetings:
        return 0

    total = 0
    seen_docs = set()

    for meeting_id, mtg_date in meetings:
        if mtg_date < cutoff_back:
            break  # meetings are newest-first; stop when we go past cutoff
        if mtg_date > cutoff_ahead:
            continue

        docs = get_meeting_documents(meeting_id)
        time.sleep(0.2)

        for doc in docs:
            doc_id = doc.get("Id")
            doc_type = doc.get("DocumentType")
            item_type = doc.get("Type")  # 1=agenda, 2=minutes

            if not doc_id or doc_id in seen_docs:
                continue
            seen_docs.add(doc_id)

            if doc_type == DOCTYPE_AGENDA_PDF:
                if no_agendas:
                    continue
                ext = "pdf"
                label = "agenda"
            elif doc_type == DOCTYPE_MINUTES_HTML:
                if no_minutes:
                    continue
                ext = "html"
                label = "minutes"
            else:
                continue  # skip HTML agenda views (DocumentType 1) and others

            month_dir = os.path.join(output_dir, mtg_date.strftime("%Y-%m"))
            filename = f"{mtg_date.isoformat()}_{slug}_{label}_{doc_id}.{ext}"
            out_path = os.path.join(month_dir, filename)

            if os.path.exists(out_path):
                if verbose:
                    print(f"    skip: {filename}")
                continue

            if dry_run:
                print(f"    [dry] {filename}")
                total += 1
                continue

            doc_url = f"{BASE_URL}/document/{doc_id}"
            if ext == "html":
                doc_url += "/"  # minutes HTML requires trailing slash

            if verbose:
                print(f"    download: {filename}")

            if download_file(doc_url, out_path):
                total += 1
                time.sleep(0.3)
            else:
                print(f"    Warning: failed {filename}")

    return total


def main():
    ap = argparse.ArgumentParser(description="Download Hartford CT meeting agendas and minutes (CivicWeb)")
    ap.add_argument("--days", type=int, default=DAYS_BACK,
                    help=f"Days back to fetch (default: {DAYS_BACK})")
    ap.add_argument("--ahead", type=int, default=90,
                    help="Days ahead for upcoming agendas (default: 90)")
    ap.add_argument("--all", action="store_true",
                    help=f"Fetch all docs back to {MIN_DATE}")
    ap.add_argument("--dry-run", action="store_true",
                    help="List what would be downloaded without downloading")
    ap.add_argument("--board", metavar="SLUG",
                    help="Only process this board (e.g. city-council)")
    ap.add_argument("--no-agendas", action="store_true", help="Skip agenda PDFs")
    ap.add_argument("--no-minutes", action="store_true", help="Skip minutes HTML")
    ap.add_argument("--verbose", "-v", action="store_true")
    ap.add_argument("--output-dir", default=OUTPUT_DIR,
                    help=f"Output directory (default: {OUTPUT_DIR})")
    args = ap.parse_args()

    cutoff_back = MIN_DATE if args.all else date.today() - timedelta(days=args.days)
    cutoff_ahead = date.today() + timedelta(days=args.ahead)

    os.makedirs(args.output_dir, exist_ok=True)

    # Determine which types to process
    if args.board:
        types_to_run = {
            tid: (slug, name) for tid, (slug, name) in MEETING_TYPES.items()
            if slug == args.board
        }
        if not types_to_run:
            print(f"Board '{args.board}' not found. Available slugs:")
            for tid, (slug, name) in sorted(MEETING_TYPES.items()):
                print(f"  {slug}  ({name})")
            return
    else:
        types_to_run = {tid: (slug, name) for tid, (slug, name) in MEETING_TYPES.items()}

    print(f"Fetching meeting type list...")
    type_latest = get_type_latest_meeting_ids()

    total = 0
    for type_id, (slug, display_name) in sorted(types_to_run.items(), key=lambda x: x[1][1]):
        latest_id = type_latest.get(type_id)
        if not latest_id:
            if args.verbose:
                print(f"{display_name}: no recent meetings found, skipping")
            continue

        print(f"{display_name}...")
        n = process_type(
            type_id, slug, display_name, latest_id,
            cutoff_back, cutoff_ahead,
            no_agendas=args.no_agendas,
            no_minutes=args.no_minutes,
            dry_run=args.dry_run,
            verbose=args.verbose,
            output_dir=args.output_dir,
        )
        print(f"  {n}")
        total += n
        time.sleep(0.5)

    label = "Would download" if args.dry_run else "Downloaded"
    print(f"\n{label} {total} documents total.")
    print(f"\nNote: Hartford meeting recordings are on YouTube:")
    print(f"  https://youtube.com/channel/UCfNNyG4keplIyA11WcOXbqw")


if __name__ == "__main__":
    main()
