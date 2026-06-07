#!/usr/bin/env python3
# download-newtown-agendas.py
# Download Newtown CT municipal agendas and minutes posted in the past N days.
#
# USAGE:
#   python3 scripts/download-newtown-agendas.py [options]
#
# REQUIREMENTS:
#   - Python 3.8+
#   - pip install playwright beautifulsoup4
#   - python3 -m playwright install chromium
#
# WHAT IT DOES:
#   1. Fetches https://www.newtown-ct.gov/minutes-and-agendas (via Playwright
#      to pass Cloudflare's bot challenge) to discover all 44 boards
#   2. For each board, fetches /node/{id}/agenda/{year} and
#      /node/{id}/minutes/{year} for each year that overlaps the date window
#   3. Parses the (meeting_date, download_url) pairs from the year list pages
#      using the dc:date span + views-row container structure
#   4. Filters rows whose meeting date falls within the lookback window
#   5. Downloads each PDF via Playwright's expect_download handler
#   6. Saves files to beat-archive/newtown-agendas/YYYY-MM/
#   7. Appends a download log to beat-archive/newtown-agendas/download-log.txt
#
# SITE STRUCTURE (CivicPlus Drupal, Cloudflare-protected):
#   Index:    https://www.newtown-ct.gov/minutes-and-agendas
#   Agendas:  /node/{id}/agenda/{year}   e.g. /node/443/agenda/2026
#   Minutes:  /node/{id}/minutes/{year}  e.g. /node/443/minutes/2026
#   Download: /board-selectmen/agenda/board-selectmen-204  (direct PDF download)
#
# NOTES:
#   - www.newtown-ct.gov is behind Cloudflare's managed bot challenge (requires
#     JavaScript execution). Plain urllib/requests all get HTTP 403. Playwright
#     solves this by running real Chromium.
#   - Downloads are triggered by navigating to the individual document URL,
#     which immediately starts a file download rather than rendering a page.
#   - Newtown does not embed video/recording links in its agenda center.
#   - The CivicPlus backend (ct-newtown.civicplus.com) only mirrors data up to
#     a certain point; 2026+ agendas live exclusively on the main site.

import argparse
import datetime
import os
import re
import sys
import time

try:
    from bs4 import BeautifulSoup
except ImportError:
    print("ERROR: beautifulsoup4 not installed.\n  pip install beautifulsoup4",
          file=sys.stderr)
    sys.exit(1)

try:
    from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout
except ImportError:
    print(
        "ERROR: playwright not installed.\n"
        "  pip install playwright\n"
        "  python3 -m playwright install chromium",
        file=sys.stderr,
    )
    sys.exit(1)

BASE_URL = "https://www.newtown-ct.gov"
INDEX_URL = f"{BASE_URL}/minutes-and-agendas"
OUTPUT_DIR = "beat-archive/newtown-agendas"
DAYS_BACK = 4
PAGE_TIMEOUT = 30_000   # ms
DOWNLOAD_TIMEOUT = 60_000  # ms
DELAY_SECONDS = 0.5

UA = (
    "Mozilla/5.0 (X11; Linux x86_64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)

_NODE_RE = re.compile(r"/node/(\d+)/")


def slugify(text):
    text = text.lower().strip()
    text = re.sub(r"[/\\]", "-", text)
    text = re.sub(r"[^\w\s-]", "", text)
    text = re.sub(r"[\s_]+", "-", text)
    text = re.sub(r"-{2,}", "-", text)
    return text.strip("-")[:60]


def load_page(page, url):
    """Navigate to url and return page HTML. Returns None if navigation
    immediately triggers a file download instead of rendering a page."""
    try:
        page.goto(url, wait_until="networkidle", timeout=PAGE_TIMEOUT)
        return page.content()
    except PWTimeout:
        return page.content()
    except Exception as e:
        if "Download is starting" in str(e) or "ERR_ABORTED" in str(e):
            return None
        raise


def parse_index(html):
    """
    Parse the minutes-and-agendas index page.
    Returns a list of dicts: {name, node_id, agenda_base, minutes_base}
    agenda_base / minutes_base are full URLs like https://.../node/443/agenda
    """
    soup = BeautifulSoup(html, "html.parser")
    boards = {}

    for a in soup.find_all("a", href=True):
        href = a["href"]
        m = _NODE_RE.search(href)
        if not m:
            continue
        node_id = m.group(1)

        if href.endswith("/agenda") or re.search(r"/node/\d+/agenda$", href):
            name = a.get_text(" ", strip=True)
            if node_id not in boards:
                boards[node_id] = {"name": name, "node_id": node_id,
                                   "agenda_base": None, "minutes_base": None}
            boards[node_id]["agenda_base"] = (
                href if href.startswith("http") else BASE_URL + href
            )
            if not boards[node_id]["name"]:
                boards[node_id]["name"] = name

        elif href.endswith("/minutes") or re.search(r"/node/\d+/minutes$", href):
            name = a.get_text(" ", strip=True)
            if node_id not in boards:
                boards[node_id] = {"name": name, "node_id": node_id,
                                   "agenda_base": None, "minutes_base": None}
            boards[node_id]["minutes_base"] = (
                href if href.startswith("http") else BASE_URL + href
            )
            if not boards[node_id]["name"]:
                boards[node_id]["name"] = name

    return list(boards.values())


def parse_year_list(html, doc_type):
    """
    Parse a year-specific listing page (e.g. /node/443/agenda/2026).
    Returns a list of dicts: {meeting_date, download_url, title}
    """
    soup = BeautifulSoup(html, "html.parser")
    results = []

    for row in soup.find_all("div", class_="views-row"):
        # Download URL from h3 > a (opens the PDF directly)
        h3 = row.find("h3")
        if not h3:
            continue
        a = h3.find("a", href=True)
        if not a:
            continue
        href = a["href"]
        download_url = href if href.startswith("http") else BASE_URL + href
        title = a.get_text(" ", strip=True)

        # Date from dc:date span
        date_span = row.find("span", attrs={"property": "dc:date"})
        if not date_span:
            continue
        content = date_span.get("content", "")
        dm = re.match(r"(\d{4}-\d{2}-\d{2})", content)
        if not dm:
            continue
        try:
            meeting_date = datetime.date.fromisoformat(dm.group(1))
        except ValueError:
            continue

        results.append({
            "meeting_date": meeting_date,
            "download_url": download_url,
            "doc_type": doc_type,
            "title": title,
        })

    return results


def main():
    parser = argparse.ArgumentParser(
        description="Download Newtown CT municipal agendas and minutes "
                    "posted in the past N days."
    )
    parser.add_argument("--days", type=int, default=DAYS_BACK, metavar="N",
                        help=f"Look back N days (default: {DAYS_BACK})")
    parser.add_argument("--output-dir", default=OUTPUT_DIR, metavar="DIR",
                        help=f"Destination directory (default: {OUTPUT_DIR})")
    parser.add_argument("--dry-run", action="store_true",
                        help="List matching items without downloading")
    parser.add_argument("--board", metavar="NAME",
                        help="Only process boards whose name contains NAME (case-insensitive)")
    parser.add_argument("--no-minutes", action="store_true",
                        help="Skip minutes, download agendas only")
    parser.add_argument("--no-agendas", action="store_true",
                        help="Skip agendas, download minutes only")
    parser.add_argument("--show-browser", action="store_true",
                        help="Run with a visible browser window (useful for debugging)")
    args = parser.parse_args()

    if datetime.date.today().weekday() in (6, 0):  # Sunday, Monday
        print("Skipping — no downloads on Sunday or Monday.")
        sys.exit(0)

    today = datetime.date.today()
    cutoff = today - datetime.timedelta(days=args.days)
    years_needed = sorted(set(range(cutoff.year, today.year + 1)), reverse=True)

    print(f"Date window : {cutoff} to {today}  ({args.days} days back)")
    print(f"Index URL   : {INDEX_URL}")
    print(f"Output dir  : {args.output_dir}")
    print()

    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=not args.show_browser)
        ctx = browser.new_context(
            user_agent=UA,
            viewport={"width": 1280, "height": 800},
            accept_downloads=True,
        )
        page = ctx.new_page()

        # --- Step 1: discover all boards ---
        print("Fetching board index...")
        html_index = load_page(page, INDEX_URL)
        if not html_index:
            print("ERROR: Could not load index page.", file=sys.stderr)
            browser.close()
            sys.exit(1)

        all_boards = parse_index(html_index)
        if not all_boards:
            print("WARNING: No boards found — page structure may have changed.",
                  file=sys.stderr)
            browser.close()
            sys.exit(1)

        print(f"Discovered {len(all_boards)} board(s).")

        if args.board:
            filter_name = args.board.lower()
            all_boards = [b for b in all_boards if filter_name in b["name"].lower()]
            print(f"Filtered to {len(all_boards)} board(s) matching '{args.board}'.")

        # --- Step 2: collect matching documents ---
        matches = []   # list of {board, doc_type, meeting_date, download_url, title}

        for board in all_boards:
            board_name = board["name"]
            pairs = []
            if not args.no_agendas and board["agenda_base"]:
                pairs.append(("agenda", board["agenda_base"]))
            if not args.no_minutes and board["minutes_base"]:
                pairs.append(("minutes", board["minutes_base"]))

            for doc_type, base_url in pairs:
                for year in years_needed:
                    year_url = f"{base_url}/{year}"
                    html = load_page(page, year_url)
                    if not html:
                        continue
                    rows = parse_year_list(html, doc_type)
                    for row in rows:
                        if cutoff <= row["meeting_date"] <= today:
                            matches.append({
                                "board": board_name,
                                **row,
                            })
                    time.sleep(DELAY_SECONDS)

        matches.sort(key=lambda x: (x["meeting_date"], x["board"]), reverse=True)

        doc_count = len(matches)
        board_names = sorted({m["board"] for m in matches})
        print(f"\nDocuments in window : {doc_count}")
        if board_names:
            print(f"Boards with matches : {len(board_names)}")
        print()

        if not matches:
            print("No documents found within the date window.")
            browser.close()
            sys.exit(0)

        if args.dry_run:
            print(f"{'Board':<45} {'Date':<12} {'Type':<8} {'Title'}")
            print("-" * 95)
            for m in matches:
                print(f"{m['board'][:44]:<45} {m['meeting_date']!s:<12} {m['doc_type']:<8} {m['title'][:30]}")
            print(f"\n{doc_count} document(s). Re-run without --dry-run to download.")
            browser.close()
            return

        # --- Step 3: download ---
        os.makedirs(args.output_dir, exist_ok=True)
        log_path = os.path.join(args.output_dir, "download-log.txt")
        log_lines = []
        dl_ok = dl_skip = dl_fail = 0

        for m in matches:
            board_slug = slugify(m["board"])
            date_str = m["meeting_date"].strftime("%Y-%m-%d")
            month_dir = os.path.join(args.output_dir, m["meeting_date"].strftime("%Y-%m"))
            os.makedirs(month_dir, exist_ok=True)
            dest = os.path.join(month_dir, f"{date_str}-{board_slug}-{m['doc_type']}.pdf")

            if os.path.exists(dest):
                print(f"  skip (exists)  {os.path.basename(dest)}")
                dl_skip += 1
                continue

            print(f"  [{m['meeting_date']}] {m['board']} — {m['doc_type']}")
            print(f"  downloading    {os.path.basename(dest)}")

            dl_page = ctx.new_page()
            try:
                with dl_page.expect_download(timeout=DOWNLOAD_TIMEOUT) as dl_info:
                    try:
                        dl_page.goto(m["download_url"], timeout=DOWNLOAD_TIMEOUT)
                    except Exception as nav_err:
                        if ("Download is starting" not in str(nav_err)
                                and "ERR_ABORTED" not in str(nav_err)):
                            raise
                download = dl_info.value
                download.save_as(dest)
                dl_ok += 1
                log_lines.append(
                    f"{datetime.datetime.now().isoformat()}  OK      {dest}")
            except Exception as e:
                print(f"  WARNING: {e}", file=sys.stderr)
                dl_fail += 1
                log_lines.append(
                    f"{datetime.datetime.now().isoformat()}  FAIL    {m['download_url']}")
                if os.path.exists(dest):
                    os.remove(dest)
            finally:
                dl_page.close()

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
#    python3 scripts/download-newtown-agendas.py --dry-run
#
# 2. Narrow to one board:
#    python3 scripts/download-newtown-agendas.py --board "Board of Selectmen"
#
# 3. Change the lookback window:
#    python3 scripts/download-newtown-agendas.py --days 60
#
# 4. Save files somewhere else:
#    python3 scripts/download-newtown-agendas.py --output-dir ~/Downloads/newtown
#
# 5. Agendas only (skip minutes):
#    python3 scripts/download-newtown-agendas.py --no-minutes
#
# 6. Debug Cloudflare issues with a visible browser:
#    python3 scripts/download-newtown-agendas.py --show-browser
#
# 7. Run on a schedule (cron — 7 AM daily):
#    0 7 * * * cd /path/to/repo && python3 scripts/download-newtown-agendas.py
#
# WHY PLAYWRIGHT (NOT urllib/requests):
#   www.newtown-ct.gov is behind Cloudflare's "managed challenge" bot protection,
#   which requires JavaScript execution to pass. All plain HTTP clients receive
#   HTTP 403. Playwright runs real Chromium, solves the challenge, and keeps the
#   session cookie valid for the duration of the run.
