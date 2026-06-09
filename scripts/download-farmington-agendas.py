#!/usr/bin/env python3
"""Download municipal meeting documents from Farmington, CT (CivicPlus CMS).

Agendas and minutes are downloaded as PDFs.
Meeting recording pages (YouTube links) are saved to recording-index.csv.
"""

import argparse
import csv
import gzip
import os
import re
import time
import urllib.error
import urllib.request
import zlib
from datetime import date, timedelta

BASE_URL = "https://www.farmington-ct.org"
OUTPUT_DIR = "beat-archive/farmington-agendas"
DAYS_BACK = 4
MIN_DATE = date(2020, 1, 1)

_HEADERS = {
    "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
    "Accept-Encoding": "gzip, deflate",
    "Referer": "https://www.farmington-ct.org/government/minutes-and-agendas",
    "Sec-Fetch-Dest": "document",
    "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-Site": "same-origin",
    "Upgrade-Insecure-Requests": "1",
}

# (slug, display name) — URL: /government/{slug}/minutes-and-agendas
AGENDA_BOARDS = [
    ("1928-building-committee", "1928 Building Committee"),
    ("ad-hoc-1928-building-committee", "Ad-Hoc 1928 Building Committee"),
    ("american-rescue-plan-act-arpa-ad-hoc-committee", "ARPA Ad-Hoc Committee"),
    ("aquifer-protection-agency", "Aquifer Protection Agency"),
    ("architectural-design-review-committee", "Architectural Design Review Committee"),
    ("auditor-ad-hoc-committee", "Auditor Ad-Hoc Committee"),
    ("bicycle-committee", "Bicycle Committee"),
    ("conservation-inland-wetlands-commission", "Conservation Inland Wetlands Commission"),
    ("economic-development-commission", "Economic Development Commission"),
    ("farmington-gateways-committee", "Farmington Gateways Committee"),
    ("historic-district-commission", "Historic District Commission"),
    ("housing-authority", "Housing Authority"),
    ("human-relations-commission", "Human Relations Commission"),
    ("land-acquisition-committee", "Land Acquisition Committee"),
    ("other-public-meeting-information", "Other Public Meeting Information"),
    ("racial-equality-taskforce", "Racial Equality Taskforce"),
    ("retirement-board", "Retirement Board"),
    ("town-council", "Town Council"),
    ("town-plan-zoning-commission", "Town Plan Zoning Commission"),
    ("traffic-review-board", "Traffic Review Board"),
    ("water-pollution-control-authority", "Water Pollution Control Authority"),
    ("zoning-board-of-appeals", "Zoning Board of Appeals"),
    ("2019-farmington-high-school-building-committee", "FHS Building Committee"),
]

# FHS subcommittees have a nested URL structure
_FHS_BASE = "/government/2019-farmington-high-school-building-committee/fhs-building-committee-subcommittee-information"
FHS_SUBCOMMITTEES = [
    ("fhs-communications-subcommittee", f"{_FHS_BASE}/communications-subcommittee-minutes-and-agendas", "FHS Communications Subcommittee"),
    ("fhs-financial-communication-subcommittee", f"{_FHS_BASE}/financial-communication-subcommittee-minutes-and-agendas", "FHS Financial Communication Subcommittee"),
    ("fhs-neighborhood-communication-subcommittee", f"{_FHS_BASE}/neighborhood-communication-subcommittee-minutes-and-agendas", "FHS Neighborhood Communication Subcommittee"),
    ("fhs-professional-partnership-subcommittee", f"{_FHS_BASE}/professional-partnership-subcommittee-minutes-and-agendas", "FHS Professional Partnership Subcommittee"),
    ("fhs-site-evaluation-subcommittee", f"{_FHS_BASE}/site-evaluation-subcommittee-minutes-and-agendas", "FHS Site Evaluation Subcommittee"),
]

# YouTube recording index pages
RECORDING_PAGES = [
    ("1928-building-committee", "1928 Building Committee", "/government/1928-building-committee/meeting-recordings"),
    ("architectural-design-review-committee", "Architectural Design Review Committee", "/government/architectural-design-review-committee/online-meeting-recordings"),
    ("bicycle-committee", "Bicycle Committee", "/government/bicycle-committee/online-meeting-recordings"),
    ("conservation-inland-wetlands-commission", "Conservation Inland Wetlands Commission", "/government/conservation-inland-wetlands-commission/online-meeting-recordings"),
    ("green-efforts-committee", "Green Efforts Committee", "/government/green-efforts-committee/online-meeting-recordings"),
    ("historic-district-commission", "Historic District Commission", "/government/historic-district-commission/online-meeting-recordings"),
    ("town-council", "Town Council", "/government/town-council/online-meeting-recordings"),
    ("town-council-vod", "Town Council (Video on Demand)", "/government/town-council/video-on-demand"),
    ("town-plan-zoning-commission", "Town Plan Zoning Commission", "/government/town-plan-zoning-commission/online-meeting-recordings"),
    ("water-pollution-control-authority", "Water Pollution Control Authority", "/government/water-pollution-control-authority/online-meeting-recordings"),
    ("zoning-board-of-appeals", "Zoning Board of Appeals", "/government/zoning-board-of-appeals/online-meeting-recordings"),
]

_TR_RE = re.compile(r"<tr[^>]*>(.*?)</tr>", re.DOTALL | re.IGNORECASE)
_START_DATE_RE = re.compile(r"itemprop='startDate'\s+datetime='(\d{4}-\d{2}-\d{2})", re.IGNORECASE)
_CELL_RE = re.compile(r"class='event_(agenda|minutes)'[^>]*>(.*?)</td>", re.DOTALL | re.IGNORECASE)
_DOC_LINK_RE = re.compile(r"href='(/home/showpublisheddocument/(\d+)/[^']+)'[^>]*>([^<]*)", re.IGNORECASE)
_LAST_PAGE_RE = re.compile(r"-npage-(\d+)[^>]*>\s*Last", re.IGNORECASE)
_YOUTUBE_RE = re.compile(r'href="(https://youtu(?:\.be|be\.com)/[^"]+)"[^>]*>([^<]+)<', re.IGNORECASE)


def fetch_html(url, retries=3):
    req = urllib.request.Request(url, headers=_HEADERS)
    for attempt in range(retries + 1):
        try:
            with urllib.request.urlopen(req, timeout=60) as r:
                raw = r.read()
                enc = r.headers.get("Content-Encoding", "").lower()
                if enc == "gzip":
                    raw = gzip.decompress(raw)
                elif enc == "deflate":
                    raw = zlib.decompress(raw)
                return raw.decode("utf-8", errors="replace")
        except (urllib.error.URLError, TimeoutError, OSError) as e:
            if attempt < retries:
                time.sleep(3 * (attempt + 1))
            else:
                print(f"    Error fetching {url}: {e}")
    return None


def parse_rows(html, cutoff_back, cutoff_ahead, no_agendas=False, no_minutes=False):
    """Parse meeting table rows. Returns (docs_list, stop_paginating).

    docs_list: [(meeting_date, doc_type, doc_id, doc_path)]
    stop_paginating: True when all rows on this page are before cutoff_back
    """
    docs = []
    oldest_date = date.today()
    found_any = False

    for row_m in _TR_RE.finditer(html):
        row = row_m.group(1)
        date_m = _START_DATE_RE.search(row)
        if not date_m:
            continue
        mtg_date = date.fromisoformat(date_m.group(1))
        found_any = True
        if mtg_date < oldest_date:
            oldest_date = mtg_date

        if mtg_date < cutoff_back or mtg_date > cutoff_ahead:
            continue

        for cell_m in _CELL_RE.finditer(row):
            doc_type = cell_m.group(1).lower()
            if no_agendas and doc_type == "agenda":
                continue
            if no_minutes and doc_type == "minutes":
                continue

            doc_m = _DOC_LINK_RE.search(cell_m.group(2))
            if not doc_m:
                continue
            doc_path, doc_id = doc_m.group(1), doc_m.group(2)
            docs.append((mtg_date, doc_type, doc_id, doc_path))

    stop = found_any and oldest_date < cutoff_back
    return docs, stop


def download_file(doc_path, out_path):
    url = f"{BASE_URL}{doc_path}"
    req = urllib.request.Request(url, headers=_HEADERS)
    for attempt in range(4):
        try:
            with urllib.request.urlopen(req, timeout=60) as r:
                data = r.read()
            os.makedirs(os.path.dirname(out_path), exist_ok=True)
            with open(out_path, "wb") as f:
                f.write(data)
            return True
        except (urllib.error.URLError, TimeoutError, OSError) as e:
            if attempt < 3:
                time.sleep(3 * (attempt + 1))
            else:
                print(f"    Error downloading {url}: {e}")
    return False


def process_board(slug, name, base_path, cutoff_back, cutoff_ahead,
                  no_agendas=False, no_minutes=False, dry_run=False,
                  verbose=False, output_dir=OUTPUT_DIR):
    seen = set()
    total = 0
    page = 1
    last_page = None

    while True:
        if page == 1:
            url = f"{BASE_URL}{base_path}"
        else:
            url = f"{BASE_URL}{base_path}/-npage-{page}"

        if verbose:
            print(f"  page {page}: {url}")

        html = fetch_html(url)
        if not html:
            break

        if page == 1:
            lp_m = _LAST_PAGE_RE.search(html)
            last_page = int(lp_m.group(1)) if lp_m else 1

        docs, stop = parse_rows(html, cutoff_back, cutoff_ahead, no_agendas, no_minutes)

        for mtg_date, doc_type, doc_id, doc_path in docs:
            if doc_id in seen:
                continue
            seen.add(doc_id)

            month_dir = os.path.join(output_dir, mtg_date.strftime("%Y-%m"))
            filename = f"{mtg_date.isoformat()}_{slug}_{doc_type}_{doc_id}.pdf"
            out_path = os.path.join(month_dir, filename)

            if os.path.exists(out_path):
                if verbose:
                    print(f"    skip: {filename}")
                continue

            if dry_run:
                print(f"    [dry] {filename}")
            else:
                if verbose:
                    print(f"    download: {filename}")
                if download_file(doc_path, out_path):
                    time.sleep(0.3)
                else:
                    continue
            total += 1

        if stop or page >= (last_page or 1):
            break
        page += 1
        time.sleep(0.5)

    return total


def fetch_recording_index(slug, board_name, page_path):
    html = fetch_html(f"{BASE_URL}{page_path}")
    if not html:
        return []
    rows = []
    for m in _YOUTUBE_RE.finditer(html):
        rows.append((board_name, m.group(2).strip(), m.group(1)))
    return rows


def main():
    ap = argparse.ArgumentParser(description="Download Farmington CT meeting docs (CivicPlus)")
    ap.add_argument("--days", type=int, default=DAYS_BACK,
                    help=f"Days back to fetch (default: {DAYS_BACK})")
    ap.add_argument("--ahead", type=int, default=90,
                    help="Days ahead for upcoming agendas (default: 90)")
    ap.add_argument("--all", action="store_true",
                    help=f"Fetch all docs back to {MIN_DATE}")
    ap.add_argument("--dry-run", action="store_true",
                    help="List what would be downloaded without downloading")
    ap.add_argument("--board", metavar="SLUG",
                    help="Only process this board (e.g. town-council)")
    ap.add_argument("--no-agendas", action="store_true", help="Skip agendas")
    ap.add_argument("--no-minutes", action="store_true", help="Skip minutes")
    ap.add_argument("--recordings", action="store_true",
                    help="Also save YouTube recording index to recording-index.csv")
    ap.add_argument("--no-subcommittees", action="store_true",
                    help="Skip FHS subcommittee pages")
    ap.add_argument("--verbose", "-v", action="store_true")
    ap.add_argument("--output-dir", default=OUTPUT_DIR,
                    help=f"Output directory (default: {OUTPUT_DIR})")
    args = ap.parse_args()

    now = datetime.datetime.now()
    if (now.weekday() == 5 and now.hour >= 18) or (now.weekday() == 6 and now.hour < 12):  # Saturday night, Sunday morning
        print("Skipping — no downloads on Saturday nights or Sunday mornings.")
        sys.exit(0)

    cutoff_back = MIN_DATE if args.all else date.today() - timedelta(days=args.days)
    cutoff_ahead = date.today() + timedelta(days=args.ahead)

    os.makedirs(args.output_dir, exist_ok=True)

    # Build full board list: (slug, name, base_path)
    all_boards = [
        (slug, name, f"/government/{slug}/minutes-and-agendas")
        for slug, name in AGENDA_BOARDS
    ]
    if not args.no_subcommittees:
        for sub_slug, sub_path, sub_name in FHS_SUBCOMMITTEES:
            all_boards.append((sub_slug, sub_name, sub_path))

    if args.board:
        filtered = [(s, n, p) for s, n, p in all_boards if s == args.board]
        if not filtered:
            print(f"Board '{args.board}' not found. Available slugs:")
            for s, n, _ in all_boards:
                print(f"  {s}  ({n})")
            return
        all_boards = filtered

    total = 0
    for slug, name, base_path in all_boards:
        print(f"{name}...")
        n = process_board(
            slug, name, base_path, cutoff_back, cutoff_ahead,
            no_agendas=args.no_agendas,
            no_minutes=args.no_minutes,
            dry_run=args.dry_run,
            verbose=args.verbose,
            output_dir=args.output_dir,
        )
        print(f"  {n}")
        total += n
        time.sleep(0.5)

    if args.recordings:
        rec_rows = []
        print("\nFetching recording indexes...")
        for slug, board_name, page_path in RECORDING_PAGES:
            if args.board and slug != args.board:
                continue
            print(f"  {board_name}...")
            rows = fetch_recording_index(slug, board_name, page_path)
            print(f"    {len(rows)} links")
            rec_rows.extend(rows)
            time.sleep(0.5)

        if rec_rows:
            csv_path = os.path.join(args.output_dir, "recording-index.csv")
            existing = set()
            if os.path.exists(csv_path):
                with open(csv_path, newline="", encoding="utf-8") as f:
                    for row in csv.DictReader(f):
                        existing.add(row["youtube_url"])

            new_rows = [(b, d, u) for b, d, u in rec_rows if u not in existing]
            if new_rows:
                write_header = not os.path.exists(csv_path)
                with open(csv_path, "a", newline="", encoding="utf-8") as f:
                    w = csv.writer(f)
                    if write_header:
                        w.writerow(["board", "date", "youtube_url"])
                    w.writerows(new_rows)
                print(f"\nAdded {len(new_rows)} new recording links to {csv_path}")
            else:
                print("\nNo new recording links.")

    label = "Would download" if args.dry_run else "Downloaded"
    print(f"\n{label} {total} documents total.")


if __name__ == "__main__":
    main()
