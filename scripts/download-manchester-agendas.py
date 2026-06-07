#!/usr/bin/env python3
# download-manchester-agendas.py
# Download municipal meeting agendas, minutes, and video recordings from
# Manchester CT for meetings whose date falls within the past N days
# (and up to 7 days ahead, to catch agendas posted early).
#
# USAGE:
#   python3 scripts/download-manchester-agendas.py [options]
#
# REQUIREMENTS:
#   - Python 3.6+  (no third-party packages needed)
#   - Internet connection
#
# WHAT IT DOES:
#   1. Fetches all Agendas and Minutes board categories from the General Code
#      eCode360 API (custId MA2034)
#   2. For each category, retrieves the current and/or prior year document list
#   3. Parses meeting dates from document titles (format: M.D.YYYY or M.D.YY)
#   4. Downloads matching PDFs to beat-archive/manchester-agendas/YYYY-MM/
#   5. Queries the Cablecast VOD API (channel16.org) for meeting recordings
#   6. Downloads matching video recordings as MP4 files
#   7. Appends a log to beat-archive/manchester-agendas/download-log.txt
#
# SITE STRUCTURE:
#   Documents (General Code eCode360, custId MA2034):
#     Types:        GET https://ecode360.com/api/location/MA2034/pub-doc/types
#                   → typeId 220 = Agendas, typeId 678 = Minutes
#     Categories:   GET https://ecode360.com/api/location/MA2034/pub-doc/type/{typeId}/categories
#     Docs by year: GET https://ecode360.com/api/location/MA2034/pub-doc/category/{catId}/year/{year}/children
#                   Returns [{type:"document", key:"{docKey}", title:"Agenda - 03.03.2026"}]
#     Download:     GET https://ecode360.com/api/MA2034/pub-doc/{docKey}/download
#                   (or https://ecode360.com/documents/MA2034/public/{docKey}.pdf)
#     Date format:  Encoded in title as M.D.YYYY or M.D.YY (zero-padding optional)
#
#   Video recordings (Cablecast, channel16.org):
#     Shows:   GET http://www.channel16.org/cablecastapi/v1/shows?site=1&offset=N
#              Returns up to 50 shows per page, sorted newest-first (1,310 total as of 2026)
#              Title format: "MTG-BOD-2026-05-05" (type-board-YYYY-MM-DD)
#              eventDate contains ISO 8601 datetime
#     VOD:     GET http://www.channel16.org/cablecastapi/v1/vods/{vodId}
#     MP4 URL: vod["url"] on reflect-channel16.cablecast.tv
#
# AGENDAS BOARDS (51 categories, typeId=220):
#   21st Century Senior Center Task Force, Advisory Board of Health,
#   Advisory Parks/Recreation/Leisure Services Commission, Bennet Housing Corporation,
#   Board of Assessment Appeals, Board of Directors, Building Committee,
#   Case Cabin Committee, Charter Review Commission, Cheney Brothers Historic District,
#   Civilian Police Review Board, Commission on Elderly, Conservation Commission,
#   Economic Development Commission, Ethics Commission, Golf Course Oversight Committee,
#   Housing Commission, Housing and Fair Rent Commission, Land Acquisition Committee,
#   Library Board, Library Building Committee, Pension Board, Planning and Zoning Commission,
#   Redevelopment Agency, Sustainability Commission, Veterans Advisory Committee,
#   Youth Commission, Zoning Board of Appeals, and more.
#
# MINUTES BOARDS (44 categories, typeId=678): same boards, titled "Board of Directors: Actions",
#   "Planning and Zoning Commission/IWA/APA: Actions", etc.

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

ECODE_BASE = "https://ecode360.com"
CUST_ID = "MA2034"
TYPE_AGENDAS = 220
TYPE_MINUTES = 678

CABLECAST_BASE = "http://www.channel16.org"
CABLECAST_REFLECT = "https://reflect-channel16.cablecast.tv"
CABLECAST_SITE = 1

OUTPUT_DIR = "beat-archive/manchester-agendas"
DAYS_BACK = 4
DAYS_AHEAD = 7
PAGE_DELAY = 0.5
DOWNLOAD_DELAY = 0.8

UA = "Mozilla/5.0 (X11; Linux x86_64; rv:120.0) Gecko/20100101 Firefox/120.0"
UA_VIDEO = "Manchester-CT-Agendas-Downloader/1.0 (journalism research)"


# --- HTTP helpers ---

def fetch_json(url, ua=UA):
    req = urllib.request.Request(
        url,
        headers={"User-Agent": ua, "Accept": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return json.loads(r.read())
    except urllib.error.HTTPError as e:
        print(f"  HTTP {e.code} — {url}", file=sys.stderr)
        return None
    except Exception as e:
        print(f"  ERROR {url}: {e}", file=sys.stderr)
        return None


def download_file(url, dest_path):
    """Download url to dest_path, streaming in chunks. Returns True on success."""
    req = urllib.request.Request(
        url,
        headers={"User-Agent": UA, "Accept": "application/pdf,video/mp4,*/*"},
    )
    try:
        with urllib.request.urlopen(req, timeout=120) as r:
            with open(dest_path, "wb") as f:
                while True:
                    chunk = r.read(65536)
                    if not chunk:
                        break
                    f.write(chunk)
        return True
    except Exception as e:
        print(f"  WARNING: {e}", file=sys.stderr)
        return False


# --- Date parsing ---

# Matches M.D.YYYY, M.D.YY, MM.DD.YYYY, etc. in document titles
_DATE_RE = re.compile(r"\b(\d{1,2})\.(\d{1,2})\.(\d{2,4})\b")


def parse_date_from_title(title):
    """
    Extract a meeting date from a General Code document title.

    Observed formats (zero-padding is inconsistent):
      "Agenda - 03.03.2026"   → 2026-03-03
      "Agenda - 1.13.2026"    → 2026-01-13
      "Agenda - 05.5.2026"    → 2026-05-05
      "Actions 4.07.2026"     → 2026-04-07
      "Agenda - 01.05.26"     → 2026-01-05  (2-digit year assumed 2000+)
      "Minutes - 01.03.12"    → 2012-01-03  (treated as 2012)

    Returns a datetime.date or None if no date found.
    """
    m = _DATE_RE.search(title)
    if not m:
        return None
    month, day, year = int(m.group(1)), int(m.group(2)), int(m.group(3))
    if year < 100:
        year += 2000
    try:
        return datetime.date(year, month, day)
    except ValueError:
        return None


# --- General Code document API ---

def fetch_categories(type_id):
    """
    Return all board/committee categories for a given document type.
    Each item: {type, key, title, hideNumber, context}.
    """
    url = f"{ECODE_BASE}/api/location/{CUST_ID}/pub-doc/type/{type_id}/categories"
    return fetch_json(url) or []


def fetch_docs_for_year(cat_key, year):
    """
    Return all documents in a category for a given year.
    Each item: {type, key, title, hideNumber}.
    """
    url = (
        f"{ECODE_BASE}/api/location/{CUST_ID}/pub-doc/category/{cat_key}"
        f"/year/{year}/children"
    )
    return fetch_json(url) or []


def collect_docs(categories, cutoff, future_limit, skip_agendas=False, skip_minutes=False):
    """
    Iterate all categories, fetching documents for each relevant year.
    Returns list of dicts:
      {board, doc_type, doc_key, doc_title, meeting_date}
    """
    today = datetime.date.today()
    years_needed = {cutoff.year, future_limit.year, today.year}

    all_docs = []
    total = len(categories)

    for i, cat in enumerate(categories, 1):
        cat_title = cat.get("title", "Unknown")
        cat_key = cat["key"]

        # Infer doc_type from category title (both typeIds mix actions/agendas/minutes)
        title_lower = cat_title.lower()
        if any(x in title_lower for x in ["action", "minute", "resolution"]):
            doc_type = "minutes"
        else:
            doc_type = "agenda"

        if skip_agendas and doc_type == "agenda":
            continue
        if skip_minutes and doc_type == "minutes":
            continue

        print(f"  [{i:>2}/{total}] {cat_title}...", end=" ", flush=True)
        found = 0

        for year in sorted(years_needed):
            docs = fetch_docs_for_year(cat_key, year)
            time.sleep(PAGE_DELAY)

            for doc in docs:
                if doc.get("type") != "document":
                    continue
                doc_date = parse_date_from_title(doc["title"])
                if doc_date and cutoff <= doc_date <= future_limit:
                    all_docs.append({
                        "board": cat_title,
                        "doc_type": doc_type,
                        "doc_key": doc["key"],
                        "doc_title": doc["title"],
                        "meeting_date": doc_date,
                    })
                    found += 1

        print(f"{found} doc(s)")

    return all_docs


# --- Cablecast video API ---

def fetch_shows_in_window(cutoff, future_limit):
    """
    Page through the Cablecast shows API and return all shows whose
    eventDate falls in [cutoff, future_limit]. Stops once we've
    gone past the cutoff window.
    """
    shows = []
    offset = 0
    total = None

    while True:
        url = f"{CABLECAST_BASE}/cablecastapi/v1/shows?site={CABLECAST_SITE}&offset={offset}"
        data = fetch_json(url, ua=UA_VIDEO)
        if not data or "shows" not in data:
            break

        if total is None:
            total = data.get("meta", {}).get("count", 0)

        page_shows = data["shows"]
        if not page_shows:
            break

        oldest_date = None
        for show in page_shows:
            raw_date = show.get("eventDate", "")
            try:
                event_date = datetime.date.fromisoformat(raw_date[:10])
            except (ValueError, TypeError):
                continue

            if cutoff <= event_date <= future_limit and show.get("title") and show.get("vods"):
                shows.append({
                    "id": show["id"],
                    "title": show["title"],
                    "event_date": event_date,
                    "vod_ids": show["vods"],
                })

            if oldest_date is None or event_date < oldest_date:
                oldest_date = event_date

        if oldest_date and oldest_date < cutoff:
            break

        offset += len(page_shows)
        if offset >= (total or 0):
            break

        time.sleep(PAGE_DELAY)

    return shows


def get_vod_url(vod_id):
    """Return the direct MP4 URL for a Cablecast VOD, or None."""
    url = f"{CABLECAST_BASE}/cablecastapi/v1/vods/{vod_id}"
    data = fetch_json(url, ua=UA_VIDEO)
    if not data:
        return None
    vod = data.get("vod", data) if isinstance(data, dict) else None
    if not vod:
        return None
    vod_url = vod.get("url")
    if vod_url and vod.get("vodState") == "complete" and not vod.get("disabled"):
        return vod_url
    return None


# --- File naming ---

def slugify(text, max_len=50):
    text = text.lower().strip()
    text = re.sub(r"[/\\&]", "-", text)
    text = re.sub(r"\s+-\s+", "-", text)
    text = re.sub(r"[^\w\s-]", "", text)
    text = re.sub(r"[\s_]+", "-", text)
    text = re.sub(r"-{2,}", "-", text)
    return text.strip("-")[:max_len]


def make_doc_path(board, doc_type, meeting_date, output_dir, counter=0):
    date_str = meeting_date.strftime("%Y-%m-%d")
    month_dir = os.path.join(output_dir, meeting_date.strftime("%Y-%m"))
    os.makedirs(month_dir, exist_ok=True)
    board_slug = slugify(board)
    suffix = f"-{counter}" if counter > 0 else ""
    return os.path.join(month_dir, f"{date_str}-{board_slug}-{doc_type}{suffix}.pdf")


def make_video_path(title, event_date, output_dir, counter=0):
    date_str = event_date.strftime("%Y-%m-%d")
    month_dir = os.path.join(output_dir, event_date.strftime("%Y-%m"))
    os.makedirs(month_dir, exist_ok=True)
    title_slug = slugify(title)
    suffix = f"-{counter}" if counter > 0 else ""
    return os.path.join(month_dir, f"{date_str}-{title_slug}{suffix}.mp4")


# --- Main ---

def main():
    parser = argparse.ArgumentParser(
        description=(
            "Download Manchester CT municipal agendas, minutes, and video recordings "
            "for meetings within the past N days."
        )
    )
    parser.add_argument(
        "--days", type=int, default=DAYS_BACK, metavar="N",
        help=f"Look back N days by meeting date (default: {DAYS_BACK})",
    )
    parser.add_argument(
        "--ahead", type=int, default=DAYS_AHEAD, metavar="N",
        help=f"Include upcoming meetings up to N days ahead (default: {DAYS_AHEAD})",
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
        help="Only fetch boards whose name contains NAME (case-insensitive)",
    )
    parser.add_argument(
        "--docs-only", action="store_true",
        help="Download PDFs only, skip video recordings",
    )
    parser.add_argument(
        "--videos-only", action="store_true",
        help="Download video recordings only, skip PDFs",
    )
    parser.add_argument(
        "--no-minutes", action="store_true",
        help="Skip minutes/actions, download agendas only",
    )
    parser.add_argument(
        "--no-agendas", action="store_true",
        help="Skip agendas, download minutes/actions only",
    )
    args = parser.parse_args()

    today = datetime.date.today()
    cutoff = today - datetime.timedelta(days=args.days)
    future_limit = today + datetime.timedelta(days=args.ahead)

    print(f"Date window : {cutoff} to {future_limit}")
    if not args.dry_run:
        print(f"Output dir  : {args.output_dir}")
    print()

    log_lines = []
    downloaded = skipped = failed = 0
    all_docs: list = []
    shows: list = []

    # ------------------------------------------------------------------ #
    # Part 1: PDFs (General Code eCode360)                                #
    # ------------------------------------------------------------------ #

    if not args.videos_only:
        print("Fetching document categories from eCode360 (MA2034)...")
        agenda_cats = fetch_categories(TYPE_AGENDAS) if not args.no_agendas else []
        minutes_cats = fetch_categories(TYPE_MINUTES) if not args.no_minutes else []

        if args.board:
            filt = args.board.lower()
            agenda_cats = [c for c in agenda_cats if filt in c["title"].lower()]
            minutes_cats = [c for c in minutes_cats if filt in c["title"].lower()]

        print(f"  Agendas: {len(agenda_cats)} categories,  Minutes: {len(minutes_cats)} categories")
        print()

        if agenda_cats:
            print(f"Fetching Agendas ({len(agenda_cats)} categories)...")
            all_docs.extend(
                collect_docs(
                    agenda_cats, cutoff, future_limit,
                    skip_agendas=args.no_agendas,
                    skip_minutes=args.no_minutes,
                )
            )
            print()

        if minutes_cats:
            print(f"Fetching Minutes ({len(minutes_cats)} categories)...")
            all_docs.extend(
                collect_docs(
                    minutes_cats, cutoff, future_limit,
                    skip_agendas=args.no_agendas,
                    skip_minutes=args.no_minutes,
                )
            )
            print()

        all_docs.sort(key=lambda x: (x["meeting_date"], x["board"]), reverse=True)

        print(
            f"Found {len(all_docs)} PDF document(s) across "
            f"{len({d['board'] for d in all_docs})} board(s)."
        )
        print()

        if args.dry_run and all_docs:
            print(f"{'Board':<44} {'Date':<12} Type    Document title")
            print("-" * 95)
            for d in all_docs:
                print(
                    f"{d['board'][:43]:<44} {d['meeting_date']!s:<12} "
                    f"{d['doc_type']:<8} {d['doc_title'][:35]}"
                )

        elif all_docs:
            os.makedirs(args.output_dir, exist_ok=True)
            fname_counters: dict = {}

            for d in all_docs:
                key = (slugify(d["board"]), d["meeting_date"], d["doc_type"])
                fname_counters[key] = fname_counters.get(key, 0) + 1
                counter = fname_counters[key] - 1

                dest = make_doc_path(
                    d["board"], d["doc_type"], d["meeting_date"],
                    args.output_dir, counter,
                )
                label = os.path.basename(dest)

                if os.path.exists(dest):
                    print(f"  skip (exists)  {label}")
                    skipped += 1
                    continue

                doc_url = f"{ECODE_BASE}/api/{CUST_ID}/pub-doc/{d['doc_key']}/download"
                print(f"  [{d['meeting_date']}] {d['board']} — {d['doc_type']}")
                print(f"  downloading    {label}")

                if download_file(doc_url, dest):
                    downloaded += 1
                    log_lines.append(
                        f"{datetime.datetime.now().isoformat()}  OK       {dest}"
                    )
                else:
                    failed += 1
                    log_lines.append(
                        f"{datetime.datetime.now().isoformat()}  FAILED   {doc_url}"
                    )
                    if os.path.exists(dest):
                        os.remove(dest)

                time.sleep(DOWNLOAD_DELAY)

    # ------------------------------------------------------------------ #
    # Part 2: Video recordings (Cablecast channel16.org)                  #
    # ------------------------------------------------------------------ #

    if not args.docs_only:
        print()
        print("Querying Cablecast VOD API (channel16.org) for recordings...")
        shows = fetch_shows_in_window(cutoff, future_limit)

        if args.board:
            filt = args.board.lower()
            shows = [s for s in shows if filt in s["title"].lower()]

        shows.sort(key=lambda s: s["event_date"], reverse=True)
        print(f"  Found {len(shows)} recording(s) in window.")
        print()

        if args.dry_run and shows:
            print(f"{'Date':<12} {'Title':<40} VOD IDs")
            print("-" * 65)
            for s in shows:
                print(f"{s['event_date']!s:<12} {s['title'][:39]:<40} {s['vod_ids']}")

        elif shows:
            os.makedirs(args.output_dir, exist_ok=True)
            title_counters: dict = {}

            for show in shows:
                for vod_id in show["vod_ids"]:
                    key = (slugify(show["title"]), show["event_date"])
                    title_counters[key] = title_counters.get(key, 0) + 1
                    counter = title_counters[key] - 1

                    dest = make_video_path(
                        show["title"], show["event_date"], args.output_dir, counter
                    )
                    label = os.path.basename(dest)

                    print(
                        f"  [{show['event_date']}] {show['title']} — VOD {vod_id}",
                        end=" ",
                        flush=True,
                    )

                    if os.path.exists(dest):
                        print(f"skip (exists): {label}")
                        skipped += 1
                        continue

                    vod_url = get_vod_url(vod_id)
                    if not vod_url:
                        print("no URL (still processing or disabled)")
                        failed += 1
                        log_lines.append(
                            f"{datetime.datetime.now().isoformat()}  NO_URL   "
                            f"vod_id={vod_id} show={show['title']}"
                        )
                        continue

                    print(f"downloading → {label}")
                    if download_file(vod_url, dest):
                        size_mb = os.path.getsize(dest) / (1024 * 1024)
                        downloaded += 1
                        log_lines.append(
                            f"{datetime.datetime.now().isoformat()}  OK       "
                            f"{dest}  ({size_mb:.0f} MB)"
                        )
                    else:
                        failed += 1
                        log_lines.append(
                            f"{datetime.datetime.now().isoformat()}  FAILED   {vod_url}"
                        )
                        if os.path.exists(dest):
                            os.remove(dest)

                    time.sleep(DOWNLOAD_DELAY)

    # ------------------------------------------------------------------ #
    # Summary and log                                                     #
    # ------------------------------------------------------------------ #

    if args.dry_run:
        total_found = len(all_docs) + len(shows)
        print(f"\n{total_found} item(s) matched. Re-run without --dry-run to download.")
        return

    if log_lines:
        log_path = os.path.join(args.output_dir, "download-log.txt")
        with open(log_path, "a") as f:
            f.write("\n".join(log_lines) + "\n")

    print()
    print(f"Done — downloaded: {downloaded}  skipped: {skipped}  failed: {failed}")
    if downloaded + skipped:
        print(f"Files in: {args.output_dir}")
    if log_lines:
        print(f"Log:      {os.path.join(args.output_dir, 'download-log.txt')}")


if __name__ == "__main__":
    main()


# --- Tips ---
#
# 1. Preview without downloading:
#    python3 scripts/download-manchester-agendas.py --dry-run
#
# 2. PDFs only (skip the large video files):
#    python3 scripts/download-manchester-agendas.py --docs-only
#
# 3. Videos only:
#    python3 scripts/download-manchester-agendas.py --videos-only
#
# 4. Filter to a single board:
#    python3 scripts/download-manchester-agendas.py --board "Board of Directors"
#    python3 scripts/download-manchester-agendas.py --board "Zoning"
#
# 5. Agendas only (no minutes/actions):
#    python3 scripts/download-manchester-agendas.py --no-minutes
#
# 6. Change the lookback window:
#    python3 scripts/download-manchester-agendas.py --days 7
#
# 7. Save files somewhere else:
#    python3 scripts/download-manchester-agendas.py --output-dir ~/Downloads/manchester
#
# 8. Run on a schedule (cron — 8 AM daily):
#    0 8 * * * cd /path/to/repo && python3 scripts/download-manchester-agendas.py --docs-only
#
# 9. Process downloaded PDFs with Claude afterward:
#    python3 scripts/download-manchester-agendas.py --docs-only && \
#      bash scripts/batch-process.sh beat-archive/manchester-agendas/
#
# TITLE DATE FORMAT NOTES:
#   Manchester clerks upload documents with inconsistent date formatting in
#   the file title. The date parser handles all observed variants:
#     "Agenda - 03.03.2026"  (MM.DD.YYYY)
#     "Agenda - 1.13.2026"   (M.DD.YYYY — leading zero omitted)
#     "Agenda - 05.5.2026"   (MM.D.YYYY — leading zero omitted on day)
#     "Actions 4.07.2026"    (no dash separator)
#     "Agenda - 01.05.26"    (MM.DD.YY — 2-digit year, assumed 2000s)
#   Documents whose title contains no parseable date are skipped with
#   a note in the log. Run --dry-run to see what gets included.
#
# VIDEO RECORDINGS NOTE:
#   Videos are typically 500 MB–3 GB per meeting. Channel 16 title codes:
#     BOD = Board of Directors
#     PZC = Planning and Zoning Commission
#     BOE = Board of Education
#     ZBA = Zoning Board of Appeals
#   Use --board to filter by these codes (e.g. --board BOD).
#
# GENERAL CODE DOCUMENT CATEGORIES (as of May 2026):
#   Agendas (typeId=220):  51 categories
#   Minutes (typeId=678):  44 categories
#   Public document URL:   https://www.manchesterct.gov/Government/Town-Leadership/
#                          Boards-Commissions-Committees/Agenda-Actions-and-Minutes
