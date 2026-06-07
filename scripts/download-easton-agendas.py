#!/usr/bin/env python3
# download-easton-agendas.py
# Download Easton CT municipal agendas and minutes posted in the past N days.
#
# USAGE:
#   python3 scripts/download-easton-agendas.py [options]
#
# REQUIREMENTS:
#   - Python 3.8+
#   - pip install playwright beautifulsoup4
#   - python3 -m playwright install chromium
#
# WHAT IT DOES:
#   1. Fetches https://www.eastonct.gov/minutes-and-agendas (via Playwright
#      to pass Cloudflare's bot challenge) to discover all boards
#   2. For each board, fetches /node/{id}/agenda/{year} and
#      /node/{id}/minutes/{year} for each year that overlaps the date window
#   3. Parses the (meeting_date, doc_page_url, title) pairs from the year list
#      pages using the dc:date span + views-row structure
#   4. Filters rows whose meeting date falls within the lookback window
#   5. For each matching document, navigates to its doc page and either:
#      a. Captures a direct file download (common for minutes)
#      b. Finds the embedded PDF link and downloads it via urllib (most agendas)
#   6. Saves any Zoom recording or individual Vimeo video links found on doc
#      pages as .url shortcut files alongside the PDF
#   7. Saves files to beat-archive/easton-agendas/YYYY-MM/
#   8. Appends a download log to beat-archive/easton-agendas/download-log.txt
#
# SITE STRUCTURE (CivicPlus Drupal 7, Cloudflare-protected):
#   Index:     https://www.eastonct.gov/minutes-and-agendas
#   Agendas:   /node/{id}/agenda/{year}   e.g. /node/2491/agenda/2026
#   Minutes:   /node/{id}/minutes/{year}  e.g. /node/2491/minutes/2026
#   Doc pages: /board-name/agenda/meeting-slug  → content page with embedded PDF
#              /board-name/minutes/meeting-slug → often a direct browser download
#   PDF files: /sites/g/files/vyhlif3071/f/agendas/*.pdf  (direct download OK)
#              /sites/g/files/vyhlif3071/f/minutes/*.pdf  (direct download OK)
#
# RECORDING LINKS:
#   - Vimeo showcase archives per board: https://www.eastonct.gov/channel-79/pages/meeting-recordings
#     These are static archive links (not per-meeting) and are not downloaded here.
#   - Individual Zoom /rec/ or Vimeo video links found on doc pages are saved
#     as .url shortcut files (use --no-video to skip).
#   - Zoom /j/ join links are skipped (meeting invites, not recordings).
#
# NOTES:
#   - www.eastonct.gov is behind Cloudflare; plain urllib gets HTTP 403.
#     Playwright runs real Chromium, passes the challenge, and maintains the
#     session cookie for all subsequent page loads.
#   - PDFs at /sites/g/files/... are accessible without Playwright (HTTP 200).
#   - Minutes doc pages usually trigger a direct browser download.
#     Agenda doc pages usually load an HTML content page with an embedded PDF link.

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

BASE_URL = "https://www.eastonct.gov"
INDEX_URL = f"{BASE_URL}/minutes-and-agendas"
OUTPUT_DIR = "beat-archive/easton-agendas"
DAYS_BACK = 4
PAGE_TIMEOUT = 30_000       # ms — page navigation
DIRECT_DL_TIMEOUT = 5_000  # ms — short window to detect direct downloads
DELAY_SECONDS = 0.5

UA = (
    "Mozilla/5.0 (X11; Linux x86_64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)

_NODE_RE = re.compile(r"/node/(\d+)/")
_DATE_RE = re.compile(r"(\d{4}-\d{2}-\d{2})")

# Recording: Zoom recordings and individual Vimeo video URLs
_ZOOM_REC_RE = re.compile(r"zoom\.us/rec/", re.IGNORECASE)
_VIMEO_VIDEO_RE = re.compile(r"vimeo\.com/\d+", re.IGNORECASE)


def slugify(text):
    text = text.lower().strip()
    text = re.sub(r"[/\\]", "-", text)
    text = re.sub(r"[^\w\s-]", "", text)
    text = re.sub(r"[\s_]+", "-", text)
    text = re.sub(r"-{2,}", "-", text)
    return text.strip("-")[:60]


def load_page(page, url):
    """Navigate to url and return HTML, or None if a download triggered."""
    try:
        page.goto(url, wait_until="networkidle", timeout=PAGE_TIMEOUT)
        return page.content()
    except PWTimeout:
        return page.content()
    except Exception as e:
        if "Download is starting" in str(e) or "ERR_ABORTED" in str(e):
            return None
        raise


def download_binary(url, dest_path):
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            with open(dest_path, "wb") as f:
                f.write(r.read())
        return True
    except Exception as e:
        print(f"  WARNING: {e} — {url}", file=sys.stderr)
        return False


def save_url_shortcut(url, dest_path):
    with open(dest_path, "w", encoding="utf-8") as f:
        f.write(f"[InternetShortcut]\nURL={url}\n")


def parse_index(html):
    """
    Parse the minutes-and-agendas index page.
    Returns list of {name, node_id, agenda_base, minutes_base}.
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
    Parse a year-specific listing page (e.g. /node/2491/agenda/2026).
    Returns list of {meeting_date, doc_page_url, title, doc_type}.
    """
    soup = BeautifulSoup(html, "html.parser")
    results = []

    for row in soup.find_all("div", class_="views-row"):
        h3 = row.find("h3")
        if not h3:
            continue
        a = h3.find("a", href=True)
        if not a:
            continue
        href = a["href"]
        doc_page_url = href if href.startswith("http") else BASE_URL + href
        title = a.get_text(" ", strip=True)

        date_span = row.find("span", attrs={"property": "dc:date"})
        if not date_span:
            continue
        content = date_span.get("content", "")
        dm = _DATE_RE.match(content)
        if not dm:
            continue
        try:
            meeting_date = datetime.date.fromisoformat(dm.group(1))
        except ValueError:
            continue

        results.append({
            "meeting_date": meeting_date,
            "doc_page_url": doc_page_url,
            "doc_type": doc_type,
            "title": title,
        })

    return results


def find_pdf_url(html):
    """
    Find the meeting PDF link on a content page.
    Prefers /f/agendas/ or /f/minutes/; falls back to any non-boilerplate
    vyhlif PDF.  Returns an absolute URL string or None.
    """
    soup = BeautifulSoup(html, "html.parser")
    # Primary: doc-specific subdirectory
    for a in soup.find_all("a", href=True):
        href = a["href"]
        if "/f/agendas/" in href or "/f/minutes/" in href:
            return href if href.startswith("http") else BASE_URL + href
    # Fallback: any vyhlif PDF that isn't a boilerplate upload
    for a in soup.find_all("a", href=True):
        href = a["href"]
        if ("vyhlif" in href and href.lower().endswith(".pdf")
                and "/f/uploads/" not in href):
            return href if href.startswith("http") else BASE_URL + href
    return None


def find_recording_links(html):
    """Return Zoom recording and individual Vimeo video URLs from a page."""
    soup = BeautifulSoup(html, "html.parser")
    seen = set()
    links = []
    for a in soup.find_all("a", href=True):
        href = a["href"]
        if (_ZOOM_REC_RE.search(href) or _VIMEO_VIDEO_RE.search(href)) and href not in seen:
            seen.add(href)
            links.append(href)
    return links


def recording_label(url):
    if _ZOOM_REC_RE.search(url):
        return "zoom-rec"
    m = _VIMEO_VIDEO_RE.search(url)
    return f"vimeo-{m.group(0).split('/')[-1]}" if m else "recording"


def main():
    parser = argparse.ArgumentParser(
        description="Download Easton CT municipal agendas and minutes "
                    "posted in the past N days."
    )
    parser.add_argument("--days", type=int, default=DAYS_BACK, metavar="N",
                        help=f"Look back N days (default: {DAYS_BACK})")
    parser.add_argument("--output-dir", default=OUTPUT_DIR, metavar="DIR",
                        help=f"Destination directory (default: {OUTPUT_DIR})")
    parser.add_argument("--dry-run", action="store_true",
                        help="List matching items without downloading")
    parser.add_argument("--board", metavar="NAME",
                        help="Only process boards whose name contains NAME "
                             "(case-insensitive)")
    parser.add_argument("--no-minutes", action="store_true",
                        help="Skip minutes, download agendas only")
    parser.add_argument("--no-agendas", action="store_true",
                        help="Skip agendas, download minutes only")
    parser.add_argument("--no-video", action="store_true",
                        help="Skip recording links (Zoom/Vimeo .url shortcuts)")
    parser.add_argument("--show-browser", action="store_true",
                        help="Run with a visible browser window (useful for debugging)")
    args = parser.parse_args()

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
        matches = []

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
                            matches.append({"board": board_name, **row})
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
            print("-" * 100)
            for m in matches:
                print(f"{m['board'][:44]:<45} {m['meeting_date']!s:<12} "
                      f"{m['doc_type']:<8} {m['title'][:35]}")
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
            title_slug = slugify(m["title"])
            date_str = m["meeting_date"].strftime("%Y-%m-%d")
            month_dir = os.path.join(args.output_dir, m["meeting_date"].strftime("%Y-%m"))
            os.makedirs(month_dir, exist_ok=True)
            dest_pdf = os.path.join(
                month_dir,
                f"{date_str}-{board_slug}-{m['doc_type']}-{title_slug}.pdf"
            )

            if os.path.exists(dest_pdf):
                print(f"  skip (exists)  {os.path.basename(dest_pdf)}")
                dl_skip += 1
                continue

            print(f"  [{m['meeting_date']}] {m['board']} — {m['doc_type']}")
            print(f"  downloading    {os.path.basename(dest_pdf)}")

            dl_page = ctx.new_page()
            pdf_ok = False
            recording_links = []
            try:
                # Attempt 1: direct download (common for minutes, some agendas)
                try:
                    with dl_page.expect_download(timeout=DIRECT_DL_TIMEOUT) as dl_info:
                        try:
                            dl_page.goto(m["doc_page_url"], timeout=PAGE_TIMEOUT)
                        except Exception as nav_err:
                            if ("Download is starting" not in str(nav_err)
                                    and "ERR_ABORTED" not in str(nav_err)):
                                raise
                    dl_info.value.save_as(dest_pdf)
                    pdf_ok = True
                except PWTimeout:
                    # Attempt 2: embedded PDF link on content page (most agendas)
                    html_doc = dl_page.content()
                    pdf_url = find_pdf_url(html_doc)
                    if pdf_url:
                        pdf_ok = download_binary(pdf_url, dest_pdf)
                    if not args.no_video:
                        recording_links = find_recording_links(html_doc)

                if pdf_ok:
                    dl_ok += 1
                    log_lines.append(
                        f"{datetime.datetime.now().isoformat()}  OK      {dest_pdf}")
                else:
                    dl_fail += 1
                    log_lines.append(
                        f"{datetime.datetime.now().isoformat()}  FAIL    "
                        f"{m['doc_page_url']}")
                    if os.path.exists(dest_pdf):
                        os.remove(dest_pdf)

            except Exception as e:
                print(f"  WARNING: {e}", file=sys.stderr)
                dl_fail += 1
                log_lines.append(
                    f"{datetime.datetime.now().isoformat()}  FAIL    {m['doc_page_url']}")
                if os.path.exists(dest_pdf):
                    os.remove(dest_pdf)
            finally:
                dl_page.close()

            # Save recording links as .url shortcuts
            if not args.no_video and recording_links:
                for i, rec_url in enumerate(recording_links, 1):
                    suffix = f"-rec{i}" if len(recording_links) > 1 else ""
                    rec_dest = os.path.join(
                        month_dir,
                        f"{date_str}-{board_slug}-{m['doc_type']}-{title_slug}{suffix}.url"
                    )
                    if not os.path.exists(rec_dest):
                        save_url_shortcut(rec_url, rec_dest)
                        label = recording_label(rec_url)
                        print(f"  saved {label:<14} {os.path.basename(rec_dest)}")
                        dl_ok += 1
                        log_lines.append(
                            f"{datetime.datetime.now().isoformat()}  OK      {rec_dest}")

            time.sleep(DELAY_SECONDS)

        browser.close()

    if log_lines:
        with open(log_path, "a") as f:
            f.write("\n".join(log_lines) + "\n")

    print()
    print(f"Downloaded/saved: {dl_ok}  Skipped: {dl_skip}  Failed: {dl_fail}")
    if dl_ok + dl_skip:
        print(f"Files in: {args.output_dir}")
    if log_lines:
        print(f"Log:      {log_path}")


if __name__ == "__main__":
    main()


# --- Tips ---
#
# 1. Preview without downloading:
#    python3 scripts/download-easton-agendas.py --dry-run
#
# 2. Narrow to one board:
#    python3 scripts/download-easton-agendas.py --board "Board of Selectmen"
#
# 3. Change the lookback window:
#    python3 scripts/download-easton-agendas.py --days 60
#
# 4. Save files somewhere else:
#    python3 scripts/download-easton-agendas.py --output-dir ~/Downloads/easton
#
# 5. Agendas only (skip minutes):
#    python3 scripts/download-easton-agendas.py --no-minutes
#
# 6. PDFs only (skip recording shortcuts):
#    python3 scripts/download-easton-agendas.py --no-video
#
# 7. Debug Cloudflare issues with a visible browser:
#    python3 scripts/download-easton-agendas.py --show-browser
#
# 8. Run on a schedule (cron — 7 AM daily):
#    0 7 * * * cd /path/to/repo && python3 scripts/download-easton-agendas.py
#
# RECORDING ARCHIVES:
#   Easton posts per-board Vimeo showcase archives at:
#   https://www.eastonct.gov/channel-79/pages/meeting-recordings
#   These static per-board links are not downloaded by this script.
#   Individual Zoom recording or Vimeo video links found on specific meeting
#   pages are saved as .url shortcut files alongside the corresponding PDF.
#
# WHY PLAYWRIGHT (NOT urllib/requests):
#   www.eastonct.gov is behind Cloudflare's managed bot challenge, which
#   requires JavaScript execution. Plain HTTP clients receive HTTP 403.
#   Playwright runs real Chromium, passes the challenge, and maintains the
#   session cookie for all subsequent page loads.
#
# HOW DOWNLOADS WORK (two paths):
#   Minutes doc pages: navigating to the URL triggers an immediate browser
#   download — captured with Playwright's expect_download handler (5s timeout).
#   Agenda doc pages: navigation loads a content page containing an embedded
#   PDF link at /sites/g/files/vyhlif3071/f/agendas/; the PDF is then
#   fetched directly with urllib (no Playwright needed for the file itself).
#   Both paths are handled automatically by the same download loop.
