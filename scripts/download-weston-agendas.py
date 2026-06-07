#!/usr/bin/env python3
# download-weston-agendas.py
# Download Weston CT municipal agendas and minutes posted in the past N days.
#
# USAGE:
#   python3 scripts/download-weston-agendas.py [options]
#
# REQUIREMENTS:
#   - Python 3.8+
#   - pip install playwright beautifulsoup4
#   - playwright install chromium
#   - A display (X11 or Wayland) OR xvfb-run for headless servers
#
# WHY NON-HEADLESS PLAYWRIGHT:
#   westonct.gov is protected by Akamai Bot Manager, which fingerprints
#   headless browsers and returns HTTP 403 to them. A non-headless Chromium
#   window (with a real display) passes the fingerprint check.
#
#   If you have no display, prefix the command with xvfb-run:
#     xvfb-run python3 scripts/download-weston-agendas.py
#
#   If you are on a home/office IP (not a datacenter), try --headless first;
#   Akamai's IP-reputation block mainly targets cloud/server IPs.
#
# WHAT IT DOES:
#   1. Loads westonct.gov/government/boards-commissions to discover all boards
#   2. For each board, visits its page and finds year folder links (/-folder-NNN)
#      whose label starts with a year in the date window (e.g. "2026", "2026 Agendas")
#   3. Visits each year folder and collects showpublisheddocument links + titles
#   4. Parses the meeting date from the document title (e.g. "04-06-2026 Minutes")
#   5. Filters to documents whose meeting date falls within the lookback window
#   6. Downloads each PDF to beat-archive/weston-agendas/YYYY-MM/ using a fresh
#      Playwright page per download (avoids page-close side effects)
#   7. Saves a .url shortcut to Weston's Vimeo recording channel
#   8. Appends a download log to beat-archive/weston-agendas/download-log.txt
#
# SITE STRUCTURE (Granicus CMS, Akamai-protected):
#   Boards hub:   /government/boards-commissions
#   Board page:   /government/boards-commissions/{elected|appointed}/{slug}
#   Year folder:  /government/boards-commissions/.../-folder-NNN
#   Document:     /home/showpublisheddocument/{docId}/{dotnetTicks}
#
# RECORDING LINKS:
#   Weston posts meeting videos on Vimeo at https://vimeo.com/westonct/videos
#   (accessible via /government/boards-commissions/meeting-videos on the town site).
#   This script saves a .url shortcut to that channel each run; individual
#   per-meeting Vimeo links are not scraped separately since they require the
#   Vimeo API or authenticated access.

import argparse
import datetime
import os
import re
import sys
import time

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

BASE_URL = "https://www.westonct.gov"
BOARDS_URL = f"{BASE_URL}/government/boards-commissions"
VIMEO_CHANNEL_URL = "https://vimeo.com/westonct/videos"
OUTPUT_DIR = "beat-archive/weston-agendas"
DAYS_BACK = 4
DELAY_SECONDS = 1.5

UA = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)

# Granicus document URL token
SHOWDOC_RE = re.compile(r"/home/showpublisheddocument/(\d+)/(\d+)", re.I)

# Sidebar navigation PDFs present on every page — exclude them
_SIDEBAR_DOC_IDS = {"324", "520"}  # Building Permits, Marriage License

# Date patterns against document title text (most-specific first)
_DATE_FORMATS = [
    # MM-DD-YYYY  e.g. "04-06-2026"
    (re.compile(r"\b(\d{1,2})-(\d{1,2})-(20\d{2})\b"), "MDY-dash"),
    # YYYY-MM-DD  e.g. "2026-04-06"
    (re.compile(r"\b(20\d{2})-(\d{2})-(\d{2})\b"), "YMD"),
    # MM/DD/YYYY
    (re.compile(r"\b(\d{1,2})/(\d{1,2})/(20\d{2})\b"), "MDY-slash"),
    # "April 6, 2026" / "April 6 2026"
    (re.compile(
        r"\b(January|February|March|April|May|June|July|August|September|"
        r"October|November|December)\s+(\d{1,2}),?\s+(20\d{2})\b", re.I
    ), "month-name"),
]

# .NET epoch for ticks-to-date fallback
_NET_EPOCH = datetime.datetime(1, 1, 1)


# --- Helpers ---

def parse_date_from_text(text):
    """Return the first date found in text as datetime.date, or None."""
    for pattern, fmt in _DATE_FORMATS:
        m = pattern.search(text or "")
        if not m:
            continue
        try:
            if fmt == "MDY-dash":
                return datetime.date(int(m.group(3)), int(m.group(1)), int(m.group(2)))
            elif fmt == "YMD":
                return datetime.date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
            elif fmt == "MDY-slash":
                return datetime.date(int(m.group(3)), int(m.group(1)), int(m.group(2)))
            elif fmt == "month-name":
                return datetime.datetime.strptime(
                    f"{m.group(1)} {int(m.group(2)):02d} {m.group(3)}", "%B %d %Y"
                ).date()
        except ValueError:
            continue
    return None


def ticks_to_date(ticks_str):
    """Convert a .NET DateTime ticks string to datetime.date."""
    try:
        ticks = int(ticks_str)
        dt = _NET_EPOCH + datetime.timedelta(microseconds=ticks // 10)
        return dt.date()
    except (ValueError, OverflowError):
        return None


def doc_type_from_text(text):
    lower = (text or "").lower()
    if "minute" in lower:
        return "minutes"
    if "agenda" in lower:
        return "agenda"
    return "document"


def slugify(text):
    text = text.lower().strip()
    text = re.sub(r"[^\w\s-]", "", text)
    text = re.sub(r"[\s_]+", "-", text)
    text = re.sub(r"-{2,}", "-", text)
    return text.strip("-")[:60]


def dest_path(board, doc_type, meeting_date, output_dir):
    date_str = meeting_date.strftime("%Y-%m-%d") if meeting_date else "unknown"
    month_dir = meeting_date.strftime("%Y-%m") if meeting_date else "unknown"
    folder = os.path.join(output_dir, month_dir)
    os.makedirs(folder, exist_ok=True)
    return os.path.join(folder, f"{date_str}-{slugify(board)}-{doc_type}.pdf")


def save_url_shortcut(url, dest_path):
    with open(dest_path, "w", encoding="utf-8") as f:
        f.write(f"[InternetShortcut]\nURL={url}\n")


# --- Browser helpers ---

def make_browser(pw, headless):
    args = ["--disable-blink-features=AutomationControlled", "--no-sandbox"]
    browser = pw.chromium.launch(headless=headless, args=args)
    ctx = browser.new_context(
        user_agent=UA,
        viewport={"width": 1280, "height": 800},
        locale="en-US",
        accept_downloads=True,
    )
    page = ctx.new_page()
    page.add_init_script(
        "Object.defineProperty(navigator, 'webdriver', {get: () => undefined})"
    )
    return browser, ctx, page


def safe_goto(page, url, timeout=25000):
    """Navigate to url; return True on success, False on error."""
    try:
        page.goto(url, timeout=timeout, wait_until="networkidle")
        time.sleep(1.5)
        return True
    except PWTimeout:
        try:
            page.goto(url, timeout=timeout, wait_until="domcontentloaded")
            time.sleep(2)
            return True
        except Exception:
            return False
    except Exception:
        return False


def check_blocked(page):
    """Return True if the page is an Akamai Access Denied response."""
    try:
        title = page.title()
        return "Access Denied" in title or "Just a moment" in title
    except Exception:
        return True  # page closed = treat as blocked


def page_html(page):
    """Return page HTML or empty string if the page is closed."""
    try:
        return page.content()
    except Exception:
        return ""


# --- Discovery ---

def collect_board_urls(page):
    """Return list of (board_url, board_name) from the boards hub page."""
    ok = safe_goto(page, BOARDS_URL)
    if not ok or check_blocked(page):
        return []

    html = page_html(page)
    if not html:
        return []
    soup = BeautifulSoup(html, "html.parser")

    boards = {}
    for a in soup.find_all("a", href=True):
        href = a["href"]
        text = a.get_text(" ", strip=True)
        if not text or len(text) < 3:
            continue
        if not re.search(
            r"/government/boards-commissions/(elected|appointed)/[^/#]+$", href
        ):
            continue
        full = href if href.startswith("http") else BASE_URL + href
        if full not in boards:
            boards[full] = text
    return list(boards.items())


def collect_year_folder_urls(page, board_url, years):
    """
    Visit the board page and return list of (year_int, folder_url) for each
    document-folder link whose label starts with a year in `years`.
    Strips anchor fragments from URLs.
    """
    ok = safe_goto(page, board_url)
    if not ok or check_blocked(page):
        return []

    html = page_html(page)
    if not html:
        return []
    soup = BeautifulSoup(html, "html.parser")

    found = []
    seen = set()
    for a in soup.find_all("a", href=True):
        href = a["href"]
        if "-folder-" not in href:
            continue
        text = a.get_text(" ", strip=True)
        m = re.match(r"^(20\d{2})\b", text)
        if not m:
            continue
        year_int = int(m.group(1))
        if year_int not in years:
            continue
        full = href if href.startswith("http") else BASE_URL + href
        full = full.split("#")[0]
        if full in seen:
            continue
        seen.add(full)
        found.append((year_int, full))
    return found


def collect_docs_from_folder(page, folder_url):
    """
    Visit the year folder URL and return list of dicts:
      {title, doc_url, doc_id, upload_date}
    Only showpublisheddocument links; sidebar boilerplate docs excluded.
    """
    ok = safe_goto(page, folder_url)
    if not ok or check_blocked(page):
        return []

    html = page_html(page)
    soup = BeautifulSoup(html, "html.parser")
    docs = []
    seen_ids = set()

    for a in soup.find_all("a", href=True):
        href = a["href"]
        m = SHOWDOC_RE.search(href)
        if not m:
            continue
        doc_id = m.group(1)
        ticks = m.group(2)
        if doc_id in seen_ids or doc_id in _SIDEBAR_DOC_IDS:
            continue
        seen_ids.add(doc_id)

        title = a.get_text(" ", strip=True)
        full_url = href if href.startswith("http") else BASE_URL + href
        upload_date = ticks_to_date(ticks)
        docs.append({
            "title": title,
            "doc_url": full_url,
            "doc_id": doc_id,
            "upload_date": upload_date,
        })

    return docs


# --- Download ---

def download_doc(ctx, doc_url, dest):
    """
    Download the document at doc_url to dest.
    Uses a fresh page per download so that the main navigation page is never
    affected by the tab-close that Granicus triggers when serving a file.
    Returns True on success.
    """
    dl_page = ctx.new_page()
    try:
        with dl_page.expect_download(timeout=30000) as dl_info:
            try:
                dl_page.goto(doc_url, timeout=25000, wait_until="commit")
            except Exception as nav_err:
                err_s = str(nav_err)
                if ("Download is starting" not in err_s
                        and "ERR_ABORTED" not in err_s
                        and "TargetClosed" not in err_s):
                    raise
        dl = dl_info.value
        dl.save_as(dest)
        return True
    except Exception as e:
        print(f"  WARNING: download failed — {e}", file=sys.stderr)
        return False
    finally:
        try:
            dl_page.close()
        except Exception:
            pass


# --- Main ---

def main():
    parser = argparse.ArgumentParser(
        description="Download Weston CT municipal agendas and minutes "
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
    parser.add_argument("--include-undated", action="store_true",
                        help="Also download docs where no meeting date can be "
                             "parsed from the title")
    parser.add_argument("--no-video", action="store_true",
                        help="Skip saving the Vimeo recording channel shortcut")
    parser.add_argument("--headless", action="store_true",
                        help="Use headless Chromium (may be blocked by Akamai "
                             "on server IPs; try from a home/office network or "
                             "use xvfb-run without this flag)")
    args = parser.parse_args()

    if datetime.date.today().weekday() in (6, 0):  # Sunday, Monday
        print("Skipping — no downloads on Sunday or Monday.")
        sys.exit(0)

    cutoff = datetime.date.today() - datetime.timedelta(days=args.days)
    today = datetime.date.today()
    years_needed = {today.year}
    if cutoff.year < today.year:
        years_needed.add(cutoff.year)

    headless = args.headless
    if not headless and not os.environ.get("DISPLAY"):
        print("WARNING: DISPLAY is not set and --headless was not requested.")
        print("  Falling back to headless mode. If blocked, try: xvfb-run python3 "
              + os.path.basename(__file__))
        headless = True

    print(f"Date window : {cutoff} to {today}  ({args.days} days back)")
    print(f"Boards hub  : {BOARDS_URL}")
    print(f"Output dir  : {args.output_dir}")
    print(f"Mode        : {'headless' if headless else 'non-headless (display=' + os.environ.get('DISPLAY','?') + ')'}")
    print()

    with sync_playwright() as pw:
        browser, ctx, page = make_browser(pw, headless)

        # --- Step 1: discover all boards ---
        print("Loading boards hub page...")
        board_urls = collect_board_urls(page)
        if not board_urls:
            print("ERROR: Could not load boards page or no boards found.", file=sys.stderr)
            if check_blocked(page):
                print("  → Akamai blocked this request.", file=sys.stderr)
                print("    Non-headless mode is the most reliable fix.", file=sys.stderr)
                print("    If no display: xvfb-run python3 " + os.path.basename(__file__),
                      file=sys.stderr)
            browser.close()
            sys.exit(1)

        print(f"Found {len(board_urls)} board page(s).")

        if args.board:
            fn = args.board.lower()
            board_urls = [(u, n) for u, n in board_urls if fn in n.lower()]
            print(f"Filtered to {len(board_urls)} board(s) matching '{args.board}'.")
        print()

        # --- Step 2: collect candidate documents ---
        candidates = []
        no_date_count = 0
        seen_doc_ids = set()

        for board_url, board_name in board_urls:
            print(f"  Scanning: {board_name}")

            year_folders = collect_year_folder_urls(page, board_url, years_needed)
            if not year_folders:
                print(f"    (no {max(years_needed)} folder found)")
                time.sleep(0.5)
                continue

            for year, folder_url in year_folders:
                docs = collect_docs_from_folder(page, folder_url)
                for doc in docs:
                    if doc["doc_id"] in seen_doc_ids:
                        continue
                    seen_doc_ids.add(doc["doc_id"])

                    meeting_date = parse_date_from_text(doc["title"])

                    if meeting_date is None:
                        no_date_count += 1
                        if args.include_undated:
                            candidates.append({**doc, "board": board_name,
                                               "meeting_date": None})
                    elif meeting_date >= cutoff:
                        candidates.append({**doc, "board": board_name,
                                           "meeting_date": meeting_date})
                time.sleep(0.5)

        candidates.sort(
            key=lambda x: (x.get("meeting_date") or datetime.date.min), reverse=True
        )

        undated_note = (
            f"  (+{no_date_count} undated included via --include-undated)"
            if args.include_undated and no_date_count
            else f"  ({no_date_count} undated skipped; use --include-undated to add)"
            if no_date_count else ""
        )
        print()
        print(f"Documents in window : {len(candidates)}{undated_note}")
        print()

        if not candidates:
            print("No documents found within the date window.")
            browser.close()
            sys.exit(0)

        if args.dry_run:
            print(f"{'Board':<40} {'Date':<12} {'Type':<9} Title")
            print("-" * 85)
            for c in candidates:
                date_s = str(c["meeting_date"]) if c["meeting_date"] else "unknown"
                dtype = doc_type_from_text(c["title"])
                print(f"{c['board'][:39]:<40} {date_s:<12} {dtype:<9} {c['title'][:30]}")
            print(f"\n{len(candidates)} document(s). Re-run without --dry-run to download.")
            browser.close()
            return

        # --- Step 3: download ---
        os.makedirs(args.output_dir, exist_ok=True)
        log_path = os.path.join(args.output_dir, "download-log.txt")
        log_lines = []
        dl_ok = dl_skip = dl_fail = 0

        for c in candidates:
            board = c["board"]
            meeting_date = c["meeting_date"]
            title = c["title"]
            doc_url = c["doc_url"]
            dtype = doc_type_from_text(title)
            date_s = str(meeting_date) if meeting_date else "unknown"

            dpath = dest_path(board, dtype, meeting_date, args.output_dir)
            label = os.path.basename(dpath)

            print(f"[{date_s}] {board}")

            if os.path.exists(dpath):
                print(f"  skip (exists)  {label}")
                dl_skip += 1
                continue

            print(f"  downloading    {label}  ({title[:40]})")
            if download_doc(ctx, doc_url, dpath):
                dl_ok += 1
                log_lines.append(
                    f"{datetime.datetime.now().isoformat()}  OK    {dpath}")
            else:
                dl_fail += 1
                log_lines.append(
                    f"{datetime.datetime.now().isoformat()}  FAIL  {doc_url}")
                if os.path.exists(dpath):
                    os.remove(dpath)

            time.sleep(DELAY_SECONDS)

        browser.close()

    # --- Save Vimeo recording channel shortcut ---
    if not args.no_video:
        vimeo_shortcut = os.path.join(args.output_dir, "weston-meeting-recordings.url")
        if not os.path.exists(vimeo_shortcut):
            os.makedirs(args.output_dir, exist_ok=True)
            save_url_shortcut(VIMEO_CHANNEL_URL, vimeo_shortcut)
            print(f"Saved recording channel: {vimeo_shortcut}")
            log_lines.append(
                f"{datetime.datetime.now().isoformat()}  OK    {vimeo_shortcut}")

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
#    python3 scripts/download-weston-agendas.py --dry-run
#
# 2. Narrow to one board:
#    python3 scripts/download-weston-agendas.py --board "Conservation Commission"
#
# 3. Change the lookback window:
#    python3 scripts/download-weston-agendas.py --days 7
#
# 4. Save files somewhere else:
#    python3 scripts/download-weston-agendas.py --output-dir ~/Downloads/weston
#
# 5. Include documents with no parseable date in the title:
#    python3 scripts/download-weston-agendas.py --include-undated
#
# 6. PDFs only (skip recording shortcut):
#    python3 scripts/download-weston-agendas.py --no-video
#
# 7. Run on a server with no display:
#    xvfb-run python3 scripts/download-weston-agendas.py
#
# 8. Run on a schedule (cron — 7 AM daily):
#    0 7 * * * cd /path/to/repo && DISPLAY=:0 python3 scripts/download-weston-agendas.py
#    OR with xvfb-run:
#    0 7 * * * cd /path/to/repo && xvfb-run python3 scripts/download-weston-agendas.py
#
# NOTE ON AKAMAI:
#   westonct.gov blocks headless browsers via Akamai Bot Manager fingerprinting.
#   Non-headless Chromium (real display) is the most reliable approach.
#   After making many requests quickly, Akamai may temporarily rate-limit the IP;
#   wait 15-30 minutes before retrying if you see "Access Denied" errors.
#
# NOTE ON RECORDINGS:
#   Weston meeting recordings are on Vimeo at https://vimeo.com/westonct/videos
#   (828 videos as of 2026). This script saves a .url shortcut to that channel
#   at beat-archive/weston-agendas/weston-meeting-recordings.url on each run.
#   Individual per-meeting Vimeo links are not scraped because the Vimeo channel
#   does not embed meeting-date metadata that can be parsed without the Vimeo API.
#
# NOTE ON MISSING BOARDS:
#   Some boards (Board of Education, Board of Ethics, Children & Youth Commission,
#   etc.) do not store documents in Granicus year folders and will show
#   "(no 2026 folder found)". This is expected — those boards maintain documents
#   through other channels (e.g., the Board of Education website) or have no
#   recent activity.
