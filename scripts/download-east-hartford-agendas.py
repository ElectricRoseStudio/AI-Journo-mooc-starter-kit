#!/usr/bin/env python3
"""Download East Hartford, CT meeting agendas, minutes, and recording index.

Requires playwright:
    pip install playwright
    playwright install chromium

The easthartfordct.gov domain is behind Cloudflare. This script uses a
headless Chromium browser (via Playwright) to bypass the CF challenge, then
downloads document PDFs directly from the /sites/g/files/ CDN path, which
is accessible without CF protection.

Agendas and minutes are Drupal nodes on a per-board, per-year listing:
  https://www.easthartfordct.gov/node/{nodeId}/{agenda|minutes}/{year}

YouTube recording links are saved to recording-index.csv (use --recordings).
"""

import argparse
import csv
import os
import re
import time
import urllib.request
from datetime import date, timedelta

BASE_URL = "https://www.easthartfordct.gov"
OUTPUT_DIR = "beat-archive/east-hartford-agendas"
DAYS_BACK = 4
MIN_DATE = date(2020, 1, 1)

# Board slug → (drupal_node_id, display_name)
BOARDS = {
    "board-of-assessment-appeals": (2166, "Board of Assessment Appeals"),
    "charter-revision-commission": (136046, "Charter Revision Commission"),
    "commission-on-aging": (2459, "Commission on Aging"),
    "commission-on-culture-and-fine-arts": (61683, "Commission on Culture and Fine Arts"),
    "commission-on-services-for-persons-with-disabilities": (
        2447, "Commission on Services for Persons with Disabilities"
    ),
    "east-hartford-school-readiness": (58103, "East Hartford School Readiness Council"),
    "economic-development-commission": (2446, "Economic Development Commission"),
    "ethics-board": (2465, "Ethics Board"),
    "historic-district-commission": (2451, "Historic District Commission"),
    "inland-wetlands-commission": (2458, "Inland Wetland Commission"),
    "library-board": (2448, "Library Commission"),
    "local-prevention-council": (185126, "Local Prevention Council"),
    "planning-and-zoning-commission": (2444, "Planning and Zoning Commission"),
    "public-building-commission": (2468, "Public Building Commission"),
    "redevelopment-agency": (2462, "Redevelopment Agency"),
    "senior-center-committee": (2905, "Senior Center Committee"),
    "town-council": (2070, "Town Council"),
    "veterans-commission": (61693, "Veteran's Commission"),
    "zoning-board-of-appeals": (2443, "Zoning Board of Appeals"),
}

# (slug, board_name, path) for recording index pages
RECORDING_PAGES = [
    (
        "board-of-assessment-appeals",
        "Board of Assessment Appeals",
        "/board-of-assessment-appeals/pages/baa-recorded-meetings",
    ),
    (
        "charter-revision-commission",
        "Charter Revision Commission",
        "/charter-revision-commission-2021/pages/charter-revision-commission-meeting-recordings",
    ),
    (
        "historic-district-commission",
        "Historic District Commission",
        "/historic-district-commission/pages/historic-district-meeting-recordings",
    ),
    (
        "planning-and-zoning-commission",
        "Planning and Zoning Commission",
        "/planning/planning-and-zoning-commission/pages/planning-zoning-meeting-recordings",
    ),
    ("town-council", "Town Council", "/town-council/pages/2026-town-council-meeting-recordings"),
    ("town-council", "Town Council", "/town-council/pages/town-council-meeting-recordings-2025"),
    ("town-council", "Town Council", "/town-council/pages/town-council-meeting-recordings-2024"),
    ("town-council", "Town Council", "/town-council/pages/town-council-meeting-recordings-2023"),
    ("town-council", "Town Council", "/town-council/pages/town-council-meeting-recordings-2022"),
    ("town-council", "Town Council", "/town-council/pages/town-council-meeting-recordings-2021"),
    ("town-council", "Town Council", "/town-council/pages/town-council-meeting-recordings-2020"),
    (
        "zoning-board-of-appeals",
        "Zoning Board of Appeals",
        "/zoning-board-of-appeals/pages/zba-recorded-meetings",
    ),
]

# Matches doc links followed by their date attribute in listing HTML.
# Excludes /node/NNN/ navigation links via negative lookahead.
_ITEM_RE = re.compile(
    r'href="(/(?!node/)[^"]+/(?:agenda|minutes)/[^"/][^"]*)"[^>]*>.*?'
    r'content="(\d{4}-\d{2}-\d{2})',
    re.DOTALL,
)

_YOUTUBE_RE = re.compile(
    r'href="(https://youtu(?:\.be|be\.com)/[^"]+)"[^>]*>([^<]*)',
    re.IGNORECASE,
)

_DL_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    )
}


def download_file(url, out_path, retries=3):
    req = urllib.request.Request(url, headers=_DL_HEADERS)
    for attempt in range(retries + 1):
        try:
            with urllib.request.urlopen(req, timeout=60) as r:
                data = r.read()
            os.makedirs(os.path.dirname(out_path), exist_ok=True)
            with open(out_path, "wb") as f:
                f.write(data)
            return True
        except Exception as e:
            if attempt < retries:
                time.sleep(3 * (attempt + 1))
            else:
                print(f"    Error downloading {url}: {e}")
    return False


def parse_listing(html, board_slug):
    """Return [(meeting_date, href_path, doc_type)] from a year listing page."""
    items = []
    seen = set()
    for m in _ITEM_RE.finditer(html):
        href = m.group(1)
        if href in seen:
            continue
        seen.add(href)
        if f"/{board_slug}/" not in href:
            continue
        try:
            mtg_date = date.fromisoformat(m.group(2))
        except ValueError:
            continue
        doc_type = "agenda" if f"/{board_slug}/agenda/" in href else "minutes"
        items.append((mtg_date, href, doc_type))
    return items


def resolve_doc_url(pw_page, href):
    """Navigate to a doc link, capture the download URL, cancel the download."""
    with pw_page.expect_download(timeout=12000) as dl_info:
        pw_page.evaluate(f"window.location.href = {repr(href)}")
    dl = dl_info.value
    file_url = dl.url
    dl.cancel()
    return file_url


def process_board(
    slug,
    node_id,
    name,
    cutoff_back,
    cutoff_ahead,
    pw_page,
    no_agendas=False,
    no_minutes=False,
    dry_run=False,
    verbose=False,
    output_dir=OUTPUT_DIR,
):
    total = 0
    seen_hrefs = set()

    type_slugs = []
    if not no_agendas:
        type_slugs.append("agenda")
    if not no_minutes:
        type_slugs.append("minutes")

    for type_slug in type_slugs:
        for year in range(cutoff_back.year, cutoff_ahead.year + 1):
            list_url = f"{BASE_URL}/node/{node_id}/{type_slug}/{year}"
            if verbose:
                print(f"  → {list_url}")

            try:
                pw_page.goto(list_url, timeout=15000)
                time.sleep(1.5)
            except Exception as e:
                print(f"  Warning: failed to load {list_url}: {e}")
                continue

            html = pw_page.content()
            items = parse_listing(html, slug)

            for mtg_date, href, doc_type in items:
                if not (cutoff_back <= mtg_date <= cutoff_ahead):
                    continue
                if href in seen_hrefs:
                    continue
                seen_hrefs.add(href)

                url_slug = href.rstrip("/").split("/")[-1][:80].rstrip("-")
                month_dir = os.path.join(output_dir, mtg_date.strftime("%Y-%m"))
                filename = f"{mtg_date.isoformat()}_{slug}_{doc_type}_{url_slug}.pdf"
                out_path = os.path.join(month_dir, filename)

                if os.path.exists(out_path):
                    if verbose:
                        print(f"    skip: {filename}")
                    continue

                if dry_run:
                    print(f"    [dry] {filename}")
                    total += 1
                    continue

                try:
                    file_url = resolve_doc_url(pw_page, href)
                except Exception as e:
                    print(f"    Warning: couldn't resolve {href}: {e}")
                    continue

                if not file_url or "/sites/g/files/" not in file_url:
                    print(f"    Warning: unexpected file URL for {href}: {file_url}")
                    continue

                if verbose:
                    print(f"    download: {filename}")

                if download_file(file_url, out_path):
                    total += 1
                    time.sleep(0.3)
                else:
                    print(f"    Warning: failed {filename}")

            time.sleep(0.5)

    return total


def fetch_recording_index(slug, board_name, path, pw_page):
    try:
        pw_page.goto(f"{BASE_URL}{path}", timeout=15000)
        time.sleep(2)
    except Exception as e:
        print(f"    Warning: failed to load {path}: {e}")
        return []
    html = pw_page.content()
    rows = []
    for m in _YOUTUBE_RE.finditer(html):
        rows.append((board_name, m.group(2).strip(), m.group(1)))
    return rows


def main():
    ap = argparse.ArgumentParser(
        description="Download East Hartford CT meeting docs (Cloudflare bypass via Playwright)"
    )
    ap.add_argument(
        "--days", type=int, default=DAYS_BACK,
        help=f"Days back to fetch (default: {DAYS_BACK})"
    )
    ap.add_argument(
        "--ahead", type=int, default=90,
        help="Days ahead for upcoming agendas (default: 90)"
    )
    ap.add_argument(
        "--all", action="store_true",
        help=f"Fetch all docs back to {MIN_DATE}"
    )
    ap.add_argument(
        "--dry-run", action="store_true",
        help="List what would be downloaded without downloading"
    )
    ap.add_argument(
        "--board", metavar="SLUG",
        help="Only process this board (e.g. town-council)"
    )
    ap.add_argument("--no-agendas", action="store_true", help="Skip agendas")
    ap.add_argument("--no-minutes", action="store_true", help="Skip minutes")
    ap.add_argument(
        "--recordings", action="store_true",
        help="Also save YouTube recording index to recording-index.csv"
    )
    ap.add_argument("--verbose", "-v", action="store_true")
    ap.add_argument(
        "--output-dir", default=OUTPUT_DIR,
        help=f"Output directory (default: {OUTPUT_DIR})"
    )
    args = ap.parse_args()

    cutoff_back = MIN_DATE if args.all else date.today() - timedelta(days=args.days)
    cutoff_ahead = date.today() + timedelta(days=args.ahead)

    if args.board:
        boards_to_run = {s: v for s, v in BOARDS.items() if s == args.board}
        if not boards_to_run:
            print(f"Board '{args.board}' not found. Available slugs:")
            for s, (_, n) in sorted(BOARDS.items()):
                print(f"  {s}  ({n})")
            return
    else:
        boards_to_run = BOARDS

    os.makedirs(args.output_dir, exist_ok=True)

    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        print("ERROR: playwright is not installed.")
        print("  pip install playwright")
        print("  playwright install chromium")
        return

    with sync_playwright() as p:
        browser = p.chromium.launch(
            headless=True,
            args=[
                "--disable-blink-features=AutomationControlled",
                "--no-sandbox",
                "--disable-setuid-sandbox",
            ],
        )
        context = browser.new_context(
            user_agent=(
                "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
            ),
            viewport={"width": 1280, "height": 720},
            java_script_enabled=True,
            locale="en-US",
            accept_downloads=True,
        )
        page = context.new_page()

        print("Establishing Cloudflare session (this may take ~10 seconds)...")
        page.goto(f"{BASE_URL}/town-council", timeout=30000)
        time.sleep(9)
        print(f"Session ready: {page.title()}\n")

        total = 0
        for slug, (node_id, name) in sorted(boards_to_run.items(), key=lambda x: x[1][1]):
            print(f"{name}...")
            n = process_board(
                slug,
                node_id,
                name,
                cutoff_back,
                cutoff_ahead,
                page,
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
            for slug, board_name, path in RECORDING_PAGES:
                if args.board and slug != args.board:
                    continue
                print(f"  {board_name}: {path.split('/')[-1]}...")
                rows = fetch_recording_index(slug, board_name, path, page)
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

                new_rows = [(b, t, u) for b, t, u in rec_rows if u not in existing]
                if new_rows:
                    write_header = not os.path.exists(csv_path)
                    with open(csv_path, "a", newline="", encoding="utf-8") as f:
                        w = csv.writer(f)
                        if write_header:
                            w.writerow(["board", "title", "youtube_url"])
                        w.writerows(new_rows)
                    print(f"\nAdded {len(new_rows)} new recording links to {csv_path}")
                else:
                    print("\nNo new recording links.")

        browser.close()

    label = "Would download" if args.dry_run else "Downloaded"
    print(f"\n{label} {total} documents total.")
    print("\nNote: Cloudflare bypass requires Playwright (pip install playwright && playwright install chromium)")


if __name__ == "__main__":
    main()
