#!/usr/bin/env python3
# download-bethel-agendas.py
# Download Bethel, CT municipal meeting agendas and minutes posted in the past N days.
#
# USAGE:
#   python3 scripts/download-bethel-agendas.py [options]
#
# REQUIREMENTS:
#   - Python 3.8+
#   - pip install beautifulsoup4
#
# WHAT IT DOES:
#   1. Fetches bethel-ct.gov/meetings — discovers all board/commission pages
#   2. Visits each board page to find year-specific meeting sub-pages
#   3. On each year page, finds PDF links whose date falls within the lookback window
#   4. Downloads matching PDFs to beat-archive/bethel-agendas/YYYY-MM/
#   5. Appends a download log to beat-archive/bethel-agendas/download-log.txt
#
# SITE STRUCTURE (GovOffice/Catalis CMS):
#   Hub:        https://bethel-ct.gov/meetings
#   Boards:     https://bethel-ct.gov/{slug}   (e.g. /bos, /bof, /zba)
#   Year pages: https://bethel-ct.gov/{year}-{slug}  or  index.asp?SEC=...
#   Documents:  https://bethel-ct.gov/vertical/sites/{GUID}/uploads/{YYYY-MM-DD}_*.pdf

import argparse
import datetime
import os
import re
import sys
import time
import urllib.error
import urllib.request

try:
    from bs4 import BeautifulSoup
except ImportError:
    print("ERROR: beautifulsoup4 is not installed.\n  pip install beautifulsoup4",
          file=sys.stderr)
    sys.exit(1)

BASE_URL = "https://bethel-ct.gov"
HUB_URL = f"{BASE_URL}/meetings"
OUTPUT_DIR = "beat-archive/bethel-agendas"
DAYS_BACK = 4
DELAY_SECONDS = 1.0

UA = "Bethel-Agendas-Downloader/1.0 (journalism research)"

# URL fragment that identifies all Bethel document uploads
UPLOAD_PATH = "/vertical/sites/"

# Date patterns for filenames and link text (most-specific first)
DATE_PATTERNS = [
    # YYYY-MM-DD  (most common in filenames)
    (re.compile(r"\b(20\d{2})-(\d{2})-(\d{2})\b"), "YMD"),
    # "April 21, 2026" / "April 21 2026"
    (re.compile(
        r"\b(January|February|March|April|May|June|July|August|September|"
        r"October|November|December)\s+(\d{1,2}),?\s+(20\d{2})\b", re.I),
     "month-name"),
    # MM/DD/YYYY or M/D/YYYY
    (re.compile(r"\b(\d{1,2})/(\d{1,2})/(20\d{2})\b"), "MDY-slash"),
    # MM-DD-YYYY or M-D-YYYY
    (re.compile(r"\b(\d{1,2})-(\d{1,2})-(20\d{2})\b"), "MDY-dash"),
    # filed_BOF_RM_agenda_10.14.2025.pdf style
    (re.compile(r"\b(\d{1,2})\.(\d{1,2})\.(20\d{2})\b"), "MDY-dot"),
]


# --- HTTP helpers ---

def fetch_html(url):
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            raw = r.read()
            charset = r.headers.get_content_charset() or "utf-8"
            return raw.decode(charset, errors="replace")
    except urllib.error.HTTPError as e:
        print(f"  HTTP {e.code} — {url}", file=sys.stderr)
        return None
    except urllib.error.URLError as e:
        print(f"  ERROR fetching {url}: {e}", file=sys.stderr)
        return None


def download_binary(url, dest_path):
    full_url = BASE_URL + url if url.startswith("/") else url
    req = urllib.request.Request(full_url, headers={"User-Agent": UA})
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            with open(dest_path, "wb") as f:
                f.write(r.read())
        return True
    except Exception as e:
        print(f"  WARNING: {e} — {full_url}", file=sys.stderr)
        return False


# --- Date parsing ---

def parse_date(text):
    """Return the first date found in text as a date object, or None."""
    for pattern, fmt in DATE_PATTERNS:
        m = pattern.search(text)
        if not m:
            continue
        try:
            if fmt == "YMD":
                return datetime.date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
            elif fmt == "month-name":
                return datetime.datetime.strptime(
                    f"{m.group(1)} {int(m.group(2)):02d} {m.group(3)}", "%B %d %Y"
                ).date()
            elif fmt in ("MDY-slash", "MDY-dash", "MDY-dot"):
                month, day, year = int(m.group(1)), int(m.group(2)), int(m.group(3))
                return datetime.date(year, month, day)
        except ValueError:
            continue
    return None


def best_date_for_link(tag):
    """
    Find the best date for a document link by checking:
    1. The filename portion of the href
    2. The link text itself
    3. The nearest table row / list item text
    """
    href = tag.get("href", "")
    filename = href.split("/")[-1].split("?")[0]
    link_text = tag.get_text(" ", strip=True)

    for candidate in (filename, link_text):
        d = parse_date(candidate)
        if d:
            return d

    for ancestor_tag in ("tr", "li", "p", "div"):
        ancestor = tag.find_parent(ancestor_tag)
        if ancestor:
            d = parse_date(ancestor.get_text(" ", strip=True))
            if d:
                return d

    return None


# --- Hub page: discover board slugs ---

# Board slugs that are navigation items, not meeting boards
_NAV_SLUGS = {
    "meetings", "boards", "government", "departments", "selectmen",
    "meeting-calendar", "meeting-information", "financials", "welcome",
    "directory", "employment", "payments", "town-seal", "privacy",
    "municipal-center", "communitycalendar", "news",
}

# Only keep links in the board/commission listing section
# (relative paths like /bos, /zba — short slugs, no hyphens in most cases,
# listed in the "Board/Commission Links" block of the meetings page)
_BOARD_SLUG_RE = re.compile(r"^/[a-z][a-z0-9\-]{1,30}$")


def discover_boards(hub_html):
    """
    Return list of (board_name, board_url) for all board/commission pages
    found in the main content area of /meetings.
    """
    soup = BeautifulSoup(hub_html, "html.parser")
    boards = []
    seen = set()

    # The board links are in the main content body — look for a div/section
    # that excludes the nav sidebar. GovOffice puts main content in a div
    # with id="content" or class containing "content".
    content = (
        soup.find(id="content")
        or soup.find(class_=re.compile(r"\bcontent\b", re.I))
        or soup.body
    )
    if not content:
        content = soup

    for a in content.find_all("a", href=True):
        href = a["href"].strip()
        text = a.get_text(" ", strip=True)

        # Must be a relative short slug
        if not _BOARD_SLUG_RE.match(href):
            continue
        slug = href.lstrip("/")
        if slug in _NAV_SLUGS or not text or len(text) < 2:
            continue
        # Skip links that look like department pages, not board meeting pages
        if re.search(r"^depts-|^index\.asp|^field-gis$|^\d", slug):
            continue

        abs_url = BASE_URL + href
        if abs_url in seen:
            continue
        seen.add(abs_url)
        boards.append((text, abs_url))

    return boards


# --- Board page: discover year sub-pages ---

def discover_year_pages(board_html, board_url, cutoff_year):
    """
    Return list of (year, abs_url) for year-specific meeting pages
    that are >= cutoff_year, found on a board's landing page.
    """
    soup = BeautifulSoup(board_html, "html.parser")
    year_pages = []
    seen = set()

    for a in soup.find_all("a", href=True):
        href = a["href"].strip()
        text = a.get_text(" ", strip=True)

        # Look for 4-digit year in link text or URL
        year_in_text = re.search(r"\b(20\d{2})\b", text)
        year_in_href = re.search(r"\b(20\d{2})\b", href)
        year_match = year_in_text or year_in_href
        if not year_match:
            continue
        year = int(year_match.group(1))
        if year < cutoff_year:
            continue

        # Normalise to absolute URL
        if href.startswith("http"):
            abs_url = href
        elif href.startswith("//"):
            abs_url = "https:" + href
        elif href.startswith("/"):
            abs_url = BASE_URL + href
        else:
            # Relative to the board page
            base = board_url.rsplit("/", 1)[0]
            abs_url = base + "/" + href.lstrip("./")

        if abs_url in seen:
            continue
        seen.add(abs_url)
        year_pages.append((year, abs_url))

    return year_pages


# --- Year page: scrape document links ---

def scrape_year_page(html, board_name):
    """
    Return list of dicts {board, title, date, doc_url} for all PDF links
    found on a year-specific meeting page.
    """
    soup = BeautifulSoup(html, "html.parser")
    items = []

    for a in soup.find_all("a", href=True):
        href = a["href"].strip()
        if not href or href.startswith("#") or href.startswith("mailto:"):
            continue

        # Only interested in document uploads
        lower = href.lower()
        is_pdf = re.search(r"\.pdf(\?|$)", lower)
        is_doc = re.search(r"\.(docx?|xlsx?)(\?|$)", lower)
        if not (is_pdf or is_doc) or UPLOAD_PATH.lower() not in lower:
            continue

        # Normalise to absolute URL
        if href.startswith("http"):
            abs_url = href
        elif href.startswith("//"):
            abs_url = "https:" + href
        elif href.startswith("/"):
            abs_url = BASE_URL + href
        else:
            abs_url = BASE_URL + "/" + href.lstrip("./")

        title = a.get_text(" ", strip=True) or os.path.basename(href.split("?")[0])
        date = best_date_for_link(a)

        items.append({
            "board": board_name,
            "title": title,
            "date": date,
            "doc_url": abs_url,
        })

    return items


# --- File path helpers ---

def slugify(text):
    text = text.lower().strip()
    text = re.sub(r"[/\\]", "-", text)
    text = re.sub(r"[^\w\s-]", "", text)
    text = re.sub(r"[\s_]+", "-", text)
    text = re.sub(r"-{2,}", "-", text)
    return text.strip("-")[:50]


def _doc_type_label(item):
    combined = (item.get("title") or "") + " " + (item.get("doc_url") or "")
    lower = combined.lower()
    if re.search(r"\bminutes?\b", lower):
        return "minutes"
    if re.search(r"\bagenda\b", lower):
        return "agenda"
    return "doc"


def doc_dest_path(item, output_dir):
    if item["date"]:
        month_dir = os.path.join(output_dir, item["date"].strftime("%Y-%m"))
    else:
        month_dir = os.path.join(output_dir, "unknown")
    os.makedirs(month_dir, exist_ok=True)

    date_str = item["date"].strftime("%Y-%m-%d") if item["date"] else "unknown-date"
    board_slug = slugify(item["board"])
    doc_type = _doc_type_label(item)
    ext = os.path.splitext(item["doc_url"].split("?")[0])[1] or ".pdf"
    return os.path.join(month_dir, f"{date_str}-{board_slug}-{doc_type}{ext}")


# --- Main ---

def main():
    parser = argparse.ArgumentParser(
        description="Download Bethel CT municipal agendas and minutes posted in the past N days."
    )
    parser.add_argument("--days", type=int, default=DAYS_BACK, metavar="N",
                        help=f"Look back N days (default: {DAYS_BACK})")
    parser.add_argument("--output-dir", default=OUTPUT_DIR, metavar="DIR",
                        help=f"Destination directory (default: {OUTPUT_DIR})")
    parser.add_argument("--dry-run", action="store_true",
                        help="List matching items without downloading")
    parser.add_argument("--include-undated", action="store_true",
                        help="Also download documents where no date could be parsed")
    parser.add_argument("--board", metavar="NAME",
                        help="Only process boards whose name contains NAME (case-insensitive)")
    args = parser.parse_args()

    today = datetime.date.today()
    cutoff = today - datetime.timedelta(days=args.days)
    cutoff_year = cutoff.year

    print(f"Cutoff date  : {cutoff}  ({args.days} days back)")
    print(f"Hub page     : {HUB_URL}")
    print(f"Output dir   : {args.output_dir}")
    print()

    # Step 1: discover all board pages
    print("Fetching hub page...")
    hub_html = fetch_html(HUB_URL)
    if not hub_html:
        print("ERROR: Could not fetch the hub page.", file=sys.stderr)
        sys.exit(1)

    boards = discover_boards(hub_html)
    if not boards:
        print("WARNING: No board pages found — the page structure may have changed.",
              file=sys.stderr)
        sys.exit(1)

    print(f"Discovered {len(boards)} board page(s).")

    if args.board:
        filter_name = args.board.lower()
        boards = [(n, u) for n, u in boards if filter_name in n.lower()]
        print(f"Filtered to {len(boards)} matching '{args.board}'.")
    print()

    # Step 2: for each board, find year pages and scrape documents
    all_items = []

    for board_name, board_url in boards:
        print(f"  Scanning: {board_name}")
        board_html = fetch_html(board_url)
        if not board_html:
            time.sleep(DELAY_SECONDS)
            continue

        year_pages = discover_year_pages(board_html, board_url, cutoff_year)
        if not year_pages:
            # Board page may directly contain documents (some boards skip sub-pages)
            items = scrape_year_page(board_html, board_name)
            all_items.extend(items)
        else:
            for year, year_url in sorted(set(year_pages), key=lambda x: x[0], reverse=True):
                year_html = fetch_html(year_url)
                if not year_html:
                    time.sleep(DELAY_SECONDS)
                    continue
                items = scrape_year_page(year_html, board_name)
                all_items.extend(items)
                time.sleep(0.5)

        time.sleep(DELAY_SECONDS)

    print()

    # Step 3: filter by date, deduplicate by URL
    recent = []
    no_date_count = 0
    seen_urls: set = set()

    for item in all_items:
        url_key = item.get("doc_url") or ""
        if url_key in seen_urls:
            continue
        seen_urls.add(url_key)

        if item["date"] is None:
            no_date_count += 1
            if args.include_undated:
                recent.append(item)
        elif item["date"] >= cutoff:
            recent.append(item)

    recent.sort(key=lambda x: (x["date"] or datetime.date.min), reverse=True)

    undated_note = (
        f"  (+{no_date_count} undated included via --include-undated)"
        if args.include_undated and no_date_count
        else f"  ({no_date_count} undated skipped; use --include-undated to add)"
        if no_date_count else ""
    )
    print(f"Documents in window  : {len(recent)}{undated_note}")
    print()

    if not recent:
        print("No documents found within the date window.")
        sys.exit(0)

    if args.dry_run:
        print(f"{'Board':<40} {'Date':<12} {'Type':<8} File")
        print("-" * 80)
        for item in recent:
            date_s = str(item["date"]) if item["date"] else "unknown"
            fname = item["doc_url"].split("/")[-1][:35] if item["doc_url"] else ""
            doc_type = _doc_type_label(item)
            print(f"{item['board'][:39]:<40} {date_s:<12} {doc_type:<8} {fname}")
        print(f"\n{len(recent)} document(s). Re-run without --dry-run to download.")
        return

    # Step 4: download
    os.makedirs(args.output_dir, exist_ok=True)
    log_path = os.path.join(args.output_dir, "download-log.txt")
    log_lines = []
    dl_ok = dl_skip = dl_fail = 0

    for item in recent:
        board = item["board"]
        date_s = str(item["date"]) if item["date"] else "unknown"
        dest = doc_dest_path(item, args.output_dir)
        label = os.path.basename(dest)

        print(f"[{date_s}] {board}")
        if os.path.exists(dest):
            print(f"  skip (exists)  {label}")
            dl_skip += 1
            continue

        print(f"  downloading    {label}")
        if download_binary(item["doc_url"], dest):
            dl_ok += 1
            log_lines.append(f"{datetime.datetime.now().isoformat()}  OK   {dest}")
        else:
            dl_fail += 1
            log_lines.append(f"{datetime.datetime.now().isoformat()}  FAIL {item['doc_url']}")
            if os.path.exists(dest):
                os.remove(dest)

        time.sleep(DELAY_SECONDS)

    # Summary
    if log_lines:
        with open(log_path, "a") as f:
            f.write("\n".join(log_lines) + "\n")

    print()
    print(f"Downloaded: {dl_ok}  Skipped: {dl_skip}  Failed: {dl_fail}")
    if dl_ok + dl_skip:
        print(f"Files in: {args.output_dir}")
    if log_lines:
        print(f"Log:      {log_path}")


if __name__ == "__main__":
    main()


# --- Tips ---
#
# 1. Preview without downloading:
#    python3 scripts/download-bethel-agendas.py --dry-run
#
# 2. Narrow to one board:
#    python3 scripts/download-bethel-agendas.py --board "Selectmen"
#
# 3. Change the lookback window:
#    python3 scripts/download-bethel-agendas.py --days 7
#
# 4. Save files somewhere else:
#    python3 scripts/download-bethel-agendas.py --output-dir ~/Downloads/bethel
#
# 5. Include documents with no parseable date:
#    python3 scripts/download-bethel-agendas.py --include-undated
#
# 6. Run on a schedule (cron — 7 AM daily):
#    0 7 * * * cd /path/to/repo && python3 scripts/download-bethel-agendas.py
