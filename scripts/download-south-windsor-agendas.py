#!/usr/bin/env python3
# download-south-windsor-agendas.py
# Download South Windsor, CT municipal meeting agendas, minutes, and Swagit
# recordings for meetings whose date falls within the past N days (and up to
# 7 days ahead).
#
# USAGE:
#   python3 scripts/download-south-windsor-agendas.py [options]
#
# REQUIREMENTS:
#   - Python 3.8+
#   - pip install playwright && playwright install chromium
#   - yt-dlp (pip3 install yt-dlp OR apt install yt-dlp) — for HLS recording downloads
#   - Internet connection
#   - A display (X11/Wayland) for non-headless mode; use xvfb-run on headless servers
#
# WHY NON-HEADLESS PLAYWRIGHT:
#   southwindsor-ct.gov is protected by Cloudflare bot detection.
#   A non-headless Chromium window passes the fingerprint check.
#   Use --headless to try headless mode (works on home/office IPs).
#
# WHAT IT DOES:
#   1. Fetches the Minutes & Agendas hub page to discover all 45 boards/commissions
#      with their Drupal node IDs
#   2. For each board, fetches the yearly agenda and minutes listing pages
#      (/node/{id}/agenda/{year} and /node/{id}/minutes/{year})
#   3. Parses each listing to find items in the date window
#   4. Downloads each matching PDF via cloudscraper (Cloudflare bypass required)
#   5. Scrapes gmedia.swagit.com to find meeting recordings in the date window
#   6. Downloads each matching recording via yt-dlp (HLS stream from Granicus CDN)
#   7. Appends a download log to beat-archive/south-windsor-agendas/download-log.txt
#
# SITE STRUCTURE:
#   CMS: Drupal 7 (CivicPlus theme) at southwindsor-ct.gov
#        Protected by Cloudflare bot detection — requires cloudscraper.
#
#   Hub page: https://www.southwindsor-ct.gov/minutes-and-agendas
#     Contains links of the form:
#       href="/node/{id}/agenda"  → board name in link text
#     One link per board; node 276 appears twice (generic then "Town Council"),
#     later occurrence wins.
#
#   Board listing pages:
#     https://www.southwindsor-ct.gov/node/{id}/agenda/{year}
#     https://www.southwindsor-ct.gov/node/{id}/minutes/{year}
#     Structure: <div class="views-row ..."> blocks, each containing:
#       - Text: "{title} {Month} {DD}, {YYYY} [- H:MMpm]"  (date in full month name)
#       - href: "/{board-slug}/agenda/{document-slug}" or "/{board-slug}/minutes/{...}"
#     href links directly serve the PDF file (Drupal file download handler).
#
#   Document download:
#     GET https://www.southwindsor-ct.gov{path-from-listing}
#     Returns application/pdf directly (no redirect). Requires Cloudflare bypass.
#
#   Recordings (Swagit / Granicus):
#     Listing: http://gmedia.swagit.com/  (paginated, no auth, no Cloudflare)
#     Structure per video block:
#       <div class="ratio" style="background-image: url('{thumb_url}');"
#            onclick="location.href='{video_url}';">
#       <h4>{Board Name} - {Month} {DD}, {YYYY}</h4>
#     Thumbnail URL: https://swagit-video.granicus.com/southwindsorct/{uuid}.10pct.jpg
#     Video URL:     https://southwindsorct.new.swagit.com/videos/{id}
#     HLS stream (derived from thumbnail UUID):
#       https://archive-stream.granicus.com/OnDemand/_definst_/
#         mp4:swagitVideo/southwindsorct/{uuid}.mp4/playlist.m3u8
#     yt-dlp downloads the HLS stream from the new.swagit.com video page URL.
#
# BOARDS (45, as of 2026-05):
#   Accessibility Advisory Committee, Agricultural Commission,
#   Agriculture Arts & Nature Sub-Committee, Architectural & Design Review
#   Committee, Audit Committee, Board of Assessment Appeals,
#   Board of Fire Commissioners, Capital Projects Committee,
#   Charter Revision Commission, Crumbling Foundations Committee,
#   Demolition Delay Committee, Economic Development Commission,
#   Energy Committee, Ethics Committee, Historic District Commission,
#   Housing and Fair Rent Commission, Human Relations Commission,
#   Inland Wetlands Agency / Conservation Commission,
#   Insurance Control Commission,
#   Juvenile Firesetter Intervention and Prevention Commission,
#   Library Board of Directors,
#   Mass Transit & Highway Advisory Commission,
#   Naming of Public Lands and Buildings (Subcommittee),
#   Open Space Task Force, Park & Recreation Commission,
#   Park and Recreation Facility Planning and Implementation Committee,
#   Patriotic Commission, Pension Committee, Personnel Board of Appeals,
#   Personnel Committee, Planning and Zoning Commission,
#   Public Building Commission, Redevelopment Agency, Senior Advisory Board,
#   Social Justice & Racial Equity Commission,
#   South Windsor Alliance for Families, South Windsor Arts Commission,
#   South Windsor Walk and Wheel Ways Subcommittee,
#   Strategic Planning Committee, Sustainable Connecticut,
#   Town Council, Town Council Rules & Procedures Committee,
#   Transparency Task Force, Water Pollution Control Authority,
#   Zoning Board of Appeals

import argparse
import datetime
import html as html_module
import os
import re
import subprocess
import sys
import time
import urllib.request
import urllib.error

try:
    from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout
except ImportError:
    sys.exit(
        "ERROR: playwright is required.\n"
        "Install with: pip install playwright && playwright install chromium"
    )

# --- Configuration ---
BASE_URL = "https://www.southwindsor-ct.gov"
HUB_URL = f"{BASE_URL}/minutes-and-agendas"
SWAGIT_BASE = "http://gmedia.swagit.com"
SWAGIT_NEW = "https://southwindsorct.new.swagit.com"
OUTPUT_DIR = "beat-archive/south-windsor-agendas"
DAYS_BACK = 4
DAYS_AHEAD = 7
DELAY_SECONDS = 0.5   # between requests to southwindsor-ct.gov

UA = "Mozilla/5.0 (X11; Linux x86_64; rv:120.0) Gecko/20100101 Firefox/120.0"

# Date regex for Drupal listing rows ("Month DD, YYYY" with full month names)
_LISTING_DATE_RE = re.compile(
    r"(January|February|March|April|May|June|July|August|September|"
    r"October|November|December)\s+(\d{1,2}),\s+(20\d{2})",
)

# Date regex for Swagit titles ("Board - Mon DD, YYYY" — full or 3-letter months)
_SWAGIT_TITLE_RE = re.compile(
    r"^(.+?)\s*-\s*"
    r"((?:Jan(?:uary)?|Feb(?:ruary)?|Mar(?:ch)?|Apr(?:il)?|May|Jun(?:e)?|"
    r"Jul(?:y)?|Aug(?:ust)?|Sep(?:tember)?|Oct(?:ober)?|Nov(?:ember)?|"
    r"Dec(?:ember)?)\s+\d{1,2},\s+20\d{2})$",
    re.IGNORECASE,
)


# --- Utilities ---

def slugify(text, max_len=55):
    text = str(text).lower().strip()
    text = re.sub(r"[/\\&]", "-", text)
    text = re.sub(r"[^\w\s-]", "", text)
    text = re.sub(r"[\s_]+", "-", text)
    text = re.sub(r"-{2,}", "-", text)
    return text.strip("-")[:max_len]


def fetch_html(page, url, timeout_ms=20000):
    """Navigate to URL and return page HTML, or raise on non-200."""
    response = page.goto(url, timeout=timeout_ms, wait_until="domcontentloaded")
    if response is None or response.status != 200:
        status = response.status if response else "no response"
        raise RuntimeError(f"HTTP {status} for {url}")
    return page.content()


def parse_meeting_date(text):
    """Extract a datetime.date from listing row text, or None."""
    m = _LISTING_DATE_RE.search(text)
    if not m:
        return None
    date_str = f"{m.group(1)} {m.group(2)}, {m.group(3)}"
    try:
        return datetime.datetime.strptime(date_str, "%B %d, %Y").date()
    except ValueError:
        return None


def parse_swagit_date(date_str):
    """Parse a Swagit title date string. Returns datetime.date or None."""
    for fmt in ("%B %d, %Y", "%b %d, %Y"):
        try:
            return datetime.datetime.strptime(date_str.strip(), fmt).date()
        except ValueError:
            pass
    return None


# --- Data fetching ---

def fetch_board_list(page):
    """
    Return {node_id: board_name} from the hub page.
    Iterates all agenda links; last occurrence of a node ID wins, so
    node 276 resolves to 'Town Council' rather than the generic 'Meeting Agendas'.
    """
    html = fetch_html(page, HUB_URL)
    boards = {}
    for m in re.finditer(
        r'href="(?:' + re.escape(BASE_URL) + r')?/node/(\d+)/agenda"[^>]*>([^<]+)</a>',
        html,
    ):
        node_id = m.group(1)
        name = html_module.unescape(m.group(2).strip())
        boards[node_id] = name  # later occurrences overwrite — keeps most-specific name
    return boards


def fetch_listing_rows(page, node_id, doc_type, year):
    """
    Fetch /node/{node_id}/{doc_type}/{year} and return list of
    {date, path, row_text}.  Returns [] on any error.
    """
    url = f"{BASE_URL}/node/{node_id}/{doc_type}/{year}"
    try:
        html = fetch_html(page, url)
    except Exception as e:
        print(f"  WARNING: could not fetch {url}: {e}", file=sys.stderr)
        return []

    rows = []
    for row_m in re.finditer(
        r'<div class="views-row[^"]*"[^>]*>(.*?)</div>\s*</div>',
        html, re.DOTALL,
    ):
        row_html = row_m.group(1)
        link_m = re.search(r'href="(/[^"]+)"', row_html)
        if not link_m:
            continue
        path = link_m.group(1)

        text = re.sub(r"<[^>]+>", " ", row_html)
        text = re.sub(r"\s+", " ", text).strip()

        date = parse_meeting_date(text)
        if not date:
            continue

        rows.append({"date": date, "path": path, "text": text})
    return rows


def fetch_swagit_page(page_num):
    """
    Fetch a page from the old Swagit listing (gmedia.swagit.com).
    Returns (videos, has_next_page) where videos is a list of
    {video_id, board, date, thumb_uuid, new_url, title}.
    """
    url = SWAGIT_BASE + (f"/?page={page_num}" if page_num > 1 else "/")
    try:
        req = urllib.request.Request(url, headers={"User-Agent": UA})
        with urllib.request.urlopen(req, timeout=20) as resp:
            html = resp.read().decode("utf-8", errors="replace")
    except Exception as e:
        print(f"  WARNING: Swagit fetch failed for page {page_num}: {e}", file=sys.stderr)
        return [], False

    has_next = f"page={page_num + 1}" in html

    videos = []
    # Each video block is a <li class="span4"> containing a ratio div and caption div.
    # ratio div:   style="background-image: url('{thumb}');" onclick="location.href='{url}';"
    # caption div: <h4>{Board Name} - {Date}</h4>
    # Extract each component separately from the <li> block for robustness.
    for block_m in re.finditer(
        r'<li class="span4">(.*?)</li>',
        html, re.DOTALL,
    ):
        block = block_m.group(1)

        thumb_m = re.search(r"background-image:\s*url\('([^']+)'\)", block)
        url_m = re.search(r'href="(https://southwindsorct\.new\.swagit\.com/videos/\d+)"', block)
        h4_m = re.search(r"<h4>([^<]+)</h4>", block)
        if not (thumb_m and url_m and h4_m):
            continue

        thumb_url = thumb_m.group(1)
        video_url = url_m.group(1).strip()
        title_text = html_module.unescape(h4_m.group(1).strip())

        vid_m = re.search(r"/videos/(\d+)", video_url)
        if not vid_m:
            continue
        video_id = vid_m.group(1)

        uuid_m = re.search(
            r"/([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})\.",
            thumb_url,
        )
        if not uuid_m:
            continue
        uuid = uuid_m.group(1)

        title_m = _SWAGIT_TITLE_RE.match(title_text)
        if not title_m:
            continue
        board_name = title_m.group(1).strip()
        vid_date = parse_swagit_date(title_m.group(2))
        if not vid_date:
            continue

        videos.append({
            "video_id": video_id,
            "board": board_name,
            "date": vid_date,
            "thumb_uuid": uuid,
            "new_url": video_url,
            "title": title_text,
        })
    return videos, has_next


def collect_swagit_videos(cutoff, future_limit):
    """
    Paginate through gmedia.swagit.com to collect recordings in the date window.
    Stops once all videos on a page predate the cutoff (videos are newest-first).
    """
    results = []
    seen_ids = set()

    for page_num in range(1, 20):   # safety cap
        videos, has_next = fetch_swagit_page(page_num)
        if not videos:
            break

        all_older_than_cutoff = True
        for v in videos:
            if v["video_id"] in seen_ids:
                continue
            seen_ids.add(v["video_id"])
            if v["date"] >= cutoff:
                all_older_than_cutoff = False
            if cutoff <= v["date"] <= future_limit:
                results.append(v)

        if all_older_than_cutoff or not has_next:
            break

        time.sleep(DELAY_SECONDS)

    return results


# --- Path helpers ---

def make_doc_path(board, doc_type, meeting_date, url_path, output_dir):
    """Return the local file path for a downloaded document."""
    date_str = meeting_date.strftime("%Y-%m-%d")
    month_dir = os.path.join(output_dir, meeting_date.strftime("%Y-%m"))
    board_slug = slugify(board, max_len=35)
    # Use the last path component as the document slug (already descriptive)
    doc_slug = url_path.rstrip("/").split("/")[-1][:50]
    fname = f"{date_str}-{board_slug}-{doc_type}-{doc_slug}.pdf"
    return os.path.join(month_dir, fname)


def make_recording_path(board, meeting_date, video_id, output_dir, ext="mp4"):
    """Return the output template for a yt-dlp download."""
    date_str = meeting_date.strftime("%Y-%m-%d")
    month_dir = os.path.join(output_dir, meeting_date.strftime("%Y-%m"))
    board_slug = slugify(board, max_len=40)
    fname = f"{date_str}-{board_slug}-{video_id}.%(ext)s"
    return os.path.join(month_dir, fname)


# --- Archive helpers ---

def is_in_archive(archive_path, video_id):
    if not os.path.exists(archive_path):
        return False
    needle = str(video_id)
    with open(archive_path) as f:
        return any(needle == line.strip() for line in f)


def add_to_archive(archive_path, video_id):
    with open(archive_path, "a") as f:
        f.write(f"{video_id}\n")


# --- Main ---

def main():
    parser = argparse.ArgumentParser(
        description=(
            "Download South Windsor CT municipal agendas, minutes, and Swagit "
            "recordings for meetings within the past N days (and up to 7 ahead)."
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
        help="List matching items without downloading",
    )
    parser.add_argument(
        "--board", metavar="NAME",
        help="Only include boards whose name contains NAME (case-insensitive)",
    )
    parser.add_argument(
        "--no-minutes", action="store_true",
        help="Skip minutes, download agendas only",
    )
    parser.add_argument(
        "--no-agendas", action="store_true",
        help="Skip agendas, download minutes only",
    )
    parser.add_argument(
        "--docs-only", action="store_true",
        help="Download PDFs only, skip recordings",
    )
    parser.add_argument(
        "--recordings-only", action="store_true",
        help="Download recordings only, skip PDFs",
    )
    parser.add_argument(
        "--headless", action="store_true",
        help="Run Chromium in headless mode (may be blocked by Cloudflare on server IPs)",
    )
    args = parser.parse_args()

    if datetime.date.today().weekday() in (6, 0):  # Sunday, Monday
        print("Skipping — no downloads on Sunday or Monday.")
        sys.exit(0)

    today = datetime.date.today()
    cutoff = today - datetime.timedelta(days=args.days)
    future_limit = today + datetime.timedelta(days=args.ahead)
    board_filter = args.board.lower() if args.board else None

    print(f"Date window : {cutoff} to {future_limit}")
    print(f"Hub page    : {HUB_URL}")
    if not args.dry_run:
        print(f"Output dir  : {args.output_dir}")
    print()

    # --- Launch browser ---
    pw_instance = sync_playwright().start()
    browser = pw_instance.chromium.launch(headless=args.headless)
    context = browser.new_context(
        user_agent=(
            "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
        )
    )
    page = context.new_page()

    # --- Fetch board list ---
    print("Fetching board list from hub page...")
    try:
        boards = fetch_board_list(page)
    except Exception as e:
        print(f"FATAL: Could not fetch board list: {e}", file=sys.stderr)
        browser.close()
        pw_instance.stop()
        sys.exit(1)
    print(f"  Found {len(boards)} board(s).\n")

    if board_filter:
        boards = {nid: name for nid, name in boards.items() if board_filter in name.lower()}
        print(f"  Board filter '{args.board}' → {len(boards)} board(s).\n")

    # --- Collect documents ---
    all_docs = []   # {board, date, path, doc_type, row_text}

    if not args.recordings_only:
        years_to_check = sorted({cutoff.year, future_limit.year})
        doc_types = []
        if not args.no_agendas:
            doc_types.append("agenda")
        if not args.no_minutes:
            doc_types.append("minutes")

        print(f"Scanning {len(boards)} board(s) × {len(doc_types)} doc type(s) × {len(years_to_check)} year(s)...")
        for node_id, board_name in boards.items():
            for doc_type in doc_types:
                for year in years_to_check:
                    rows = fetch_listing_rows(page, node_id, doc_type, year)
                    for row in rows:
                        if cutoff <= row["date"] <= future_limit:
                            all_docs.append({
                                "board": board_name,
                                "date": row["date"],
                                "path": row["path"],
                                "doc_type": doc_type,
                                "row_text": row["text"],
                            })
                    time.sleep(DELAY_SECONDS)

        print(f"  Found {len(all_docs)} document(s) in window.\n")

    # --- Collect recordings ---
    all_recordings = []

    if not args.docs_only:
        print("Fetching Swagit recording listings...")
        all_recordings = collect_swagit_videos(cutoff, future_limit)
        if board_filter:
            all_recordings = [v for v in all_recordings if board_filter in v["board"].lower()]
        print(f"  Found {len(all_recordings)} recording(s) in window.\n")

    if not all_docs and not all_recordings:
        print("No items found in the date window.")
        return

    # Sort by date desc
    all_docs.sort(key=lambda x: (x["date"], x["board"]), reverse=True)
    all_recordings.sort(key=lambda x: (x["date"], x["board"]), reverse=True)

    # --- Dry-run listing ---
    if args.dry_run:
        if all_docs:
            print(f"{'Board':<42} {'Date':<12} {'Type':<8} Document")
            print("-" * 100)
            for d in all_docs:
                doc_slug = d["path"].rstrip("/").split("/")[-1][:45]
                print(
                    f"{d['board'][:41]:<42} "
                    f"{d['date']!s:<12} "
                    f"{d['doc_type']:<8} "
                    f"{doc_slug}"
                )
            print()

        if all_recordings:
            print(f"{'Board':<42} {'Date':<12} {'Video ID':<10} Title")
            print("-" * 100)
            for v in all_recordings:
                print(
                    f"{v['board'][:41]:<42} "
                    f"{v['date']!s:<12} "
                    f"{v['video_id']:<10} "
                    f"{v['title'][:40]}"
                )
            print()

        total = len(all_docs) + len(all_recordings)
        print(f"{total} item(s) matched. Re-run without --dry-run to download.")
        return

    # --- Download PDFs ---
    os.makedirs(args.output_dir, exist_ok=True)
    log_path = os.path.join(args.output_dir, "download-log.txt")
    log_lines = []
    downloaded = skipped = failed = 0

    if all_docs:
        print(f"Downloading {len(all_docs)} document(s)...")
        for d in all_docs:
            month_dir = os.path.join(args.output_dir, d["date"].strftime("%Y-%m"))
            dest = make_doc_path(d["board"], d["doc_type"], d["date"], d["path"], args.output_dir)
            label = os.path.basename(dest)

            if os.path.exists(dest):
                print(f"  skip (exists)  {label}")
                skipped += 1
                continue

            os.makedirs(month_dir, exist_ok=True)
            full_url = BASE_URL + d["path"]
            print(f"  [{d['date']}] {d['board'][:45]} — {d['doc_type']}")
            print(f"  downloading    {label}")

            try:
                response = page.goto(full_url, timeout=60000, wait_until="domcontentloaded")
                if response is None or response.status not in (200, 206):
                    status = response.status if response else "no response"
                    print(f"  WARNING: HTTP {status} for {full_url}", file=sys.stderr)
                    failed += 1
                    log_lines.append(
                        f"{datetime.datetime.now().isoformat()}  FAILED   {full_url}"
                    )
                    continue
                ct = response.headers.get("content-type", "")
                if "pdf" not in ct.lower() and "octet-stream" not in ct.lower():
                    print(f"  WARNING: unexpected content-type {ct!r}, skipping", file=sys.stderr)
                    failed += 1
                    continue
                with open(dest, "wb") as f:
                    f.write(response.body())
                downloaded += 1
                log_lines.append(
                    f"{datetime.datetime.now().isoformat()}  OK       {dest}"
                )
            except Exception as e:
                print(f"  WARNING: {e}", file=sys.stderr)
                failed += 1
                log_lines.append(
                    f"{datetime.datetime.now().isoformat()}  FAILED   {full_url}"
                )
                if os.path.exists(dest):
                    os.remove(dest)

            time.sleep(DELAY_SECONDS)
        print()

    # --- Download recordings via yt-dlp ---
    if all_recordings:
        archive_path = os.path.join(args.output_dir, "media-archive.txt")
        print(f"Downloading {len(all_recordings)} recording(s) via yt-dlp...")

        for v in all_recordings:
            video_id = v["video_id"]
            if is_in_archive(archive_path, video_id):
                print(f"  skip (archive) video {video_id}  {v['board'][:45]}")
                skipped += 1
                continue

            month_dir = os.path.join(args.output_dir, v["date"].strftime("%Y-%m"))
            os.makedirs(month_dir, exist_ok=True)
            outtmpl = make_recording_path(v["board"], v["date"], video_id, args.output_dir)

            print(f"  [{v['date']}] {v['board'][:45]} (video {video_id})")
            print(f"  downloading    {os.path.basename(outtmpl.replace('%(ext)s', 'mp4'))}")

            cmd = [
                "yt-dlp",
                "--no-playlist",
                "--quiet",
                "--no-warnings",
                "-o", outtmpl,
                v["new_url"],
            ]
            result = subprocess.run(cmd, capture_output=True, text=True)
            if result.returncode == 0:
                downloaded += 1
                add_to_archive(archive_path, video_id)
                log_lines.append(
                    f"{datetime.datetime.now().isoformat()}  OK       video {video_id} {v['board']}"
                )
            else:
                failed += 1
                err = result.stderr.strip()[:120] if result.stderr else "unknown"
                print(f"  WARNING: yt-dlp failed: {err}", file=sys.stderr)
                log_lines.append(
                    f"{datetime.datetime.now().isoformat()}  FAILED   video {video_id} {v['new_url']}"
                )
        print()

    if log_lines:
        with open(log_path, "a") as f:
            f.write("\n".join(log_lines) + "\n")

    browser.close()
    pw_instance.stop()

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
#    python3 scripts/download-south-windsor-agendas.py --dry-run
#
# 2. Narrow to one board:
#    python3 scripts/download-south-windsor-agendas.py --board "Planning and Zoning"
#
# 3. PDFs only (no recording downloads):
#    python3 scripts/download-south-windsor-agendas.py --docs-only
#
# 4. Recordings only:
#    python3 scripts/download-south-windsor-agendas.py --recordings-only
#
# 5. Agendas only (skip minutes):
#    python3 scripts/download-south-windsor-agendas.py --no-minutes
#
# 6. Change the lookback window:
#    python3 scripts/download-south-windsor-agendas.py --days 14
#
# 7. Run on a schedule (cron — 7 AM daily):
#    0 7 * * * cd /path/to/repo && python3 scripts/download-south-windsor-agendas.py
#
# NOTES:
#   - The main site southwindsor-ct.gov is protected by Cloudflare bot detection.
#     A non-headless Playwright/Chromium session bypasses this by presenting a
#     real browser fingerprint. If running on a server without a display, use
#     xvfb-run or pass --headless (headless works on home/office IPs).
#   - Node 276 appears twice on the hub page: once as "Meeting Agendas" (a generic
#     hub link) and once as "Town Council" (the board-specific link). The script
#     uses the last occurrence, giving "Town Council".
#   - Each Drupal listing page covers one year. The script fetches the current
#     year and, if the lookback window crosses a year boundary, also the prior year.
#   - Individual document URLs (/{board-slug}/agenda/{doc-slug}) serve PDFs
#     directly via Drupal's file download handler.
#   - Meeting recordings are hosted on Swagit/Granicus. The old site
#     (gmedia.swagit.com) provides a paginated HTML listing without authentication.
#     The HLS stream is at:
#       archive-stream.granicus.com/OnDemand/_definst_/mp4:swagitVideo/
#         southwindsorct/{uuid}.mp4/playlist.m3u8
#     yt-dlp is used to download from the new.swagit.com video page URL, which
#     resolves to the same HLS stream automatically.
#   - A media-archive.txt file tracks downloaded recordings by Swagit video ID
#     so they are not re-downloaded on subsequent runs.
