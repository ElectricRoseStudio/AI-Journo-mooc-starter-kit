#!/usr/bin/env python3
# download-ridgefield-agendas.py
# Download Ridgefield CT municipal meeting agendas, minutes, and recording
# shortcuts for meetings posted in the past N days.
#
# USAGE:
#   python3 scripts/download-ridgefield-agendas.py [options]
#
# REQUIREMENTS:
#   - Python 3.8+ (no third-party packages required)
#   - pdftohtml (from poppler-utils) — required for BOE agenda index parsing
#     Install: sudo apt install poppler-utils
#
# WHAT IT DOES:
#   1. Fetches the Ridgefield CT Agendas, Minutes & Video hub page
#   2. Discovers all 34 board/committee sub-pages (Revize CMS)
#   3. Parses the Revize document center on each board page:
#      agendas, packets, and minutes PDFs
#   4. Checks recording sub-pages (meeting_audios.php, audio_recordings.php)
#      and saves media links as .url shortcuts
#   5. Downloads and parses the BOE meetings PDF index (Google Drive):
#      fetches agenda/minutes/packet PDFs; saves YouTube video shortcuts
#   6. Scrapes the BOE YouTube channel for committee meeting videos not
#      yet listed in the PDF index
#   7. Saves a .url shortcut for the Parks & Recreation Commission page
#      (site is Cloudflare-protected; documents must be viewed manually)
#   8. Filters all items by date (--days lookback window)
#   9. Saves PDFs to beat-archive/ridgefield-agendas/YYYY-MM/
#   10. Saves recording shortcuts to beat-archive/ridgefield-agendas/recordings/
#   11. Appends a download log to beat-archive/ridgefield-agendas/download-log.txt
#
# SITE STRUCTURE:
#   Revize CMS (ridgefieldct.gov):
#     Hub:  https://www.ridgefieldct.gov/government/agendas_minutes_video.php
#     Notes: Hub document center is JS-rendered; board sub-pages have static HTML.
#            All hrefs resolve to site root. video_url column is always empty;
#            recordings are on separate sub-pages (meeting_audios.php, etc.).
#   BOE (Board of Education):
#     PDF index:  Google Drive (publicly shared, no auth)
#     Documents:  Google Drive PDFs
#     Videos:     https://www.youtube.com/@rpsboe7673/streams
#   Parks & Recreation Commission:
#     https://www.ridgefieldparksandrec.org/about-parks-recreation/commission
#     Cloudflare-protected — saved as .url shortcut for manual access

import argparse
import datetime
import html
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

BASE_URL = "https://www.ridgefieldct.gov"
HUB_URL = f"{BASE_URL}/government/agendas_minutes_video.php"
OUTPUT_DIR = "beat-archive/ridgefield-agendas"
DAYS_BACK = 4
DELAY_SECONDS = 1.0

UA = ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36")

BOE_BOARD_NAME = "Board of Education"
BOE_PDF_GDRIVE_ID = "1NTnma6VP-cYj1Tfxr3n-EE7UWOptfdSF"
BOE_PDF_URL = (
    f"https://drive.google.com/uc?export=download&id={BOE_PDF_GDRIVE_ID}"
)
BOE_YT_CHANNEL_URL = "https://www.youtube.com/@rpsboe7673/streams"

PARKS_REC_BOARD_NAME = "Parks & Recreation Commission"
PARKS_REC_URL = (
    "https://www.ridgefieldparksandrec.org/about-parks-recreation/commission"
)

# ---------------------------------------------------------------------------
# Regex patterns
# ---------------------------------------------------------------------------

# Revize document center: one entry per row spanning date + name + 4 columns
_ENTRY_RE = re.compile(
    r'<span\s+class="agenda-date">\s*(.*?)\s*</span>.*?'
    r'<span\s+class="agenda-name">\s*(.*?)\s*</span>.*?'
    r'class="agenda_doc">(.*?)</td>.*?'
    r'class="packet_doc">(.*?)</td>.*?'
    r'class="minutes_doc">(.*?)</td>.*?'
    r'class="video_url">(.*?)</td>',
    re.S | re.I,
)
_HREF_RE = re.compile(r'href=["\']([^"\']+)["\']', re.I)
_STRIP_TAGS_RE = re.compile(r'<[^>]+>')

# Board links on hub page (hub uses href= "..." with space; hrefs are relative)
_BOARD_LINK_RE = re.compile(
    r'<a\s[^>]*href=\s*"(boards_committees_commissions/[^"#?]+)"[^>]*>(.*?)</a>',
    re.S | re.I,
)
# Fallback: agenda_minutes.php sub-link on a board overview page
_AGENDA_PAGE_RE = re.compile(
    r'href="(/?[^"]*agenda_minutes\.php)"',
    re.I,
)
# Recording sub-page links (hrefs are relative, no leading /)
_REC_SUBPAGE_RE = re.compile(
    r'href=\s*"(/?[^"]*(?:audio_recordings?|meeting_audios?|video_recordings?)\.php)"',
    re.I,
)

# Date: MM/DD/YYYY (Revize doc center format)
_DATE_RE = re.compile(r'\b(\d{1,2})/(\d{1,2})/(20\d{2})\b')
# Date: "April 21, 2026" (Revize recording sub-page link text)
_DATE_LONG_RE = re.compile(
    r'\b(January|February|March|April|May|June|July|August|September|'
    r'October|November|December)\s+(\d{1,2}),?\s+(20\d{2})\b',
    re.I,
)

# Revize recording sub-page: <a href="media_url">link text with date</a>
_REC_LINK_RE = re.compile(
    r'<a\s[^>]*href=\s*"'
    r'(https?://(?:[a-z0-9.-]+\.)?(?:youtube\.com|youtu\.be|vimeo\.com|'
    r'zoom\.us|dropbox\.com|boxcast\.com)/[^"]+|'
    r'[^"]+\.(?:mp3|mp4|wav|m4a|webm)(?:\?[^"]*)?)"'
    r'[^>]*>(.*?)</a>',
    re.S | re.I,
)
_SHARE_PATH_RE = re.compile(r'(?:sharer|share\.|/share)', re.I)

# BOE: extract Google Drive file ID from view or download URL
_GDRIVE_ID_RE = re.compile(
    r'drive\.google\.com/(?:file/d/|uc[^"]*[?&]id=)([A-Za-z0-9_-]{20,})',
    re.I,
)

# BOE YouTube channel: pairs videoId with video title from page JSON
_YT_VID_TITLE_RE = re.compile(
    r'"videoId":"([A-Za-z0-9_-]{11})"'
    r'(?:(?!"videoId").){0,4000}'
    r'"lockupMetadataViewModel":\{"title":\{"content":"([^"]+)"',
    re.S,
)
# BOE: date in YouTube video title (e.g. "5-11-2026", "05-07-26", "2/9/26")
_TITLE_DATE_RE = re.compile(r'(\d{1,2})[-/](\d{1,2})[-/](\d{2,4})')

# pdftohtml XML structure
_PDF_PAGE_RE = re.compile(r'<page\b[^>]*>(.*?)</page>', re.S)
_PDF_TEXT_RE = re.compile(r'<text\s+top="(\d+)"[^>]*>(.*?)</text>', re.S)
_PDF_HREF_RE = re.compile(r'<a\s+href="([^"]+)"', re.I)
_PDF_MONTH_YEAR_RE = re.compile(
    r'^(January|February|March|April|May|June|July|August|September|'
    r'October|November|December)\s+(202\d)\s*$',
    re.I,
)
_PDF_MONTH_ONLY_RE = re.compile(
    r'^(January|February|March|April|May|June|July|August|September|'
    r'October|November|December)\s*$',
    re.I,
)
_PDF_MONTH_DAY_RE = re.compile(
    r'^(January|February|March|April|May|June|July|August|September|'
    r'October|November|December)\s+(\d{1,2})\*?(?:\s|$)',
    re.I,
)
_PDF_MONTHS = {
    m.lower(): i + 1
    for i, m in enumerate([
        "january", "february", "march", "april", "may", "june",
        "july", "august", "september", "october", "november", "december",
    ])
}


# ---------------------------------------------------------------------------
# HTTP helpers
# ---------------------------------------------------------------------------

def fetch_html(url, *, timeout=30):
    req = urllib.request.Request(url, headers={
        "User-Agent": UA,
        "Accept": "text/html,application/xhtml+xml,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
    })
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            raw = r.read()
            charset = r.headers.get_content_charset() or "utf-8"
            return raw.decode(charset, errors="replace")
    except urllib.error.HTTPError as e:
        print(f"  HTTP {e.code} — {url}", file=sys.stderr)
        return None
    except Exception as e:
        print(f"  ERROR fetching {url}: {e}", file=sys.stderr)
        return None


def download_binary(url, dest_path, *, timeout=60):
    """Download url to dest_path. Returns True on success."""
    req = urllib.request.Request(
        url.replace(" ", "%20"),
        headers={"User-Agent": UA},
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            data = r.read()
        if not data:
            return False
        with open(dest_path, "wb") as f:
            f.write(data)
        return True
    except urllib.error.HTTPError as e:
        print(f"  HTTP {e.code} — {url}", file=sys.stderr)
        return False
    except Exception as e:
        print(f"  ERROR: {e}", file=sys.stderr)
        return False


def save_url_shortcut(url, path):
    with open(path, "w") as f:
        f.write(f"[InternetShortcut]\nURL={url}\n")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def slugify(text):
    text = _STRIP_TAGS_RE.sub("", text)
    text = html.unescape(text).lower().strip()
    text = re.sub(r"[/\\]", "-", text)
    text = re.sub(r"[^\w\s-]", "", text)
    text = re.sub(r"[\s_]+", "-", text)
    text = re.sub(r"-{2,}", "-", text)
    return text.strip("-")[:60]


def strip_tags(s):
    return html.unescape(_STRIP_TAGS_RE.sub("", s)).strip()


def parse_date(text):
    """
    Parse MM/DD/YYYY or 'Month DD, YYYY' from text. Returns date or None.
    """
    text = strip_tags(text)
    m = _DATE_RE.search(text)
    if m:
        try:
            return datetime.date(int(m.group(3)), int(m.group(1)), int(m.group(2)))
        except ValueError:
            pass
    m = _DATE_LONG_RE.search(text)
    if m:
        try:
            return datetime.datetime.strptime(
                f"{m.group(1)} {int(m.group(2)):02d} {m.group(3)}", "%B %d %Y"
            ).date()
        except ValueError:
            pass
    return None


def resolve_url(href):
    """Resolve a Revize href to an absolute URL (site-root relative)."""
    if not href:
        return None
    href = href.strip()
    if not href or href.startswith("#") or href.startswith("mailto:"):
        return None
    if href.startswith("http"):
        return href
    return BASE_URL + "/" + href.lstrip("/")


def first_href(cell_html):
    """Return the first href value from a cell's HTML, or None."""
    m = _HREF_RE.search(cell_html)
    return m.group(1).strip() if m else None


def _gdrive_download_url(url):
    """Convert a Google Drive view URL to a direct-download URL."""
    m = _GDRIVE_ID_RE.search(url)
    if not m:
        return None
    return f"https://drive.google.com/uc?export=download&id={m.group(1)}"


# ---------------------------------------------------------------------------
# Revize CMS board discovery
# ---------------------------------------------------------------------------

def discover_boards(hub_html):
    """
    Return list of (board_name, absolute_url) from the hub page.
    Searches only the #post content div to exclude global nav links,
    and requires at least two path segments under boards_committees_commissions.
    """
    post_m = re.search(r'id="post"[^>]*>(.*)', hub_html, re.S | re.I)
    content = post_m.group(1) if post_m else hub_html

    # Limit to the board-listing table
    table_m = re.search(r'(<table[^>]*>.*?</table>)', content, re.S | re.I)
    content = table_m.group(1) if table_m else content

    boards = []
    seen = set()
    for m in _BOARD_LINK_RE.finditer(content):
        href = m.group(1)
        if len(href.rstrip("/").split("/")) < 2:
            continue
        name = strip_tags(m.group(2))
        if not name or len(name) < 3:
            continue
        abs_url = BASE_URL + "/" + href.lstrip("/")
        if abs_url in seen:
            continue
        seen.add(abs_url)
        boards.append((name, abs_url))
    return boards


def get_document_page(board_url):
    """
    Fetch the board page. If it contains the Revize document center, return it.
    Otherwise look for a linked agenda_minutes.php sub-page.
    Returns (url, html) — html may lack a document center if none was found.
    """
    board_html = fetch_html(board_url)
    if not board_html:
        return None, None

    if 'class="agenda-date"' in board_html:
        return board_url, board_html

    m = _AGENDA_PAGE_RE.search(board_html)
    if m:
        sub_url = BASE_URL + "/" + m.group(1).lstrip("/")
        if sub_url != board_url:
            sub_html = fetch_html(sub_url)
            if sub_html and 'class="agenda-date"' in sub_html:
                return sub_url, sub_html

    return board_url, board_html


# ---------------------------------------------------------------------------
# Revize document center parsing
# ---------------------------------------------------------------------------

def parse_entries(board_name, html_content, cutoff):
    """
    Parse Revize document center rows. Returns entry dicts for meetings >= cutoff.
    """
    entries = []
    for m in _ENTRY_RE.finditer(html_content):
        date = parse_date(m.group(1))
        if date is None or date < cutoff:
            continue
        entries.append({
            "board": board_name,
            "name": strip_tags(m.group(2)),
            "date": date,
            "agenda_url": resolve_url(first_href(m.group(3))),
            "packet_url": resolve_url(first_href(m.group(4))),
            "minutes_url": resolve_url(first_href(m.group(5))),
            "video_url": resolve_url(first_href(m.group(6))),
        })
    return entries


# ---------------------------------------------------------------------------
# Revize recording sub-page parsing
# ---------------------------------------------------------------------------

def parse_recordings(board_name, rec_html, cutoff):
    """
    Parse a Revize recording sub-page. Each entry is an <a href="media_url">
    with the meeting date in the link text.
    Returns list of dicts: {board, date, recording_url}
    """
    recordings = []
    seen_urls = set()
    for m in _REC_LINK_RE.finditer(rec_html):
        href = m.group(1)
        if _SHARE_PATH_RE.search(href):
            continue
        date = parse_date(strip_tags(m.group(2)))
        if date is None or date < cutoff:
            continue
        rec_url = href if href.startswith("http") else resolve_url(href)
        if rec_url and rec_url not in seen_urls:
            seen_urls.add(rec_url)
            recordings.append({"board": board_name, "date": date, "recording_url": rec_url})
    return recordings


# ---------------------------------------------------------------------------
# BOE: Google Drive PDF index
# ---------------------------------------------------------------------------

def scrape_boe_pdf(cutoff):
    """
    Download and parse the BOE meetings index PDF (Google Drive).
    Returns entry dicts with agenda/packet/minutes download URLs and YouTube
    video URLs. Requires pdftohtml (poppler-utils); returns [] if missing.
    """
    if not shutil.which("pdftohtml"):
        print(
            "  WARNING: pdftohtml not found (sudo apt install poppler-utils). "
            "Skipping BOE PDF parsing.",
            file=sys.stderr,
        )
        return []

    print("  Fetching BOE meetings index PDF...")
    req = urllib.request.Request(BOE_PDF_URL, headers={"User-Agent": UA})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            pdf_data = r.read()
    except Exception as e:
        print(f"  ERROR fetching BOE PDF: {e}", file=sys.stderr)
        return []

    tmp = tempfile.mktemp(suffix=".pdf")
    try:
        with open(tmp, "wb") as f:
            f.write(pdf_data)
        result = subprocess.run(
            ["pdftohtml", "-xml", "-stdout", tmp],
            capture_output=True, text=True, timeout=30,
        )
        xml = result.stdout
    finally:
        if os.path.exists(tmp):
            os.remove(tmp)

    if not xml:
        print("  ERROR: pdftohtml produced no output.", file=sys.stderr)
        return []

    return _parse_boe_xml(xml, cutoff)


def _parse_boe_xml(xml, cutoff):
    """
    Parse pdftohtml -xml output to extract dated BOE meeting links.
    Scans text elements top-to-bottom, tracking month/year/day context.
    Document links (Agenda, Packet, Minutes, Video) are associated with the
    most recently seen meeting date.
    """
    entries = {}  # (month_lower, day, year) -> {agenda, packet, minutes, video}
    current_year = None
    current_month = None
    current_day = None

    for page_m in _PDF_PAGE_RE.finditer(xml):
        items = [
            (int(tm.group(1)), _STRIP_TAGS_RE.sub("", tm.group(2)).strip(),
             (_PDF_HREF_RE.search(tm.group(2)) or type("", (), {"group": lambda s, n: None})()).group(1))
            for tm in _PDF_TEXT_RE.finditer(page_m.group(1))
        ]

        for _, text, href in sorted(items, key=lambda x: x[0]):
            if not text:
                continue

            # "Month YYYY" section header
            mym = _PDF_MONTH_YEAR_RE.match(text)
            if mym:
                current_month = mym.group(1).lower()
                current_year = mym.group(2)
                current_day = None
                continue

            # "Month" alone (no year — preserve current_year)
            mom = _PDF_MONTH_ONLY_RE.match(text)
            if mom:
                current_month = mom.group(1).lower()
                current_day = None
                continue

            # "Month Day" or "Month Day - Description..."
            mdm = _PDF_MONTH_DAY_RE.match(text)
            if mdm:
                current_month = mdm.group(1).lower()
                current_day = mdm.group(2)
                continue

            # Document link associated with the current date
            if href and current_month and current_day and current_year:
                key = (current_month, current_day, current_year)
                if key not in entries:
                    entries[key] = {"agenda": [], "packet": [], "minutes": [], "video": []}
                label = text.lower().split()[0] if text else ""
                if label == "agenda":
                    entries[key]["agenda"].append(href)
                elif label == "minutes":
                    entries[key]["minutes"].append(href)
                elif label in ("packet", "packets"):
                    entries[key]["packet"].append(href)
                elif label == "video":
                    entries[key]["video"].append(href)

    def _to_dl(url):
        if url and "drive.google.com" in url:
            return _gdrive_download_url(url) or url
        return url

    result = []
    for (month_lower, day, year), docs in entries.items():
        month_num = _PDF_MONTHS.get(month_lower)
        if not month_num:
            continue
        try:
            date = datetime.date(int(year), month_num, int(day))
        except ValueError:
            continue
        if date < cutoff:
            continue
        result.append({
            "board": BOE_BOARD_NAME,
            "name": f"{month_lower.capitalize()} {day}",
            "date": date,
            "agenda_url": _to_dl(docs["agenda"][0]) if docs["agenda"] else None,
            "packet_url": _to_dl(docs["packet"][0]) if docs["packet"] else None,
            "minutes_url": _to_dl(docs["minutes"][0]) if docs["minutes"] else None,
            "video_url": docs["video"][0] if docs["video"] else None,
        })

    return result


# ---------------------------------------------------------------------------
# BOE: YouTube channel
# ---------------------------------------------------------------------------

def _parse_title_date(title):
    """
    Extract a date from a BOE YouTube video title.
    Handles: '5-11-2026', '05-07-26', '4-27-26', '2/9/26'.
    """
    m = _TITLE_DATE_RE.search(title)
    if not m:
        return None
    month, day, yr = int(m.group(1)), int(m.group(2)), int(m.group(3))
    if yr < 100:
        yr += 2000
    try:
        return datetime.date(yr, month, day)
    except ValueError:
        return None


def scrape_boe_youtube(cutoff, known_dates=None):
    """
    Scrape the BOE YouTube streams channel for videos within the date window.
    known_dates: set of dates already linked in the PDF (to avoid duplicates).
    Returns list of recording dicts: {board, date, recording_url}
    """
    known_dates = known_dates or set()
    print("  Fetching BOE YouTube channel...")
    page_html = fetch_html(BOE_YT_CHANNEL_URL)
    if not page_html:
        return []

    recordings = []
    seen_ids = set()
    for m in _YT_VID_TITLE_RE.finditer(page_html):
        vid_id = m.group(1)
        title = html.unescape(m.group(2))
        if vid_id in seen_ids:
            continue
        seen_ids.add(vid_id)

        date = _parse_title_date(title)
        if date is None or date < cutoff or date in known_dates:
            continue

        recordings.append({
            "board": BOE_BOARD_NAME,
            "date": date,
            "recording_url": f"https://www.youtube.com/watch?v={vid_id}",
        })

    return recordings


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        description=(
            "Download Ridgefield CT municipal meeting agendas, minutes, and "
            "recording shortcuts for the past N days."
        )
    )
    parser.add_argument("--days", type=int, default=DAYS_BACK, metavar="N",
                        help=f"Look back N days (default: {DAYS_BACK})")
    parser.add_argument("--output-dir", default=OUTPUT_DIR, metavar="DIR",
                        help=f"Destination directory (default: {OUTPUT_DIR})")
    parser.add_argument("--dry-run", action="store_true",
                        help="List matching items without downloading")
    parser.add_argument("--board", metavar="NAME",
                        help="Only process boards whose name contains NAME "
                             "(case-insensitive; e.g. 'Selectpersons', 'Education', 'Parks')")
    parser.add_argument("--no-agendas", action="store_true",
                        help="Skip agenda PDFs")
    parser.add_argument("--no-packets", action="store_true",
                        help="Skip agenda packet PDFs")
    parser.add_argument("--no-minutes", action="store_true",
                        help="Skip minutes PDFs")
    parser.add_argument("--no-video", action="store_true",
                        help="Skip saving recording shortcuts")
    args = parser.parse_args()

    now = datetime.datetime.now()
    if (now.weekday() == 5 and now.hour >= 18) or (now.weekday() == 6 and now.hour < 12):  # Saturday night, Sunday morning
        print("Skipping — no downloads on Saturday nights or Sunday mornings.")
        sys.exit(0)

    cutoff = datetime.date.today() - datetime.timedelta(days=args.days)
    want_agendas = not args.no_agendas
    want_packets = not args.no_packets
    want_minutes = not args.no_minutes

    filt = args.board.lower() if args.board else None
    include_boe = filt is None or filt in BOE_BOARD_NAME.lower()
    include_parks_rec = filt is None or filt in PARKS_REC_BOARD_NAME.lower()

    print(f"Cutoff date : {cutoff}  ({args.days} days back)")
    print(f"Hub page    : {HUB_URL}")
    print(f"Output dir  : {args.output_dir}")
    if args.board:
        print(f"Board filter: '{args.board}'")
    print()

    # --- Step 1: Revize CMS boards ---
    print("Fetching hub page...")
    hub_html = fetch_html(HUB_URL)
    if not hub_html:
        print("ERROR: Could not fetch the hub page.", file=sys.stderr)
        sys.exit(1)

    boards = discover_boards(hub_html)
    if not boards:
        print("WARNING: No board pages found on hub — structure may have changed.",
              file=sys.stderr)
        sys.exit(1)

    print(f"Discovered {len(boards)} Revize board page(s).")
    if filt:
        boards = [(n, u) for n, u in boards if filt in n.lower()]
        if boards:
            print(f"Filtered to {len(boards)} matching '{args.board}'.")
    print()

    # Exit early only if nothing at all will run
    if filt and not boards and not include_boe and not include_parks_rec:
        print(f"No boards match '{args.board}'.")
        sys.exit(0)

    all_entries = []
    all_recordings = []

    for board_name, board_url in boards:
        print(f"  Scanning: {board_name}")
        doc_url, doc_html = get_document_page(board_url)
        if not doc_html:
            continue

        entries = parse_entries(board_name, doc_html, cutoff)
        all_entries.extend(entries)
        if entries:
            print(f"    {len(entries)} entry(s) in date window")

        # Recording sub-pages: filter to the current board's directory,
        # normalising trailing underscores (e.g. planning_and_zoning_commission_/)
        board_rel = board_url.replace(BASE_URL + "/", "")
        board_parts = board_rel.lstrip("/").split("/")
        board_seg = board_parts[1].rstrip("_") if len(board_parts) >= 2 else None
        rec_subpage_urls = set()
        for m in _REC_SUBPAGE_RE.finditer(doc_html):
            href = m.group(1).lstrip("/")
            href_parts = href.split("/")
            if len(href_parts) >= 2 and href_parts[1].rstrip("_") == board_seg:
                rec_subpage_urls.add(BASE_URL + "/" + href)
        for sub_url in rec_subpage_urls:
            rec_html = fetch_html(sub_url)
            if rec_html:
                recs = parse_recordings(board_name, rec_html, cutoff)
                all_recordings.extend(recs)
                if recs:
                    print(f"    {len(recs)} recording(s) in date window")
            time.sleep(0.3)

        time.sleep(0.3)

    if boards:
        print()
        print(f"Revize: {len(all_entries)} entries, {len(all_recordings)} recordings")
        print()

    # --- Step 2: Board of Education ---
    if include_boe:
        print(f"=== {BOE_BOARD_NAME} ===")
        boe_entries = scrape_boe_pdf(cutoff)
        print(f"  {len(boe_entries)} entry(s) from PDF index")
        # Dates already linked to a video in the PDF — skip in YouTube pass
        boe_dated_videos = {e["date"] for e in boe_entries if e["video_url"]}
        boe_recordings = scrape_boe_youtube(cutoff, known_dates=boe_dated_videos)
        print(f"  {len(boe_recordings)} additional recording(s) from YouTube channel")
        all_entries.extend(boe_entries)
        all_recordings.extend(boe_recordings)
        print()

    # --- Step 3: Parks & Recreation (shortcut only) ---
    parks_rec_shortcut = (
        PARKS_REC_URL if (include_parks_rec and not args.no_video) else None
    )

    # --- Collate ---
    all_entries.sort(key=lambda e: (e["date"], e["board"], e["name"]))
    all_recordings.sort(key=lambda r: (r["date"], r["board"]))

    if not all_entries and not all_recordings and not parks_rec_shortcut:
        print("No items found within the date window.")
        return

    if args.dry_run:
        if all_entries:
            print(f"{'Date':<12} {'Board':<40} {'Name':<35} Agnd Pkts Mins Vid")
            print("-" * 100)
            for e in all_entries:
                has_a = "Y" if (e["agenda_url"] and want_agendas) else "-"
                has_p = "Y" if (e["packet_url"] and want_packets) else "-"
                has_m = "Y" if (e["minutes_url"] and want_minutes) else "-"
                has_v = "Y" if (e["video_url"] and not args.no_video) else "-"
                print(f"{str(e['date']):<12} {e['board'][:39]:<40} "
                      f"{e['name'][:34]:<35} {has_a:<5}{has_p:<5}{has_m:<5}{has_v}")

        if all_recordings and not args.no_video:
            print()
            print(f"{'Date':<12} {'Board':<40} Recording URL")
            print("-" * 85)
            for r in all_recordings:
                print(f"{str(r['date']):<12} {r['board'][:39]:<40} "
                      f"{r['recording_url'][:32]}")

        if parks_rec_shortcut:
            print()
            print(f"Parks & Rec: {PARKS_REC_URL}")
            print("  [will be saved as recordings/parks-rec-commission.url]")

        total_docs = sum(
            bool(e["agenda_url"] and want_agendas) +
            bool(e["packet_url"] and want_packets) +
            bool(e["minutes_url"] and want_minutes)
            for e in all_entries
        )
        print(f"\n{total_docs} doc(s), {len(all_recordings)} recording(s). "
              "Re-run without --dry-run to download.")
        return

    # --- Step 4: download ---
    os.makedirs(args.output_dir, exist_ok=True)
    log_path = os.path.join(args.output_dir, "download-log.txt")
    log_lines = []
    dl_ok = dl_skip = dl_fail = 0
    rec_ok = rec_skip = 0

    for e in all_entries:
        date_str = e["date"].strftime("%Y-%m-%d")
        board_slug = slugify(e["board"])
        month_dir = os.path.join(args.output_dir, e["date"].strftime("%Y-%m"))
        os.makedirs(month_dir, exist_ok=True)

        print(f"[{date_str}] {e['board']} — {e['name']}")

        for doc_type, url, flag in [
            ("agenda",  e["agenda_url"],  want_agendas),
            ("packet",  e["packet_url"],  want_packets),
            ("minutes", e["minutes_url"], want_minutes),
        ]:
            if not flag or not url:
                continue

            # Google Drive download URLs end in /uc (no extension); fall back to .pdf
            ext = os.path.splitext(url.split("?")[0].split("#")[0])[1].lower() or ".pdf"
            dest = os.path.join(month_dir, f"{date_str}-{board_slug}-{doc_type}{ext}")
            label = os.path.basename(dest)

            if os.path.exists(dest):
                print(f"  skip (exists)  {label}")
                dl_skip += 1
            else:
                print(f"  downloading    {label}")
                if download_binary(url, dest):
                    dl_ok += 1
                    log_lines.append(
                        f"{datetime.datetime.now().isoformat()}  OK      {dest}")
                else:
                    dl_fail += 1
                    log_lines.append(
                        f"{datetime.datetime.now().isoformat()}  FAIL    {url}")
                    if os.path.exists(dest):
                        os.remove(dest)
                time.sleep(DELAY_SECONDS)

        # video_url — YouTube or other streaming link, save as shortcut
        if not args.no_video and e["video_url"]:
            rec_dir = os.path.join(args.output_dir, "recordings")
            os.makedirs(rec_dir, exist_ok=True)
            rec_fname = f"{date_str}-{board_slug}-recording.url"
            rec_path = os.path.join(rec_dir, rec_fname)
            if os.path.exists(rec_path):
                rec_skip += 1
            else:
                save_url_shortcut(e["video_url"], rec_path)
                print(f"  saved          recordings/{rec_fname}")
                rec_ok += 1
                log_lines.append(
                    f"{datetime.datetime.now().isoformat()}  URL     {rec_path}")

    # Revize recording sub-pages + BOE YouTube channel shortcuts
    if not args.no_video:
        rec_dir = os.path.join(args.output_dir, "recordings")
        os.makedirs(rec_dir, exist_ok=True)
        for r in all_recordings:
            date_str = r["date"].strftime("%Y-%m-%d")
            board_slug = slugify(r["board"])
            rec_fname = f"{date_str}-{board_slug}-recording.url"
            rec_path = os.path.join(rec_dir, rec_fname)
            if os.path.exists(rec_path):
                rec_skip += 1
            else:
                save_url_shortcut(r["recording_url"], rec_path)
                print(f"[{date_str}] {r['board']}")
                print(f"  saved          recordings/{rec_fname}")
                rec_ok += 1
                log_lines.append(
                    f"{datetime.datetime.now().isoformat()}  URL     {rec_path}")

        # Parks & Recreation Commission (standing shortcut)
        if parks_rec_shortcut:
            parks_path = os.path.join(rec_dir, "parks-rec-commission.url")
            if not os.path.exists(parks_path):
                save_url_shortcut(parks_rec_shortcut, parks_path)
                print(f"Parks & Rec: saved recordings/parks-rec-commission.url")
                log_lines.append(
                    f"{datetime.datetime.now().isoformat()}  URL     {parks_path}")

    if log_lines:
        with open(log_path, "a") as f:
            f.write("\n".join(log_lines) + "\n")

    print()
    print(f"Documents:  Downloaded {dl_ok}  Skipped {dl_skip}  Failed {dl_fail}")
    print(f"Recordings: Saved {rec_ok}  Skipped {rec_skip}")
    if dl_ok + dl_skip + rec_ok:
        print(f"Files in:   {args.output_dir}")
    if log_lines:
        print(f"Log:        {log_path}")


if __name__ == "__main__":
    main()


# --- Tips ---
#
# 1. Preview without downloading:
#    python3 scripts/download-ridgefield-agendas.py --dry-run
#
# 2. Narrow to one board:
#    python3 scripts/download-ridgefield-agendas.py --board "Selectpersons"
#    python3 scripts/download-ridgefield-agendas.py --board "Planning"
#    python3 scripts/download-ridgefield-agendas.py --board "Education"
#    python3 scripts/download-ridgefield-agendas.py --board "Parks"
#
# 3. Change the lookback window:
#    python3 scripts/download-ridgefield-agendas.py --days 7
#
# 4. Save files somewhere else:
#    python3 scripts/download-ridgefield-agendas.py --output-dir ~/Downloads/ridgefield
#
# 5. Agendas only:
#    python3 scripts/download-ridgefield-agendas.py --no-minutes --no-packets
#
# 6. Skip recording shortcuts:
#    python3 scripts/download-ridgefield-agendas.py --no-video
#
# 7. Run on a schedule (cron — 7 AM daily):
#    0 7 * * * cd /path/to/repo && python3 scripts/download-ridgefield-agendas.py
#
# SITE NOTES:
#   Revize CMS: ridgefieldct.gov (not .org — .org is Cloudflare-blocked).
#   Video column in document center is always empty; recordings are on sub-pages:
#     meeting_audios.php   — Board of Selectpersons (Vimeo 2020-21, BoxCast 2020-22)
#     audio_recordings.php — Planning & Zoning (Zoom 2026), Inland Wetlands (Zoom 2026)
#   All Revize hrefs resolve to site root, not the page directory.
#
#   BOE documents are on Google Drive (publicly shared, no authentication).
#   BOE PDF index requires pdftohtml (poppler-utils):
#     sudo apt install poppler-utils
#   BOE YouTube channel includes committee meetings not in the PDF index.
#
#   Parks & Recreation Commission site blocks bots (Cloudflare 403).
#   A .url shortcut (recordings/parks-rec-commission.url) is saved for manual review.
