#!/usr/bin/env python3
# download-wilton-agendas.py
# Download Wilton CT municipal agendas and minutes posted in the past N days.
#
# USAGE:
#   python3 scripts/download-wilton-agendas.py [options]
#
# REQUIREMENTS:
#   - Python 3.8+
#   - pip install playwright beautifulsoup4
#   - playwright install chromium
#
# WHY PLAYWRIGHT:
#   wiltonct.gov sits behind Cloudflare, which blocks plain HTTP clients.
#   Playwright drives a real Chromium browser that passes the JS challenge.
#
# WHAT IT DOES:
#   1. Loads the Wilton CT Minutes & Agendas hub page
#   2. Collects all board /node/XXXX/agenda and /node/XXXX/minutes links
#   3. For each board+type, loads the year-filtered view (e.g. /node/XXXX/agenda/2026)
#   4. Parses ISO datetime from each row's date span, filters to the lookback window
#   5. Visits each matching item page to find the PDF download link
#      - handles pages that serve the file directly (browser download trigger)
#      - handles HTML pages that contain a PDF <a href>
#   6. Saves PDFs to beat-archive/wilton-agendas/YYYY-MM/
#   7. Appends a download log to beat-archive/wilton-agendas/download-log.txt
#
# SITE STRUCTURE (Drupal 7, Cloudflare-protected):
#   Hub:     https://www.wiltonct.gov/minutes-and-agendas
#   List:    https://www.wiltonct.gov/node/XXXX/agenda/YYYY  (views-row blocks)
#   Item:    https://www.wiltonct.gov/[board-slug]/agenda/[item-slug]
#   Files:   https://www.wiltonct.gov/sites/g/files/vyhlif10026/f/agendas/file.pdf

import argparse
import datetime
import os
import re
import sys
import time
import urllib.request
import urllib.error

try:
    from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout
except ImportError:
    print(
        "ERROR: playwright is not installed.\n"
        "  pip install playwright\n"
        "  playwright install chromium",
        file=sys.stderr,
    )
    sys.exit(1)

try:
    from bs4 import BeautifulSoup
except ImportError:
    print("ERROR: beautifulsoup4 is not installed.\n  pip install beautifulsoup4",
          file=sys.stderr)
    sys.exit(1)

BASE_URL = "https://www.wiltonct.gov"
HUB_URL = f"{BASE_URL}/minutes-and-agendas"
OUTPUT_DIR = "beat-archive/wilton-agendas"
DAYS_BACK = 4
DELAY_SECONDS = 1.5

UA = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)


# --- Helpers ---

def slugify(text):
    text = text.lower().strip()
    text = re.sub(r"[/\\]", "-", text)
    text = re.sub(r"[^\w\s-]", "", text)
    text = re.sub(r"[\s_]+", "-", text)
    text = re.sub(r"-{2,}", "-", text)
    return text.strip("-")[:60]


def parse_iso_date(iso_str):
    """Parse 'YYYY-MM-DDTHH:MM:SS±HH:MM' → datetime.date, or None."""
    m = re.match(r"(\d{4})-(\d{2})-(\d{2})", iso_str or "")
    if m:
        try:
            return datetime.date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
        except ValueError:
            pass
    return None


def dest_path_for(board, doc_type, meeting_date, output_dir):
    date_str = meeting_date.strftime("%Y-%m-%d") if meeting_date else "unknown"
    month_dir = meeting_date.strftime("%Y-%m") if meeting_date else "unknown"
    board_slug = slugify(board)
    folder = os.path.join(output_dir, month_dir)
    os.makedirs(folder, exist_ok=True)
    return os.path.join(folder, f"{date_str}-{board_slug}-{doc_type}.pdf")


def download_binary(url, dest_path, ua=UA):
    """Download url to dest_path via urllib. Returns True on success."""
    req = urllib.request.Request(url, headers={"User-Agent": ua})
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            with open(dest_path, "wb") as f:
                f.write(r.read())
        return True
    except Exception as e:
        print(f"  WARNING: {e} — {url}", file=sys.stderr)
        return False


# --- Hub page scraping ---

def collect_boards(page):
    """
    Load hub page and return list of dicts:
      {board, node_id, agenda_url, minutes_url}
    """
    page.goto(HUB_URL, timeout=30000, wait_until="networkidle")
    time.sleep(2)

    links = page.eval_on_selector_all(
        "a[href]",
        "els => els.map(e => [e.href, e.textContent.trim()])"
    )

    boards = {}
    for href, text in links:
        m = re.search(r"/node/(\d+)/(agenda|minutes)$", href)
        if not m:
            continue
        node_id, doc_type = m.group(1), m.group(2)
        if node_id not in boards:
            boards[node_id] = {"board": text, "node_id": node_id,
                               "agenda_url": None, "minutes_url": None}
        if text:
            boards[node_id]["board"] = text
        if doc_type == "agenda":
            boards[node_id]["agenda_url"] = href
        else:
            boards[node_id]["minutes_url"] = href

    return list(boards.values())


# --- Year-filtered view scraping ---

def collect_items_for_year(page, node_id, doc_type, year):
    """
    Load /node/XXXX/(agenda|minutes)/YYYY and return list of dicts:
      {title, item_url, meeting_date}
    """
    url = f"{BASE_URL}/node/{node_id}/{doc_type}/{year}"
    try:
        page.goto(url, timeout=30000, wait_until="networkidle")
    except PWTimeout:
        return []
    time.sleep(1)

    html = page.content()
    soup = BeautifulSoup(html, "html.parser")
    items = []

    for row in soup.select("div.views-row"):
        date_span = row.select_one("span.date-display-single")
        meeting_date = None
        if date_span and date_span.get("content"):
            meeting_date = parse_iso_date(date_span["content"])

        link = row.select_one("h3 a") or row.select_one("a[href]")
        if not link:
            continue
        title = link.get_text(" ", strip=True)
        href = link.get("href", "")
        if not href or href.startswith("#"):
            continue
        item_url = href if href.startswith("http") else BASE_URL + href

        items.append({"title": title, "item_url": item_url, "meeting_date": meeting_date})

    return items


# --- Item page PDF retrieval ---

def get_pdf_url_from_page(page, item_url):
    """
    Navigate to an item page and return the PDF URL, or None.
    Handles pages that serve the PDF directly (browser download trigger) and
    pages that contain an <a href> link to the PDF.
    Returns (pdf_url_or_None, playwright_download_or_None).
    """
    try:
        with page.expect_download(timeout=8000) as dl_info:
            page.goto(item_url, wait_until="commit", timeout=20000)
        return None, dl_info.value
    except Exception:
        pass

    try:
        html = page.content()
    except Exception:
        return None, None

    soup = BeautifulSoup(html, "html.parser")
    for a in soup.find_all("a", href=True):
        href = a["href"]
        # Wilton's Drupal file identifier
        if re.search(r"vyhlif10026/f/(agendas|minutes|uploads)", href, re.I):
            full = href if href.startswith("http") else BASE_URL + href
            return full, None
        if re.search(r"\.pdf(\?|$)", href, re.I) and "wiltonct.gov" in href:
            full = href if href.startswith("http") else BASE_URL + href
            return full, None

    return None, None


# --- Main ---

def main():
    parser = argparse.ArgumentParser(
        description="Download Wilton CT municipal agendas and minutes "
                    "posted in the past N days."
    )
    parser.add_argument("--days", type=int, default=DAYS_BACK, metavar="N",
                        help=f"Look back N days (default: {DAYS_BACK})")
    parser.add_argument("--output-dir", default=OUTPUT_DIR, metavar="DIR",
                        help=f"Destination directory (default: {OUTPUT_DIR})")
    parser.add_argument("--dry-run", action="store_true",
                        help="List matching items without downloading")
    parser.add_argument("--include-undated", action="store_true",
                        help="Also process items where no meeting date could be parsed")
    parser.add_argument("--board", metavar="NAME",
                        help="Only process boards whose name contains NAME (case-insensitive)")
    parser.add_argument("--no-minutes", action="store_true",
                        help="Skip minutes, download agendas only")
    parser.add_argument("--no-agendas", action="store_true",
                        help="Skip agendas, download minutes only")
    args = parser.parse_args()

    if datetime.date.today().weekday() in (6, 0):  # Sunday, Monday
        print("Skipping — no downloads on Sunday or Monday.")
        sys.exit(0)

    cutoff = datetime.date.today() - datetime.timedelta(days=args.days)
    today = datetime.date.today()
    years_to_check = {today.year}
    if cutoff.year < today.year:
        years_to_check.add(cutoff.year)
    years_to_check = sorted(years_to_check, reverse=True)

    print(f"Cutoff date : {cutoff}  ({args.days} days back)")
    print(f"Hub page    : {HUB_URL}")
    print(f"Output dir  : {args.output_dir}")
    print(f"Years       : {', '.join(str(y) for y in years_to_check)}")
    print()

    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        ctx = browser.new_context(user_agent=UA, accept_downloads=True)
        page = ctx.new_page()

        # --- Step 1: collect all boards ---
        print("Loading hub page...")
        boards = collect_boards(page)
        if not boards:
            print("ERROR: No boards found on hub page.", file=sys.stderr)
            browser.close()
            sys.exit(1)
        print(f"Found {len(boards)} board(s).")

        if args.board:
            fn = args.board.lower()
            boards = [b for b in boards if fn in b["board"].lower()]
            print(f"Filtered to {len(boards)} board(s) matching '{args.board}'.")
        print()

        # --- Step 2: collect candidate items ---
        doc_types = []
        if not args.no_agendas:
            doc_types.append("agenda")
        if not args.no_minutes:
            doc_types.append("minutes")

        candidates = []
        no_date_count = 0

        for board in boards:
            bname = board["board"]
            nid = board["node_id"]

            for doc_type in doc_types:
                if doc_type == "agenda" and not board["agenda_url"]:
                    continue
                if doc_type == "minutes" and not board["minutes_url"]:
                    continue

                for year in years_to_check:
                    print(f"  Scanning {bname} / {doc_type} / {year}...")
                    items = collect_items_for_year(page, nid, doc_type, year)
                    time.sleep(0.5)

                    for item in items:
                        d = item["meeting_date"]
                        if d is None:
                            no_date_count += 1
                            if args.include_undated:
                                candidates.append({**item, "board": bname, "doc_type": doc_type})
                        elif d >= cutoff:
                            candidates.append({**item, "board": bname, "doc_type": doc_type})

        # Deduplicate by item_url
        seen_urls: set = set()
        unique_candidates = []
        for c in candidates:
            if c["item_url"] not in seen_urls:
                seen_urls.add(c["item_url"])
                unique_candidates.append(c)
        candidates = unique_candidates

        undated_note = (
            f"  (+{no_date_count} undated included via --include-undated)"
            if args.include_undated and no_date_count
            else f"  ({no_date_count} undated skipped; use --include-undated to add)"
            if no_date_count else ""
        )
        candidates.sort(key=lambda x: (x.get("meeting_date") or datetime.date.min), reverse=True)

        print()
        print(f"Items in date window: {len(candidates)}{undated_note}")
        print()

        if not candidates:
            print("No items found within the date window.")
            browser.close()
            sys.exit(0)

        if args.dry_run:
            print(f"{'Board':<42} {'Date':<12} {'Type':<8} Title")
            print("-" * 85)
            for c in candidates:
                date_s = str(c["meeting_date"]) if c["meeting_date"] else "unknown"
                print(f"{c['board'][:41]:<42} {date_s:<12} {c['doc_type']:<8} {c['title'][:35]}")
            print(f"\n{len(candidates)} item(s). Re-run without --dry-run to download.")
            browser.close()
            return

        # --- Step 3: download ---
        os.makedirs(args.output_dir, exist_ok=True)
        log_path = os.path.join(args.output_dir, "download-log.txt")
        log_lines = []
        dl_ok = dl_skip = dl_fail = 0

        for c in candidates:
            board = c["board"]
            doc_type = c["doc_type"]
            title = c["title"]
            meeting_date = c["meeting_date"]
            item_url = c["item_url"]
            date_s = str(meeting_date) if meeting_date else "unknown"

            dest = dest_path_for(board, doc_type, meeting_date, args.output_dir)
            label = os.path.basename(dest)

            print(f"[{date_s}] {board}  [{doc_type}]")

            if os.path.exists(dest):
                print(f"  skip (exists)  {label}")
                dl_skip += 1
                continue

            print(f"  fetching page  {item_url.split('/')[-1][:50]}")
            pdf_url, direct_dl = get_pdf_url_from_page(page, item_url)
            time.sleep(DELAY_SECONDS)

            if direct_dl is not None:
                print(f"  saving direct  {label}")
                try:
                    direct_dl.save_as(dest)
                    dl_ok += 1
                    log_lines.append(
                        f"{datetime.datetime.now().isoformat()}  OK      {dest}")
                except Exception as e:
                    print(f"  WARNING: {e}", file=sys.stderr)
                    dl_fail += 1
                    log_lines.append(
                        f"{datetime.datetime.now().isoformat()}  FAIL    {item_url}")
            elif pdf_url:
                print(f"  downloading    {label}")
                if download_binary(pdf_url, dest):
                    dl_ok += 1
                    log_lines.append(
                        f"{datetime.datetime.now().isoformat()}  OK      {dest}")
                else:
                    dl_fail += 1
                    log_lines.append(
                        f"{datetime.datetime.now().isoformat()}  FAIL    {pdf_url}")
                    if os.path.exists(dest):
                        os.remove(dest)
            else:
                print(f"  WARNING: no PDF found — {item_url}", file=sys.stderr)
                dl_fail += 1
                log_lines.append(
                    f"{datetime.datetime.now().isoformat()}  NO-PDF  {item_url}")

            time.sleep(DELAY_SECONDS)

        browser.close()

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
#    python3 scripts/download-wilton-agendas.py --dry-run
#
# 2. Narrow to one board:
#    python3 scripts/download-wilton-agendas.py --board "Planning"
#
# 3. Change the lookback window:
#    python3 scripts/download-wilton-agendas.py --days 7
#
# 4. Save files somewhere else:
#    python3 scripts/download-wilton-agendas.py --output-dir ~/Downloads/wilton
#
# 5. Agendas only (skip minutes):
#    python3 scripts/download-wilton-agendas.py --no-minutes
#
# 6. Run on a schedule (cron — 7 AM daily):
#    0 7 * * * cd /path/to/repo && python3 scripts/download-wilton-agendas.py
#
# NOTE: First run may be slow because Playwright loads each board's listing page.
# Subsequent runs skip already-downloaded files instantly.
