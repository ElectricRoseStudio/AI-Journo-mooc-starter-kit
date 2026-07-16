#!/usr/bin/env python3
# download-stamford-agendas.py
# Download Stamford CT municipal meeting agendas, minutes, and video recordings.
#
# USAGE:
#   python3 scripts/download-stamford-agendas.py [options]
#
# REQUIREMENTS:
#   - Python 3.6+  (no third-party packages needed for docs)
#   - yt-dlp       (for video: pip install yt-dlp or brew install yt-dlp)
#   - Internet connection
#
# WHAT IT DOES:
#   Documents (--docs-only or default):
#     1. Fetches each board/commission page on stamfordct.gov (Granicus GovAccess CMS)
#     2. Finds year-based document folders for Agendas and Minutes sections
#     3. Parses meeting dates from document titles (multiple patterns)
#     4. Downloads PDFs within the date window to beat-archive/stamford-agendas/YYYY-MM/
#
#   Video (--include-video or --video-only):
#     5. Scrapes each board's Granicus ViewPublisher page
#        (cityofstamford.granicus.com/ViewPublisher.php?view_id=N)
#     6. Filters clips by Unix timestamp date
#     7. Downloads recordings with yt-dlp
#
# SITE STRUCTURE:
#   Board page:   https://www.stamfordct.gov/government/boards-commissions/{board}
#   Doc folder:   https://www.stamfordct.gov/government/boards-commissions/{board}/-folder-{id}
#   Document URL: https://www.stamfordct.gov/home/showpublisheddocument/{docId}/{netTicks}
#   Video view:   https://cityofstamford.granicus.com/ViewPublisher.php?view_id={N}
#   Video clip:   https://cityofstamford.granicus.com/MediaPlayer.php?view_id={N}&clip_id={M}
#
# NOTE: The stamfordct.gov site is behind Akamai CDN which blocks plain urllib
# requests. Full browser-like headers (User-Agent, Sec-Fetch-*) are required.
# Accept-Encoding is omitted to avoid brotli/gzip decompression complexity.
#
# NOTE: Document dates are parsed from titles. Multiple formats are handled:
#   "Month DD, YYYY"  (e.g., "PB Regular Meeting - May 19, 2026")
#   "MMDDYYYY"        (e.g., "05192026 BOF Special Meeting")
#   "MM/DD/YYYY"      (e.g., "BOE Meeting 05/19/2026")
#   "YYYY-MM-DD"      (ISO format)
# Fallback: .NET DateTime ticks embedded in the document URL are decoded.
#
# NOTE: Not all boards post documents on their GovAccess page. Some use separate
# subpages or external sites. Boards without discoverable year folders are skipped.

import argparse
import datetime
import gzip
import html as html_module
import os
import re
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

YT_DLP_NODE = "node:/home/richkirby/.local/bin/yt-dlp-node"  # yt-dlp needs Node 22+; symlink kept current by scripts/update-yt-dlp-node.sh

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

BASE_URL     = "https://www.stamfordct.gov"
GRANICUS_URL = "https://cityofstamford.granicus.com"
OUTPUT_DIR   = "beat-archive/stamford-agendas"
DAYS_BACK    = 4
DAYS_AHEAD   = 7
DELAY_SECONDS = 0.5

# Full browser headers to bypass Akamai CDN — required by stamfordct.gov
FETCH_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
    "Sec-Fetch-Dest": "document",
    "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-Site": "none",
}

# Granicus view IDs → board/commission name
GRANICUS_VIEWS = {
    3:  "Board of Education",
    4:  "Board of Finance",
    5:  "OPEB Trustees",
    6:  "Health Commission",
    7:  "Animal Control Task Force",
    8:  "Zoning Board",
    9:  "Parks & Recreation Commission",
    10: "Harbor Management Commission",
    11: "Traffic Advisory Committee",
    12: "Transit District",
    14: "Board of Representatives",
    15: "Water Pollution Control Authority",
    17: "Environmental Protection Board",
    18: "Historic Preservation Advisory Commission",
    19: "Zoning Board of Appeals",
    20: "Planning Board",
    21: "Camera Review Committee",
    22: "Fire Commission",
    23: "Police Commission",
    24: "Social Services Commission",
    25: "Board of Ethics",
    26: "Personnel Commission",
    28: "CERF",
    29: "Custodian & Mechanic Pension",
}

# Board/commission pages on stamfordct.gov for document scraping.
# Format: (display_name, page_path)
BOARD_PAGES = [
    ("ADA Advisory Council",              "/government/boards-commissions/stamford-ada-advisory-council"),
    ("Affordable Housing Trust Fund",     "/government/boards-commissions/affordable-housing-trust-fund"),
    ("Animal Control Task Force",         "/government/boards-commissions/animal-control-task-force"),
    ("Appointments Commission",           "/government/boards-commissions/appointments-commission"),
    ("Arts & Culture Commission",         "/government/boards-commissions/arts-culture-commission"),
    ("Board of Assessment Appeals",       "/government/boards-commissions/board-of-assessment-appeals"),
    ("Board of Education",                "/government/boards-commissions/board-of-education"),
    ("Board of Ethics",                   "/government/boards-commissions/board-of-ethics"),
    ("Board of Finance",                  "/government/boards-commissions/board-of-finance"),
    ("Board of Representatives",          "/government/boards-commissions/board-of-representatives"),
    ("Camera Review Committee",           "/government/boards-commissions/camera-review-committee"),
    ("CERF",                              "/government/boards-commissions/classified-employees-retirement-fund"),
    ("Custodian & Mechanic Pension",      "/government/boards-commissions/custodian-and-mechanic-s-retirement-fund"),
    ("Economic Development Commission",   "/government/boards-commissions/economic-development-commission"),
    ("Enterprise Zone Board",             "/government/boards-commissions/enterprise-zone-board"),
    ("Environmental Protection Board",    "/government/operations/environmental-protection-board"),
    ("Fire Commission",                   "/government/boards-commissions/fire-commission"),
    ("Firefighters Pension Fund",         "/government/boards-commissions/firefighters-pension-fund"),
    ("Golf Commission",                   "/government/boards-commissions/golf-commission-e-gaynor-brennan"),
    ("Harbor Management Commission",      "/government/boards-commissions/harbor-management-commission"),
    ("Health Commission",                 "/government/boards-commissions/health-commission"),
    ("Historic Preservation",             "/government/boards-commissions/historic-preservation-advisory-commission"),
    ("Investment Advisory Committee",     "/government/boards-commissions/investment-advisory-committee"),
    ("OPEB Board of Trustees",            "/government/boards-commissions/opeb-board-of-trustees"),
    ("Parks & Recreation Commission",     "/government/boards-commissions/parks-recreation-commission"),
    ("Personnel Commission",              "/government/boards-commissions/personnel-commission"),
    ("Planning Board",                    "/government/boards-commissions/planning-board"),
    ("Police Commission",                 "/government/boards-commissions/police-commission"),
    ("Police Pension Board",              "/government/boards-commissions/police-pension-board"),
    ("Social Services Commission",        "/government/boards-commissions/social-services-commission"),
    ("Stamford Asset Management Group",   "/government/boards-commissions/stamford-asset-management-group"),
    ("Stamford Transit District",         "/government/boards-commissions/stamford-transit-district"),
    ("Tax Abatement Committee",           "/government/boards-commissions/tax-abatement-committee"),
    ("Terry Conners Rink Committee",      "/government/boards-commissions/terry-conners-rink-committee"),
    ("Traffic Advisory Committee",        "/government/boards-commissions/traffic-advisory-committee"),
    ("Urban Redevelopment Commission",    "/government/boards-commissions/urban-redevelopment-commission"),
    ("WPCA Board",                        "/government/boards-commissions/wpca-board"),
    ("Water Pollution Control Authority", "/government/operations/water-pollution-control-authority"),
    ("Zoning Board",                      "/government/boards-commissions/zoning-board"),
    ("Zoning Board of Appeals",           "/government/boards-commissions/zoning-board-of-appeals"),
]

# ---------------------------------------------------------------------------
# Date parsing
# ---------------------------------------------------------------------------

# .NET DateTime ticks: 100-nanosecond intervals since Jan 1, 0001 UTC
_NET_EPOCH_TICKS = 621355968000000000  # .NET ticks at Unix epoch (Jan 1, 1970)

MONTHS = {
    "january": 1, "february": 2, "march": 3, "april": 4,
    "may": 5, "june": 6, "july": 7, "august": 8,
    "september": 9, "october": 10, "november": 11, "december": 12,
    "jan": 1, "feb": 2, "mar": 3, "apr": 4,
    "jun": 6, "jul": 7, "aug": 8, "sep": 9, "sept": 9,
    "oct": 10, "nov": 11, "dec": 12,
}

_DATE_MONTH_RE = re.compile(
    r"\b(January|February|March|April|May|June|July|August|September|"
    r"October|November|December|Jan|Feb|Mar|Apr|Jun|Jul|Aug|Sep|Sept|Oct|Nov|Dec)"
    r"\.?\s+(\d{1,2}),?\s+(20\d{2})\b",
    re.I,
)
_DATE_SLASH_RE  = re.compile(r"\b(\d{1,2})[/\-](\d{1,2})[/\-](20\d{2})\b")
_DATE_ISO_RE    = re.compile(r"\b(20\d{2})[/\-](\d{2})[/\-](\d{2})\b")
_DATE_8DIG_RE   = re.compile(r"(?<!\d)(\d{2})(\d{2})(20\d{2})(?!\d)")
_NET_TICKS_RE   = re.compile(r"/showpublisheddocument/\d+/(\d{15,19})(?:[/?#]|$)")


def _safe_date(y, m, d):
    try:
        return datetime.date(int(y), int(m), int(d))
    except ValueError:
        return None


def parse_doc_date(title, url_path=""):
    """Extract meeting date from document title; fall back to .NET URL ticks."""
    # Month DD, YYYY
    m = _DATE_MONTH_RE.search(title)
    if m:
        mo = MONTHS.get(m.group(1).lower())
        if mo:
            d = _safe_date(m.group(3), mo, m.group(2))
            if d:
                return d

    # MM/DD/YYYY or MM-DD-YYYY
    m = _DATE_SLASH_RE.search(title)
    if m:
        d = _safe_date(m.group(3), m.group(1), m.group(2))
        if d:
            return d

    # YYYY-MM-DD
    m = _DATE_ISO_RE.search(title)
    if m:
        d = _safe_date(m.group(1), m.group(2), m.group(3))
        if d:
            return d

    # MMDDYYYY (e.g., "05192026 BOF Special Meeting")
    m = _DATE_8DIG_RE.search(title)
    if m:
        mm, dd, yyyy = int(m.group(1)), int(m.group(2)), int(m.group(3))
        if 1 <= mm <= 12 and 1 <= dd <= 31:
            d = _safe_date(yyyy, mm, dd)
            if d:
                return d

    # Fallback: decode .NET ticks from URL (this is publish date, not meeting date)
    m = _NET_TICKS_RE.search(url_path)
    if m:
        try:
            unix_s = (int(m.group(1)) - _NET_EPOCH_TICKS) / 10_000_000
            if 0 < unix_s < 4e9:
                return datetime.datetime.fromtimestamp(
                    unix_s, tz=datetime.timezone.utc
                ).date()
        except (ValueError, OSError):
            pass

    return None


# ---------------------------------------------------------------------------
# HTTP helpers
# ---------------------------------------------------------------------------

def fetch_html(url, allow_gzip=True):
    """Fetch a URL with browser-like headers. Returns HTML string or None."""
    headers = dict(FETCH_HEADERS)
    if allow_gzip:
        headers["Accept-Encoding"] = "gzip"
    req = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            raw = r.read()
            if r.headers.get("Content-Encoding") == "gzip":
                raw = gzip.decompress(raw)
            charset = r.headers.get_content_charset() or "utf-8"
            return raw.decode(charset, errors="replace")
    except urllib.error.HTTPError as e:
        print(f"  HTTP {e.code} — {url}", file=sys.stderr)
        return None
    except urllib.error.URLError as e:
        print(f"  ERROR {url}: {e}", file=sys.stderr)
        return None


def download_file(url, dest_path):
    """Download a file to dest_path. Returns True on success."""
    headers = dict(FETCH_HEADERS)
    headers["Accept"] = "application/pdf, */*"
    req = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            with open(dest_path, "wb") as f:
                f.write(r.read())
        return True
    except Exception as e:
        print(f"  WARNING: {e}", file=sys.stderr)
        return False


# ---------------------------------------------------------------------------
# Slug / path helpers
# ---------------------------------------------------------------------------

def slugify(text, max_len=50):
    text = text.lower().strip()
    text = re.sub(r"[/\\&+]", "-", text)
    text = re.sub(r"\s+-\s+", "-", text)
    text = re.sub(r"[^\w\s-]", "", text)
    text = re.sub(r"[\s_]+", "-", text)
    text = re.sub(r"-{2,}", "-", text)
    return text.strip("-")[:max_len]


def make_month_dir(output_dir, date):
    d = os.path.join(output_dir, date.strftime("%Y-%m"))
    os.makedirs(d, exist_ok=True)
    return d


def doc_dest_path(output_dir, board, doc_type, meeting_date):
    prefix = meeting_date.strftime("%Y-%m-%d")
    return os.path.join(
        make_month_dir(output_dir, meeting_date),
        f"{prefix}-{slugify(board)}-{doc_type}.pdf",
    )


# ---------------------------------------------------------------------------
# GovAccess document scraping
# ---------------------------------------------------------------------------

# <a title="Click to open YYYY folder" href=".../-folder-NNN">YYYY</a>
_FOLDER_LINK_RE = re.compile(
    r'<a[^>]+title=["\']Click to open (\d{4}) folder["\'][^>]+href=["\']([^"\']+)["\']',
    re.I,
)

# Search backwards from folder link for the nearest h2/h3 heading
_SECTION_HEAD_RE = re.compile(r'<h[23][^>]*>([^<]*)</h[23]>', re.I)


def _doc_type_from_context(html, pos):
    """Return 'agenda', 'minutes', or 'document' based on nearest preceding heading."""
    snippet = html[max(0, pos - 4000): pos]
    for h in reversed(_SECTION_HEAD_RE.findall(snippet)):
        h_low = h.lower()
        if "agenda" in h_low:
            return "agenda"
        if "minute" in h_low:
            return "minutes"
    return "document"


def find_board_folders(html, target_years):
    """
    Return [(doc_type, year, full_folder_url), ...] from a board page.
    """
    results = []
    seen = set()
    for m in _FOLDER_LINK_RE.finditer(html):
        year = int(m.group(1))
        if year not in target_years:
            continue
        folder_path = m.group(2)
        if not folder_path.startswith("http"):
            folder_path = BASE_URL + folder_path
        if folder_path in seen:
            continue
        seen.add(folder_path)
        doc_type = _doc_type_from_context(html, m.start())
        results.append((doc_type, year, folder_path))
    return results


# href="/home/showpublisheddocument/{docId}/{ticks}" → link text = document title
_DOC_LINK_RE = re.compile(
    r'<a[^>]+href=["\'](/home/showpublisheddocument/\d+/\d+)["\'][^>]*>(.*?)</a>',
    re.S | re.I,
)


def parse_folder_docs(html):
    """Return [(title, doc_url), ...] from a folder page."""
    docs = []
    seen = set()
    for m in _DOC_LINK_RE.finditer(html):
        path = m.group(1)
        if path in seen:
            continue
        seen.add(path)
        title = re.sub(r"<[^>]+>", "", m.group(2))
        title = " ".join(title.split()).strip()
        if not title:
            continue
        docs.append((title, BASE_URL + path))
    return docs


# ---------------------------------------------------------------------------
# Granicus video scraping
# ---------------------------------------------------------------------------

_GRAN_ROW_RE  = re.compile(r'<tr\s+class="(?:even|odd)"[^>]*>(.*?)</tr>', re.S | re.I)
_GRAN_TS_RE   = re.compile(r'<span[^>]*display:\s*none[^>]*>(\d+)</span>')
_GRAN_NAME_RE = re.compile(r'scope="row"[^>]*>(.*?)</td>', re.S | re.I)
_GRAN_CLIP_RE = re.compile(r'MediaPlayer\.php\?view_id=\d+&clip_id=(\d+)')


def parse_granicus_view(view_id, board_name, cutoff, future_limit):
    """
    Fetch a Granicus ViewPublisher page and return matching clips.
    Returns [(board_name, meeting_name, meeting_date, clip_id), ...]
    """
    url = f"{GRANICUS_URL}/ViewPublisher.php?view_id={view_id}"
    html = fetch_html(url, allow_gzip=False)
    if not html:
        return []

    clips = []
    seen_clips = set()
    for row_m in _GRAN_ROW_RE.finditer(html):
        row = row_m.group(1)

        ts_m = _GRAN_TS_RE.search(row)
        if not ts_m:
            continue
        try:
            ts = int(ts_m.group(1))
            meeting_date = datetime.datetime.fromtimestamp(
                ts, tz=datetime.timezone.utc
            ).date()
        except (ValueError, OSError):
            continue

        if not (cutoff <= meeting_date <= future_limit):
            continue

        clip_m = _GRAN_CLIP_RE.search(row)
        if not clip_m:
            continue
        clip_id = clip_m.group(1)
        if clip_id in seen_clips:
            continue
        seen_clips.add(clip_id)

        name_m = _GRAN_NAME_RE.search(row)
        meeting_name = ""
        if name_m:
            meeting_name = html_module.unescape(
                re.sub(r"<[^>]+>", "", name_m.group(1)).strip()
            )

        clips.append((board_name, meeting_name, meeting_date, clip_id))

    return clips


def download_video(view_id, clip_id, dest_template, dry_run=False):
    url = f"{GRANICUS_URL}/MediaPlayer.php?view_id={view_id}&clip_id={clip_id}"
    if dry_run:
        return True
    cmd = [
        "yt-dlp", "--js-runtimes", YT_DLP_NODE, "--no-playlist",
        "-f", "bestvideo+bestaudio/best",
        "--merge-output-format", "mp4",
        "-o", dest_template,
        "--no-overwrites", "--quiet", "--no-warnings", url,
    ]
    try:
        subprocess.run(cmd, check=True, timeout=3600)
        return True
    except FileNotFoundError:
        print("  ERROR: yt-dlp not found. Install with: pip install yt-dlp", file=sys.stderr)
        return False
    except subprocess.CalledProcessError as e:
        print(f"  WARNING: yt-dlp returned {e.returncode}", file=sys.stderr)
        return False
    except subprocess.TimeoutExpired:
        print(f"  ERROR: yt-dlp timed out downloading {url} — partial file kept, will resume next run", file=sys.stderr)
        return False


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        description="Download Stamford CT municipal meeting agendas, minutes, and video."
    )
    parser.add_argument(
        "--days", type=int, default=DAYS_BACK, metavar="N",
        help=f"Look back N days (default: {DAYS_BACK})",
    )
    parser.add_argument(
        "--ahead", type=int, default=DAYS_AHEAD, metavar="N",
        help=f"Include meetings up to N days ahead (default: {DAYS_AHEAD})",
    )
    parser.add_argument(
        "--output-dir", default=OUTPUT_DIR, metavar="DIR",
        help=f"Destination directory (default: {OUTPUT_DIR})",
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="List matches without downloading",
    )
    parser.add_argument(
        "--board", metavar="NAME",
        help="Only process boards whose name contains NAME (case-insensitive)",
    )

    vid_group = parser.add_mutually_exclusive_group()
    vid_group.add_argument(
        "--include-video", action="store_true",
        help="Download PDFs and Granicus video recordings",
    )
    vid_group.add_argument(
        "--video-only", action="store_true",
        help="Download Granicus videos only, skip PDFs",
    )
    vid_group.add_argument(
        "--docs-only", action="store_true",
        help="Download PDFs only (default behavior)",
    )
    args = parser.parse_args()

    now = datetime.datetime.now()
    if (now.weekday() == 5 and now.hour >= 18) or (now.weekday() == 6 and now.hour < 12):  # Saturday night, Sunday morning
        print("Skipping — no downloads on Saturday nights or Sunday mornings.")
        sys.exit(0)

    do_docs  = not args.video_only
    do_video = args.include_video or args.video_only
    board_filter = args.board.lower() if args.board else None

    today        = datetime.date.today()
    cutoff       = today - datetime.timedelta(days=args.days)
    future_limit = today + datetime.timedelta(days=args.ahead)
    target_years = {cutoff.year, today.year, future_limit.year}

    print(f"Date window : {cutoff} to {future_limit}")
    if do_docs:
        print(f"Source (doc): {BASE_URL} (GovAccess — {len(BOARD_PAGES)} boards)")
    if do_video:
        print(f"Source (vid): {GRANICUS_URL} ({len(GRANICUS_VIEWS)} views)")
    if not args.dry_run:
        print(f"Output dir  : {args.output_dir}")
    print()

    # ----------------------------------------------------------------
    # Phase 1: PDF documents via GovAccess board pages
    # ----------------------------------------------------------------
    all_docs = []  # [(board_name, doc_type, meeting_date, doc_url, title), ...]

    if do_docs:
        boards_to_scan = [
            (name, path) for name, path in BOARD_PAGES
            if not board_filter or board_filter in name.lower()
        ]
        print(f"Scanning {len(boards_to_scan)} board page(s) for documents...")

        for board_name, page_path in boards_to_scan:
            page_url = BASE_URL + page_path
            html = fetch_html(page_url)
            if not html:
                continue

            folders = find_board_folders(html, target_years)
            if not folders:
                continue

            board_docs = []
            for doc_type, year, folder_url in folders:
                folder_html = fetch_html(folder_url)
                if not folder_html:
                    continue
                for title, doc_url in parse_folder_docs(folder_html):
                    meeting_date = parse_doc_date(title, doc_url)
                    if meeting_date and cutoff <= meeting_date <= future_limit:
                        board_docs.append(
                            (board_name, doc_type, meeting_date, doc_url, title)
                        )
                time.sleep(DELAY_SECONDS)

            if board_docs:
                print(f"  {board_name}: {len(board_docs)} document(s)")
            all_docs.extend(board_docs)
            time.sleep(DELAY_SECONDS)

        # Deduplicate by URL
        seen_urls = set()
        deduped = []
        for item in all_docs:
            if item[3] not in seen_urls:
                seen_urls.add(item[3])
                deduped.append(item)
        all_docs = sorted(deduped, key=lambda x: (x[2], x[0]), reverse=True)

        print(f"\n  Total: {len(all_docs)} document(s) in date window.")
        print()

    # ----------------------------------------------------------------
    # Phase 2: Video recordings via Granicus
    # ----------------------------------------------------------------
    all_clips = []  # [(board_name, meeting_name, meeting_date, view_id, clip_id), ...]

    if do_video:
        views_to_scan = {
            vid: name for vid, name in GRANICUS_VIEWS.items()
            if not board_filter or board_filter in name.lower()
        }
        print(f"Scanning {len(views_to_scan)} Granicus view(s) for recordings...")

        for view_id, board_name in sorted(views_to_scan.items()):
            clips = parse_granicus_view(view_id, board_name, cutoff, future_limit)
            if clips:
                print(f"  {board_name}: {len(clips)} recording(s)")
            for board_name, meeting_name, meeting_date, clip_id in clips:
                all_clips.append((board_name, meeting_name, meeting_date, view_id, clip_id))
            time.sleep(DELAY_SECONDS)

        all_clips.sort(key=lambda x: (x[2], x[0]), reverse=True)
        print(f"\n  Total: {len(all_clips)} recording(s) in date window.")
        print()

    if not all_docs and not all_clips:
        print("Nothing found within the date window.")
        return

    # ----------------------------------------------------------------
    # Dry-run: print summary
    # ----------------------------------------------------------------
    if args.dry_run:
        if all_docs:
            print(f"{'Board':<42} {'Date':<12} {'Type':<9} Title")
            print("-" * 90)
            for board_name, doc_type, meeting_date, _, title in all_docs:
                print(
                    f"{board_name[:41]:<42} {meeting_date!s:<12} "
                    f"{doc_type:<9} {title[:34]}"
                )
            print(f"\n{len(all_docs)} document(s).")
        if all_clips:
            if all_docs:
                print()
            print(f"{'Board':<42} {'Date':<12} Recording")
            print("-" * 80)
            for board_name, meeting_name, meeting_date, _, clip_id in all_clips:
                print(f"{board_name[:41]:<42} {meeting_date!s:<12} {meeting_name[:34]}")
            print(f"\n{len(all_clips)} recording(s).")
        print("\nRe-run without --dry-run to download.")
        return

    # ----------------------------------------------------------------
    # Download PDFs
    # ----------------------------------------------------------------
    os.makedirs(args.output_dir, exist_ok=True)
    log_path = os.path.join(args.output_dir, "download-log.txt")
    log_lines = []
    downloaded = skipped = failed = 0

    for board_name, doc_type, meeting_date, doc_url, title in all_docs:
        dest = doc_dest_path(args.output_dir, board_name, doc_type, meeting_date)
        label = os.path.basename(dest)

        if os.path.exists(dest):
            print(f"  skip (exists)  {label}")
            skipped += 1
            continue

        print(f"  [{meeting_date}] {board_name} — {doc_type}")
        print(f"  downloading    {label}")

        if download_file(doc_url, dest):
            downloaded += 1
            log_lines.append(f"{datetime.datetime.now().isoformat()}  OK       {dest}")
        else:
            failed += 1
            log_lines.append(f"{datetime.datetime.now().isoformat()}  FAILED   {doc_url}")
            if os.path.exists(dest):
                os.remove(dest)

        time.sleep(DELAY_SECONDS)

    # ----------------------------------------------------------------
    # Download videos
    # ----------------------------------------------------------------
    vid_downloaded = vid_skipped = vid_failed = 0

    if all_clips:
        if all_docs:
            print()
        print("Downloading recordings...")
        seen_clips = set()
        for board_name, meeting_name, meeting_date, view_id, clip_id in all_clips:
            if clip_id in seen_clips:
                continue
            seen_clips.add(clip_id)

            title_slug = slugify(meeting_name or board_name)
            date_str = meeting_date.strftime("%Y-%m-%d")
            mdir = make_month_dir(args.output_dir, meeting_date)
            dest_template = os.path.join(mdir, f"{date_str}-{title_slug}.%(ext)s")
            dest_mp4 = os.path.join(mdir, f"{date_str}-{title_slug}.mp4")

            if os.path.exists(dest_mp4):
                print(f"  skip (exists)  {os.path.basename(dest_mp4)}")
                vid_skipped += 1
                continue

            print(f"  [{meeting_date}] {board_name} — {meeting_name}")
            granicus_url = f"{GRANICUS_URL}/MediaPlayer.php?view_id={view_id}&clip_id={clip_id}"
            print(f"  source URL:    {granicus_url}")
            if download_video(view_id, clip_id, dest_template):
                vid_downloaded += 1
                log_lines.append(f"{datetime.datetime.now().isoformat()}  OK       {dest_mp4}")
            else:
                vid_failed += 1
                log_lines.append(
                    f"{datetime.datetime.now().isoformat()}  FAILED   "
                    f"{GRANICUS_URL}/MediaPlayer.php?view_id={view_id}&clip_id={clip_id}"
                )

    if log_lines:
        with open(log_path, "a") as f:
            f.write("\n".join(log_lines) + "\n")

    print()
    if do_docs:
        print(f"PDFs  — downloaded: {downloaded}  skipped: {skipped}  failed: {failed}")
    if do_video:
        print(f"Video — downloaded: {vid_downloaded}  skipped: {vid_skipped}  failed: {vid_failed}")
    if downloaded + skipped + vid_downloaded + vid_skipped:
        print(f"Files in: {args.output_dir}")
    if log_lines:
        print(f"Log:      {log_path}")


if __name__ == "__main__":
    main()


# ---------------------------------------------------------------------------
# Tips
# ---------------------------------------------------------------------------
#
# 1. Preview without downloading (scans all board pages — takes ~2-3 min):
#    python3 scripts/download-stamford-agendas.py --dry-run
#
# 2. Preview one board quickly:
#    python3 scripts/download-stamford-agendas.py --dry-run --board "Planning Board"
#
# 3. Documents + video recordings:
#    python3 scripts/download-stamford-agendas.py --include-video
#
# 4. Video only (faster — skips GovAccess page scraping):
#    python3 scripts/download-stamford-agendas.py --video-only
#
# 5. Narrow to one board (both docs and video):
#    python3 scripts/download-stamford-agendas.py --board "Zoning"
#    python3 scripts/download-stamford-agendas.py --board "Board of Finance"
#
# 6. Change the lookback window:
#    python3 scripts/download-stamford-agendas.py --days 7
#
# 7. Save files somewhere else:
#    python3 scripts/download-stamford-agendas.py --output-dir ~/Downloads/stamford
#
# 8. Run on a schedule (cron — 8 AM daily):
#    0 8 * * * cd /path/to/repo && python3 scripts/download-stamford-agendas.py
#
# NOTE: Full-scan discovery (40 board pages + their folder pages) takes ~2-3 min
# with 0.5s request delays. Use --board to narrow scope for day-to-day use.
#
# NOTE: stamfordct.gov blocks plain urllib. The script sends full browser headers
# (Sec-Fetch-*) to bypass Akamai CDN detection. If 403s appear, update the
# User-Agent Chrome version string in FETCH_HEADERS.
#
# NOTE: The .NET ticks URL fallback is the document *publish* date, not meeting
# date. It is accurate to ±1 day for most agendas (published day-of or day-before).
#
# NOTE: Granicus views 1, 16, and 30 are empty/inactive and are excluded.
# View 2 (Terry Conners Rink) contains non-government-meeting content.
