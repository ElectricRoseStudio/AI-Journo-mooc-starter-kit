#!/usr/bin/env python3
# download-stratford-agendas.py
# Download Stratford CT municipal meeting agendas and minutes from the
# town's ThrillShare CMS posted in the past N days.
#
# USAGE:
#   python3 scripts/download-stratford-agendas.py [options]
#
# REQUIREMENTS:
#   - Python 3.8+
#   - pip install playwright beautifulsoup4
#   - playwright install chromium
#
# WHAT IT DOES:
#   1. Queries ThrillShare Events API (no bot challenge) for meetings in window.
#   2. Uses Playwright Chromium to bypass the town's JS bot challenge and scrape
#      document links from agenda year-folder and minutes folder pages.
#   3. Downloads PDFs directly from the ThrillShare CDN (no Playwright needed).
#   Files are saved to beat-archive/stratford-agendas/YYYY-MM/.
#
# NOTE: No video recordings available — meetings air on Channel 79 cable TV
#   only; no web streaming or on-demand video found.
#
# SOURCES:
#   Events API: https://thrillshare-cmsv2.services.thrillshare.com/api/v4/o/14549/cms/events
#   Documents:  https://www.stratfordct.gov/documents/town-hall/agendas-%26-minutes/

import argparse
import datetime
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request

try:
    from bs4 import BeautifulSoup
except ImportError:
    print("ERROR: beautifulsoup4 not installed.\n  pip install beautifulsoup4", file=sys.stderr)
    sys.exit(1)

try:
    from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeout
except ImportError:
    print(
        "ERROR: playwright not installed.\n"
        "  pip install playwright && playwright install chromium",
        file=sys.stderr,
    )
    sys.exit(1)

# --- Configuration ---
SITE_URL        = "https://www.stratfordct.gov"
OUTPUT_DIR      = "beat-archive/stratford-agendas"
DAYS_BACK       = 4

# ThrillShare org config
TS_API_BASE     = "https://thrillshare-cmsv2.services.thrillshare.com"
TS_ORG_ID       = "14549"
TS_EVENTS_URL   = f"{TS_API_BASE}/api/v4/o/{TS_ORG_ID}/cms/events"
ROOT_FOLDER_ID  = "411747"
ROOT_FOLDER_URL = f"{SITE_URL}/documents/town-hall/agendas-%26-minutes/{ROOT_FOLDER_ID}"

PW_WAIT    = 4000   # ms to settle after domcontentloaded
PW_TIMEOUT = 30000  # ms page load timeout
DELAY      = 0.5    # s between HTTP downloads

UA = "Stratford-Agendas-Downloader/1.0 (journalism research)"

MONTHS = {
    'january':1,'february':2,'march':3,'april':4,'may':5,'june':6,
    'july':7,'august':8,'september':9,'october':10,'november':11,'december':12,
}

# Date patterns, most specific first
_DATE_PATS = [
    # YYYY-MM-DD or YYYY.MM.DD
    (re.compile(r'\b(20\d{2})[.\-](\d{1,2})[.\-](\d{1,2})\b'),
     lambda m: _safe_date(int(m.group(1)), int(m.group(2)), int(m.group(3)))),
    # MM/DD/YYYY, MM-DD-YYYY, MM.DD.YYYY
    (re.compile(r'\b(\d{1,2})[./\-](\d{1,2})[./\-](20\d{2})\b'),
     lambda m: _safe_date(int(m.group(3)), int(m.group(1)), int(m.group(2)))),
    # Month DD, YYYY
    (re.compile(
        r'\b(January|February|March|April|May|June|July|August'
        r'|September|October|November|December)\s+(\d{1,2}),?\s+(20\d{2})\b', re.I),
     lambda m: _safe_date(int(m.group(3)), MONTHS[m.group(1).lower()], int(m.group(2)))),
    # Month DD (no year — assume current year)
    (re.compile(
        r'\b(January|February|March|April|May|June|July|August'
        r'|September|October|November|December)\s+(\d{1,2})\b', re.I),
     lambda m: _safe_date(datetime.date.today().year, MONTHS[m.group(1).lower()], int(m.group(2)))),
]


def _safe_date(y, m, d):
    try:
        return datetime.date(y, m, d)
    except ValueError:
        return None


def parse_date_from_title(title):
    for pat, fn in _DATE_PATS:
        match = pat.search(title)
        if match:
            d = fn(match)
            if d:
                return d
    return None


# --- HTTP helpers ---

def fetch_json(url):
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return json.loads(r.read().decode("utf-8", errors="replace"))
    except urllib.error.HTTPError as e:
        print(f"  HTTP {e.code} — {url}", file=sys.stderr)
        return None
    except Exception as e:
        print(f"  ERROR: {e} — {url}", file=sys.stderr)
        return None


def download_file(url, dest):
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            with open(dest, "wb") as f:
                f.write(r.read())
        return True
    except Exception as e:
        print(f"  WARNING: {e} — {url}", file=sys.stderr)
        return False


# --- Playwright helpers ---

def pw_load(page, url):
    """Navigate to url; wait for domcontentloaded + JS settle time."""
    try:
        page.goto(url, wait_until="domcontentloaded", timeout=PW_TIMEOUT)
        page.wait_for_timeout(PW_WAIT)
        return True
    except PlaywrightTimeout:
        print(f"  WARNING: timeout — {url}", file=sys.stderr)
        return False


def pw_doc_links(page):
    """Return list of (href, text) for [data-cy='document-link'] on the page."""
    results = []
    for el in page.query_selector_all('[data-cy="document-link"]'):
        href = el.get_attribute("href") or ""
        text = el.inner_text().strip()
        if href:
            if not href.startswith("http"):
                href = SITE_URL + href
            results.append((href, text))
    return results


def _is_folder_href(href):
    """True if href looks like a ThrillShare folder link (numeric ID, not CDN)."""
    return (
        bool(re.search(r'/\d+(/|$)', href))
        and "assets.thrillshare" not in href
        and "files-backend" not in href
    )


def pw_get_board_map(page):
    """Load root folder; return {slug: (board_id, full_href)} for all boards."""
    if not pw_load(page, ROOT_FOLDER_URL):
        return {}
    board_map = {}
    for el in page.query_selector_all('a[href]'):
        href = el.get_attribute("href") or ""
        if "assets.thrillshare" in href or "files-backend" in href:
            continue
        # Board-level URL: .../agendas-%26-minutes/{slug}/{id}  (one numeric segment at end)
        m = re.search(r'/agendas[^/]*/([^/]+)/(\d+)(?:[/?#]|$)', href)
        if m:
            slug, bid = m.group(1), m.group(2)
            if slug not in board_map:
                full = href if href.startswith("http") else SITE_URL + href
                board_map[slug] = (bid, full)
    return board_map


def pw_find_subfolder(page, board_href, keyword):
    """
    Load board_href; find first folder link whose path contains /{keyword}/.
    Returns full href or None.
    """
    url = board_href if board_href.startswith("http") else SITE_URL + board_href
    if not pw_load(page, url):
        return None
    for el in page.query_selector_all('a[href]'):
        href = el.get_attribute("href") or ""
        if f"/{keyword.lower()}/" in href.lower() and _is_folder_href(href):
            return href if href.startswith("http") else SITE_URL + href
    return None


def pw_find_year_folder(page, type_href, year):
    """
    Load type_href (minutes or agendas folder); find subfolder for the given year.
    Returns full href or None.
    """
    url = type_href if type_href.startswith("http") else SITE_URL + type_href
    if not pw_load(page, url):
        return None
    year_str = str(year)
    for el in page.query_selector_all('a[href]'):
        href = el.get_attribute("href") or ""
        if f"/{year_str}/" in href and _is_folder_href(href):
            return href if href.startswith("http") else SITE_URL + href
    return None


# --- ThrillShare Events API ---

def fetch_events(cutoff, future_limit):
    """
    Query ThrillShare Events API for meetings in the date window.
    Returns list of dicts: {title, date, agenda_url}
    """
    url = (
        f"{TS_EVENTS_URL}"
        f"?start_date={cutoff.isoformat()}&end_date={future_limit.isoformat()}"
    )
    data = fetch_json(url)
    if not data:
        return []

    events_raw = data if isinstance(data, list) else (
        data.get("events") or data.get("data") or []
    )

    results = []
    for ev in events_raw:
        raw_date = ev.get("start_at") or ev.get("start_date") or ev.get("date") or ""
        if not raw_date:
            continue
        try:
            edate = datetime.date.fromisoformat(raw_date[:10])
        except ValueError:
            continue
        if not (cutoff <= edate <= future_limit):
            continue

        title = ev.get("title") or ev.get("name") or ""
        desc  = ev.get("description") or ""

        # Extract agenda year-folder URL from event description HTML
        agenda_url = None
        if desc:
            soup = BeautifulSoup(desc, "html.parser")
            for a in soup.find_all("a", href=True):
                href = a["href"]
                if "/documents/" in href and "agendas" in href:
                    agenda_url = href if href.startswith("http") else SITE_URL + href
                    break

        results.append({"title": title, "date": edate, "agenda_url": agenda_url})

    return results


# --- Slug / filename helpers ---

def extract_board_slug(url):
    """Extract board slug from a ThrillShare document-folder URL."""
    m = re.search(r'/agendas[^/]*/([^/]+)/', url)
    return m.group(1) if m else None


def slugify(text, max_len=50):
    text = text.lower().strip()
    text = re.sub(r"[/\\]", "-", text)
    text = re.sub(r"\s+-\s+", "-", text)
    text = re.sub(r"[^\w\s-]", "", text)
    text = re.sub(r"[\s_]+", "-", text)
    text = re.sub(r"-{2,}", "-", text)
    return text.strip("-")[:max_len]


def month_dir(date, output_dir):
    path = os.path.join(output_dir, date.strftime("%Y-%m"))
    os.makedirs(path, exist_ok=True)
    return path


def make_dest(date, board_slug, doc_type, url, output_dir):
    d        = month_dir(date, output_dir)
    date_str = date.strftime("%Y-%m-%d")
    board    = slugify(board_slug, max_len=30)
    dtype    = slugify(doc_type, max_len=10)
    fname    = url.split("?")[0].split("/")[-1]
    orig     = slugify(os.path.splitext(fname)[0], max_len=40)
    ext      = os.path.splitext(fname)[1] or ".pdf"
    return os.path.join(d, f"{date_str}-{board}-{dtype}-{orig}{ext}")


# --- Main ---

def main():
    parser = argparse.ArgumentParser(
        description=(
            "Download Stratford CT municipal meeting agendas and minutes "
            "from ThrillShare CMS posted in the past N days."
        )
    )
    parser.add_argument("--days", type=int, default=DAYS_BACK, metavar="N",
                        help=f"Look back N days (default: {DAYS_BACK})")
    parser.add_argument("--output-dir", default=OUTPUT_DIR, metavar="DIR",
                        help=f"Output directory (default: {OUTPUT_DIR})")
    parser.add_argument("--dry-run", action="store_true",
                        help="List matches without downloading")
    parser.add_argument("--show-browser", action="store_true",
                        help="Run with a visible browser window (useful for debugging)")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--agendas-only", action="store_true",
                      help="Download agendas only (skip minutes)")
    mode.add_argument("--minutes-only", action="store_true",
                      help="Download minutes only (skip agendas)")
    args = parser.parse_args()

    now = datetime.datetime.now()
    if (now.weekday() == 5 and now.hour >= 18) or (now.weekday() == 6 and now.hour < 12):  # Saturday night, Sunday morning
        print("Skipping — no downloads on Saturday nights or Sunday mornings.")
        sys.exit(0)

    do_agendas = not args.minutes_only
    do_minutes = not args.agendas_only

    today        = datetime.date.today()
    cutoff       = today - datetime.timedelta(days=args.days)
    future_limit = today + datetime.timedelta(days=1)
    current_year = today.year

    print(f"Date window : {cutoff} to {today}  ({args.days} days)")
    print(f"Portal      : {SITE_URL}")
    print(f"Output dir  : {args.output_dir}")
    print()

    # --- Phase 1: Events API (no Playwright, no bot challenge) ---
    print("Fetching ThrillShare events...")
    events = fetch_events(cutoff, future_limit)
    events.sort(key=lambda e: e["date"], reverse=True)
    print(f"Found {len(events)} event(s) in window.")
    print()

    if not events:
        print("No events found. Exiting.")
        return

    # Collect unique board slugs; deduplicate agenda folder URLs
    board_slugs    = set()
    agenda_url_map = {}  # url → event (first occurrence wins)
    for ev in events:
        aurl = ev.get("agenda_url")
        if aurl:
            slug = extract_board_slug(aurl)
            if slug:
                board_slugs.add(slug)
                if aurl not in agenda_url_map:
                    agenda_url_map[aurl] = ev
        else:
            print(
                f"  WARNING: no agenda URL found for: {ev['title']} ({ev['date']})",
                file=sys.stderr,
            )

    all_docs = []  # list of (date, board_slug, doc_type, url)

    # --- Phase 2: Playwright — scrape agenda and minutes folder pages ---
    with sync_playwright() as pw:
        with pw.chromium.launch(headless=not args.show_browser) as browser:
            page = browser.new_page()
            page.set_extra_http_headers({"User-Agent": UA})

            # Build board-ID map from root folder (required for minutes navigation)
            board_map = {}
            if do_minutes and board_slugs:
                print("Loading root folder to map board IDs...")
                board_map = pw_get_board_map(page)
                print(f"Found {len(board_map)} board(s) in root folder.")
                print()

            # Agendas: load each unique agenda year-folder URL from the events API
            if do_agendas and agenda_url_map:
                print("Fetching agenda folders...")
                for aurl, ev in sorted(
                    agenda_url_map.items(), key=lambda x: x[1]["date"], reverse=True
                ):
                    slug = extract_board_slug(aurl) or "unknown"
                    print(f"  [{ev['date']}] {ev['title']}")
                    if not pw_load(page, aurl):
                        continue
                    for href, text in pw_doc_links(page):
                        doc_date = parse_date_from_title(text) or ev["date"]
                        if cutoff <= doc_date <= future_limit:
                            all_docs.append((doc_date, slug, "agenda", href))
                print()

            # Minutes: board folder → minutes type folder → year folder → docs
            if do_minutes and board_slugs:
                print("Fetching minutes folders...")
                for slug in sorted(board_slugs):
                    if slug not in board_map:
                        print(
                            f"  WARNING: '{slug}' not in board map — skipping minutes",
                            file=sys.stderr,
                        )
                        continue
                    _, board_href = board_map[slug]
                    print(f"  {slug}")

                    minutes_href = pw_find_subfolder(page, board_href, "minutes")
                    if not minutes_href:
                        print(f"    no minutes subfolder found", file=sys.stderr)
                        continue

                    year_href = pw_find_year_folder(page, minutes_href, current_year)
                    if not year_href:
                        print(
                            f"    no {current_year} folder in minutes", file=sys.stderr
                        )
                        continue

                    year_url = year_href if year_href.startswith("http") else SITE_URL + year_href
                    if not pw_load(page, year_url):
                        continue

                    for href, text in pw_doc_links(page):
                        doc_date = parse_date_from_title(text)
                        if doc_date and cutoff <= doc_date <= future_limit:
                            all_docs.append((doc_date, slug, "minutes", href))
                print()

    # --- Phase 3: Download ---
    all_docs.sort(key=lambda x: x[0], reverse=True)
    print(f"Found {len(all_docs)} document(s) in window.")
    print()

    dl_ok = dl_skip = dl_fail = 0

    for (doc_date, slug, doc_type, url) in all_docs:
        dest   = make_dest(doc_date, slug, doc_type, url, args.output_dir)
        header = f"  [{doc_date}] {slug.replace('-', ' ').title()} — {doc_type.title()}"

        if args.dry_run:
            print(header)
            print(f"    {os.path.basename(dest)}")
            continue

        print(header)
        if os.path.exists(dest):
            print(f"    skip (exists)  {os.path.basename(dest)}")
            dl_skip += 1
            continue

        print(f"    downloading    {os.path.basename(dest)}")
        if download_file(url, dest):
            dl_ok += 1
        else:
            dl_fail += 1
            if os.path.exists(dest):
                os.remove(dest)
        time.sleep(DELAY)

    if not args.dry_run:
        print()
        print(f"Documents  — downloaded: {dl_ok}  skipped: {dl_skip}  failed: {dl_fail}")
        if dl_ok + dl_skip:
            print(f"Files in: {args.output_dir}")


if __name__ == "__main__":
    main()


# --- Tips ---
#
# Dry run to see what would be downloaded (15-day window):
#   python3 scripts/download-stratford-agendas.py --dry-run
#
# Download agendas and minutes (default 15-day window):
#   python3 scripts/download-stratford-agendas.py
#
# Agendas only:
#   python3 scripts/download-stratford-agendas.py --agendas-only
#
# Minutes only:
#   python3 scripts/download-stratford-agendas.py --minutes-only
#
# Extend the lookback window:
#   python3 scripts/download-stratford-agendas.py --days 30
#
# Debug with a visible browser window:
#   python3 scripts/download-stratford-agendas.py --show-browser --dry-run
#
# Custom output directory:
#   python3 scripts/download-stratford-agendas.py --output-dir ~/Downloads/stratford
#
# Run daily via cron (7 AM):
#   0 7 * * * cd /path/to/repo && python3 scripts/download-stratford-agendas.py
#
# NOTE: Agendas are discovered via the ThrillShare Events API (fast, no browser
# needed for this step). Minutes are found by navigating each board's folder
# hierarchy in Playwright. Dates are parsed from document titles.
