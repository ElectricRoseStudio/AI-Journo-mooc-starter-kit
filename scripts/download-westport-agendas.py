#!/usr/bin/env python3
# download-westport-agendas.py
# Download municipal meeting agendas, minutes, and video recordings from
# Westport CT for meetings whose date falls within the past N days (and up
# to 7 days ahead, to catch agendas posted early for upcoming meetings).
#
# USAGE:
#   python3 scripts/download-westport-agendas.py [options]
#
# REQUIREMENTS:
#   - Python 3.8+
#   - pip install playwright playwright-stealth
#   - python3 -m playwright install chromium
#   - Internet connection
#
# WHAT IT DOES:
#   1. Fetches the Westport CT meeting list (past and upcoming pages)
#   2. Parses each event row for board name, date, agenda URL, minutes URL
#   3. Downloads Agenda and Minutes PDFs whose meeting date falls within
#      the date window to beat-archive/westport-agendas/YYYY-MM/
#   4. Optionally downloads video recordings from the CHAMP archive via
#      the CHAMP API (--include-video flag)
#   5. Appends a download log to beat-archive/westport-agendas/download-log.txt
#
# SITE STRUCTURE:
#   Documents (VisionLive CMS, powered by Granicus):
#     Meeting list: /about/advanced-components/meeting-list-calendar/-toggle-allpast/-npage-N
#                   /about/advanced-components/meeting-list-calendar/-toggle-allupcoming/-npage-N
#     Each page returns 20 events. Past events are newest-first; upcoming
#     are oldest-first. Pagination stops when all events on a page fall
#     outside the date window.
#     Each event row has columns: Event | Date/Time | Agenda | Minutes | Other
#     Document download: /home/showpublisheddocument/{doc_id}/{version_token}
#     The server requires a browser-like User-Agent and Referer header.
#
#   Videos (CHAMP Data Systems):
#     Player:     https://play.champds.com/westportct/archive/1
#     API root:   https://playapi.champds.com/westportct/
#     Archive:    GET /westportct/archive/1   → JSON with 7 ArchiveGroups
#     Groups:     Board of Selectmen (1), Board of Finance (2),
#                 Planning and Zoning Commission (3), Zoning Board of Appeals (4),
#                 Conservation Commission (5), Representative Town Meeting (6),
#                 Other Meetings and Events (7)
#     By date:    GET /westportct/archiveGroupDate/{ag_id}/LOCAL/{start}/{end}/
#                 → list of events; EventMediaClassID=2 means recorded video exists
#     Download:   https://play.champds.com/DOWNLOAD-MEDIA/westportct/eventmainmedia/{event_id}
#                 Returns MP4 directly (no authentication required)
#     Videos are typically 80 MB – 2 GB each depending on meeting length.

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

try:
    from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout
    from playwright_stealth import Stealth
except ImportError as _ie:
    print(
        f"ERROR: missing dependency: {_ie}\n"
        "  pip install playwright playwright-stealth\n"
        "  python3 -m playwright install chromium",
        file=sys.stderr,
    )
    sys.exit(1)

# --- Configuration ---
BASE_URL = "https://www.westportct.gov"
LIST_URL = f"{BASE_URL}/about/advanced-components/meeting-list-calendar"
OUTPUT_DIR = "beat-archive/westport-agendas"
DAYS_BACK = 4
DAYS_AHEAD = 7   # capture agendas posted early for upcoming meetings
DELAY_SECONDS = 1
PAGE_DELAY = 0.5  # delay between listing page fetches

# CHAMP video archive
CHAMP_API = "https://playapi.champds.com/westportct"
CHAMP_PLAY = "https://play.champds.com"
CHAMP_REFERER = "https://play.champds.com/westportct/archive/1"
# Archive groups: (CustomerArchiveGroupID, GroupName)
CHAMP_GROUPS = [
    (1, "Board of Selectmen"),
    (2, "Board of Finance"),
    (3, "Planning and Zoning Commission"),
    (4, "Zoning Board of Appeals"),
    (5, "Conservation Commission"),
    (6, "Representative Town Meeting"),
    (7, "Other Meetings and Events"),
]

UA = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)
VIDEO_DELAY = 0.5  # delay between CHAMP API requests
PAGE_TIMEOUT = 30_000  # ms


# --- HTTP helpers ---

def fetch_html(page, url):
    """Navigate to url with Playwright and return HTML, or None on error."""
    try:
        page.goto(url, wait_until="networkidle", timeout=PAGE_TIMEOUT)
        return page.content()
    except PWTimeout:
        return page.content()
    except Exception as e:
        print(f"  ERROR fetching {url}: {e}", file=sys.stderr)
        return None


def fetch_json(url):
    """GET url with CHAMP API headers; return parsed JSON or None on error."""
    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": UA,
            "Accept": "application/json, text/javascript, */*; q=0.01",
            "Referer": CHAMP_REFERER,
            "X-Requested-With": "XMLHttpRequest",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return json.loads(r.read().decode("utf-8", errors="replace"))
    except urllib.error.URLError as e:
        print(f"  ERROR fetching {url}: {e}", file=sys.stderr)
        return None
    except (json.JSONDecodeError, ValueError) as e:
        print(f"  ERROR parsing JSON from {url}: {e}", file=sys.stderr)
        return None


def download_file(ctx, path, dest_path):
    """Download a VisionLive PDF via the Playwright browser context (carries session cookies)."""
    url = BASE_URL + path if path.startswith("/") else path
    try:
        response = ctx.request.get(
            url,
            headers={"Referer": LIST_URL, "Accept": "application/pdf,*/*"},
            timeout=60_000,
        )
        if not response.ok:
            print(f"  WARNING: {url} returned HTTP {response.status}", file=sys.stderr)
            return False
        with open(dest_path, "wb") as f:
            f.write(response.body())
        return True
    except Exception as e:
        print(f"  WARNING: {e}", file=sys.stderr)
        return False


def download_champ_video(event_id, dest_path):
    """
    Download a CHAMP recorded video (MP4) via the DOWNLOAD-MEDIA endpoint.
    Returns True on success.
    """
    url = f"{CHAMP_PLAY}/DOWNLOAD-MEDIA/westportct/eventmainmedia/{event_id}"
    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": UA,
            "Accept": "*/*",
            "Referer": CHAMP_REFERER,
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=3600) as r:
            total = int(r.headers.get("Content-Length", 0))
            written = 0
            with open(dest_path, "wb") as f:
                while True:
                    chunk = r.read(1 << 20)  # 1 MB
                    if not chunk:
                        break
                    f.write(chunk)
                    written += len(chunk)
                    if total:
                        pct = written * 100 // total
                        print(f"\r  {pct}%  {written//(1<<20)} MB / {total//(1<<20)} MB  ", end="", flush=True)
            print()
        return True
    except Exception as e:
        print(f"\n  WARNING: {e}", file=sys.stderr)
        return False


# --- PDF parsing helpers (VisionLive) ---

def parse_events(html):
    """
    Parse event rows from a VisionLive meeting list page.
    Returns list of {board, date, agenda_path, minutes_path}.
    """
    results = []
    rows = re.findall(r"<tr[^>]*>.*?</tr>", html, re.DOTALL)

    for row in rows:
        if "event_datetime" not in row:
            continue

        name_m = re.search(r"itemprop=['\"]summary['\"]>([^<]+)<", row)
        if not name_m:
            continue
        board = name_m.group(1).strip()

        date_m = re.search(r"class=['\"]event_datetime['\"][^>]*>\s*(\d{1,2}/\d{1,2}/\d{4})", row)
        if not date_m:
            continue
        try:
            meeting_date = datetime.datetime.strptime(date_m.group(1), "%m/%d/%Y").date()
        except ValueError:
            continue

        agenda_cell = re.search(r"class=['\"]event_agenda['\"][^>]*>(.*?)</td>", row, re.DOTALL)
        agenda_path = None
        if agenda_cell:
            a = re.search(r"href=['\"](/home/showpublisheddocument/[^'\"]+)['\"]", agenda_cell.group(1))
            if a:
                agenda_path = a.group(1)

        minutes_cell = re.search(r"class=['\"]event_minutes['\"][^>]*>(.*?)</td>", row, re.DOTALL)
        minutes_path = None
        if minutes_cell:
            m = re.search(r"href=['\"](/home/showpublisheddocument/[^'\"]+)['\"]", minutes_cell.group(1))
            if m:
                minutes_path = m.group(1)

        results.append({
            "board": board,
            "date": meeting_date,
            "agenda_path": agenda_path,
            "minutes_path": minutes_path,
        })

    return results


def fetch_pages(pw_page, toggle, cutoff, future_limit, is_past):
    """
    Fetch paginated VisionLive meeting list pages via Playwright.
    Returns events whose date falls within [cutoff, future_limit].
    is_past=True → pages are newest-first; is_past=False → oldest-first.
    """
    results = []
    page_num = 1

    while True:
        if page_num == 1:
            url = f"{LIST_URL}/-toggle-{toggle}"
        else:
            url = f"{LIST_URL}/-toggle-{toggle}/-npage-{page_num}"

        html = fetch_html(pw_page, url)
        if not html:
            break

        events = parse_events(html)
        if not events:
            break

        dates = [e["date"] for e in events]

        for event in events:
            d = event["date"]
            if cutoff <= d <= future_limit:
                results.append(event)

        if is_past:
            if min(dates) < cutoff:
                break
        else:
            if max(dates) > future_limit:
                break

        page_num += 1
        time.sleep(PAGE_DELAY)

    return results


# --- CHAMP video helpers ---

def query_champ_events(ag_id, cutoff, future_limit):
    """
    Query CHAMP archiveGroupDate for events in [cutoff, future_limit].
    Returns a list of events with EventMediaClassID=2 (recorded video).
    """
    start = cutoff.strftime("%Y-%m-%dT00:00:00")
    end = future_limit.strftime("%Y-%m-%dT23:59:59")
    url = f"{CHAMP_API}/archiveGroupDate/{ag_id}/LOCAL/{start}/{end}/"
    data = fetch_json(url)
    if not data or not isinstance(data, list):
        return []
    return [e for e in data if e.get("EventMediaClassID") == 2]


# --- Utilities ---

def slugify(text, max_len=60):
    text = text.lower().strip()
    text = re.sub(r"[/\\&]", "-", text)
    text = re.sub(r"\s+-\s+", "-", text)
    text = re.sub(r"[^\w\s-]", "", text)
    text = re.sub(r"[\s_]+", "-", text)
    text = re.sub(r"-{2,}", "-", text)
    return text.strip("-")[:max_len]


def make_dest_path(board, doc_type, meeting_date, output_dir, suffix=""):
    date_prefix = meeting_date.strftime("%Y-%m-%d")
    month_dir = meeting_date.strftime("%Y-%m")
    board_slug = slugify(board, max_len=45)
    month_path = os.path.join(output_dir, month_dir)
    os.makedirs(month_path, exist_ok=True)
    fname = f"{date_prefix}-{board_slug}-{doc_type}{suffix}.pdf"
    return os.path.join(month_path, fname)


def make_video_dest_path(board, meeting_date, output_dir, suffix=""):
    date_prefix = meeting_date.strftime("%Y-%m-%d")
    month_dir = meeting_date.strftime("%Y-%m")
    board_slug = slugify(board, max_len=45)
    month_path = os.path.join(output_dir, month_dir)
    os.makedirs(month_path, exist_ok=True)
    fname = f"{date_prefix}-{board_slug}-video{suffix}.mp4"
    return os.path.join(month_path, fname)


# --- Main ---

def main():
    parser = argparse.ArgumentParser(
        description=(
            "Download Westport CT municipal agendas, minutes, and video recordings "
            "for meetings within the past N days."
        )
    )
    parser.add_argument(
        "--days", type=int, default=DAYS_BACK, metavar="N",
        help=f"Look back N days by meeting date (default: {DAYS_BACK})",
    )
    parser.add_argument(
        "--ahead", type=int, default=DAYS_AHEAD, metavar="N",
        help=f"Also include meetings up to N days ahead (default: {DAYS_AHEAD})",
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
        help="Only include boards whose name contains NAME (case-insensitive)",
    )
    parser.add_argument(
        "--include-video", action="store_true",
        help="Also download video recordings from the CHAMP archive",
    )
    parser.add_argument(
        "--docs-only", action="store_true",
        help="Download only PDFs; skip video even if --include-video is set",
    )
    parser.add_argument(
        "--show-browser", action="store_true",
        help="Run with a visible browser window (useful for debugging)",
    )
    args = parser.parse_args()

    now = datetime.datetime.now()
    if (now.weekday() == 5 and now.hour >= 18) or (now.weekday() == 6 and now.hour < 12):  # Saturday night, Sunday morning
        print("Skipping — no downloads on Saturday nights or Sunday mornings.")
        sys.exit(0)

    today = datetime.date.today()
    cutoff = today - datetime.timedelta(days=args.days)
    future_limit = today + datetime.timedelta(days=args.ahead)

    include_video = args.include_video and not args.docs_only

    print(f"Date window : {cutoff} to {future_limit}")
    print(f"Site        : {BASE_URL}")
    if not args.dry_run:
        print(f"Output dir  : {args.output_dir}")
    if include_video:
        print(f"Video       : CHAMP archive ({CHAMP_PLAY})")
    print()

    log_path = os.path.join(args.output_dir, "download-log.txt")
    log_lines: list = []
    downloaded = skipped = failed = 0

    with Stealth().use_sync(sync_playwright()) as pw:
        browser = pw.chromium.launch(headless=not args.show_browser)
        ctx = browser.new_context(user_agent=UA, locale="en-US")
        pw_page = ctx.new_page()

        # Warm up session so Cloudflare challenge cookies are set
        print("Establishing session with westportct.gov ...")
        fetch_html(pw_page, BASE_URL + "/")
        time.sleep(PAGE_DELAY)

        # ------------------------------------------------------------------ #
        # Part 1: PDFs from VisionLive meeting list                           #
        # ------------------------------------------------------------------ #

        print("Fetching past meeting pages...")
        past_events = fetch_pages(pw_page, "allpast", cutoff, future_limit, is_past=True)
        print(f"  Found {len(past_events)} past event(s) in window.")

        print("Fetching upcoming meeting pages...")
        upcoming_events = fetch_pages(pw_page, "allupcoming", cutoff, future_limit, is_past=False)
        print(f"  Found {len(upcoming_events)} upcoming event(s) in window.")
        print()

        # Merge and deduplicate by document path
        seen_paths = set()
        all_docs = []
        for event in past_events + upcoming_events:
            for doc_type, path_key in (("agenda", "agenda_path"), ("minutes", "minutes_path")):
                path = event.get(path_key)
                if path and path not in seen_paths:
                    seen_paths.add(path)
                    all_docs.append({
                        "board": event["board"],
                        "date": event["date"],
                        "doc_type": doc_type,
                        "path": path,
                    })

        if args.board:
            filter_name = args.board.lower()
            all_docs = [e for e in all_docs if filter_name in e["board"].lower()]

        # Detect duplicate (board, date, doctype) filenames and assign suffixes
        seen_keys: dict = {}
        for e in all_docs:
            key = (e["board"], e["date"], e["doc_type"])
            seen_keys[key] = seen_keys.get(key, 0) + 1
        key_counter: dict = {}
        for e in all_docs:
            key = (e["board"], e["date"], e["doc_type"])
            if seen_keys[key] > 1:
                key_counter[key] = key_counter.get(key, 0) + 1
                e["suffix"] = f"-{key_counter[key]}"
            else:
                e["suffix"] = ""

        all_docs.sort(key=lambda x: (x["date"], x["board"]), reverse=True)

        print(
            f"Found {len(all_docs)} PDF document(s) across "
            f"{len({e['board'] for e in all_docs})} board(s)."
        )

        # ------------------------------------------------------------------ #
        # Part 2: Videos from CHAMP archive                                   #
        # ------------------------------------------------------------------ #

        video_events = []
        if include_video or args.dry_run:
            print("\nQuerying CHAMP video archive...")
            for ag_id, group_name in CHAMP_GROUPS:
                if args.board and args.board.lower() not in group_name.lower():
                    continue
                evts = query_champ_events(ag_id, cutoff, future_limit)
                for e in evts:
                    dt_str = e.get("EventDateTimeLocal", e.get("EventDateTimeUTC", ""))[:10]
                    try:
                        meeting_date = datetime.date.fromisoformat(dt_str)
                    except ValueError:
                        continue
                    video_events.append({
                        "board": e.get("EventTitle") or group_name,
                        "group": group_name,
                        "date": meeting_date,
                        "event_id": e["CustomerEventID"],
                    })
                time.sleep(VIDEO_DELAY)

            print(f"  Found {len(video_events)} video recording(s) in window.")

        print()

        if not all_docs and not video_events:
            browser.close()
            return

        if args.dry_run:
            if all_docs:
                print(f"{'Board':<48} {'Date':<12} Type")
                print("-" * 72)
                for e in all_docs:
                    print(f"{e['board'][:47]:<48} {e['date']!s:<12} {e['doc_type']}")
            if video_events:
                print(f"\n{'Board':<48} {'Date':<12} Type")
                print("-" * 72)
                for e in video_events:
                    print(f"{e['board'][:47]:<48} {e['date']!s:<12} video (CHAMP #{e['event_id']})")
            total = len(all_docs) + len(video_events)
            print(f"\n{total} item(s). Re-run without --dry-run to download.")
            browser.close()
            return

        # ------------------------------------------------------------------ #
        # Download PDFs                                                        #
        # ------------------------------------------------------------------ #

        os.makedirs(args.output_dir, exist_ok=True)

        for e in all_docs:
            dest = make_dest_path(
                e["board"], e["doc_type"], e["date"],
                args.output_dir, suffix=e.get("suffix", ""),
            )
            label = os.path.basename(dest)

            if os.path.exists(dest):
                print(f"  skip (exists)  {label}")
                skipped += 1
                continue

            print(f"  [{e['date']}] {e['board']} — {e['doc_type']}")
            print(f"  downloading    {label}")

            if download_file(ctx, e["path"], dest):
                downloaded += 1
                log_lines.append(
                    f"{datetime.datetime.now().isoformat()}  OK       {dest}"
                )
            else:
                failed += 1
                log_lines.append(
                    f"{datetime.datetime.now().isoformat()}  FAILED   {BASE_URL + e['path']}"
                )
                if os.path.exists(dest):
                    os.remove(dest)

            time.sleep(DELAY_SECONDS)

        # ------------------------------------------------------------------ #
        # Download videos                                                      #
        # ------------------------------------------------------------------ #

        if include_video:
            vid_key_count: dict = {}
            for e in video_events:
                key = (e["board"], e["date"])
                vid_key_count[key] = vid_key_count.get(key, 0) + 1
            vid_key_counter: dict = {}
            for e in video_events:
                key = (e["board"], e["date"])
                if vid_key_count[key] > 1:
                    vid_key_counter[key] = vid_key_counter.get(key, 0) + 1
                    e["suffix"] = f"-{vid_key_counter[key]}"
                else:
                    e["suffix"] = ""

            for e in video_events:
                dest = make_video_dest_path(
                    e["board"], e["date"], args.output_dir, suffix=e.get("suffix", "")
                )
                label = os.path.basename(dest)

                if os.path.exists(dest):
                    print(f"  skip (exists)  {label}")
                    skipped += 1
                    continue

                size_url = f"{CHAMP_PLAY}/DOWNLOAD-MEDIA/westportct/eventmainmedia/{e['event_id']}"
                print(f"  [{e['date']}] {e['board']} — video (CHAMP #{e['event_id']})")
                print(f"  downloading    {label}")

                if download_champ_video(e["event_id"], dest):
                    downloaded += 1
                    log_lines.append(
                        f"{datetime.datetime.now().isoformat()}  OK       {dest}"
                    )
                else:
                    failed += 1
                    log_lines.append(
                        f"{datetime.datetime.now().isoformat()}  FAILED   {size_url}"
                    )
                    if os.path.exists(dest):
                        os.remove(dest)

        browser.close()

    if log_lines:
        with open(log_path, "a") as f:
            f.write("\n".join(log_lines) + "\n")

    print()
    print(f"Done — downloaded: {downloaded}  skipped: {skipped}  failed: {failed}")
    if downloaded + skipped:
        print(f"Files in: {args.output_dir}")
    if log_lines:
        print(f"Log:      {log_path}")


if __name__ == "__main__":
    main()


# --- Tips ---
#
# 1. Preview without downloading:
#    python3 scripts/download-westport-agendas.py --dry-run
#
# 2. Download docs + video recordings for the past 30 days:
#    python3 scripts/download-westport-agendas.py --include-video
#
# 3. Narrow to one board:
#    python3 scripts/download-westport-agendas.py --board "Board of Selectmen"
#
# 4. Change the lookback window:
#    python3 scripts/download-westport-agendas.py --days 7
#
# 5. Documents only (no video even if flag is passed):
#    python3 scripts/download-westport-agendas.py --docs-only
#
# 6. Save files somewhere else:
#    python3 scripts/download-westport-agendas.py --output-dir ~/Downloads/westport
#
# 7. Run on a schedule (cron — 8 AM daily):
#    0 8 * * * cd /path/to/repo && python3 scripts/download-westport-agendas.py
#
# 8. Run daily with video included:
#    0 8 * * * cd /path/to/repo && python3 scripts/download-westport-agendas.py --include-video
#
# 9. Process downloaded PDFs with Claude afterward:
#    python3 scripts/download-westport-agendas.py && bash scripts/batch-process.sh beat-archive/westport-agendas/
#
# NOTE: The --ahead flag (default: 7 days) captures agendas for upcoming meetings
# that have already been published. Run daily to stay current.
#
# NOTE: Westport CT uses VisionLive CMS (powered by Granicus) for meeting
# documents. Downloads require a browser-like User-Agent and Referer header;
# plain curl without these returns 403 Forbidden.
#
# NOTE: Video recordings are served by CHAMP Data Systems
# (play.champds.com/westportct). Only 6 boards are recorded: Board of
# Selectmen, Board of Finance, Planning and Zoning Commission, Zoning Board
# of Appeals, Conservation Commission, and Representative Town Meeting.
# The "Other Meetings and Events" group also appears occasionally.
# Videos are downloaded as MP4 files directly — no yt-dlp required.
# Files range from ~80 MB to ~2 GB depending on meeting length.
# Re-runs are safe; existing files are skipped.
#
# NOTE: The CHAMP archive's board names (EventTitle) may differ from the
# VisionLive board names. Both are preserved in the filenames so you can
# match them manually if needed.
