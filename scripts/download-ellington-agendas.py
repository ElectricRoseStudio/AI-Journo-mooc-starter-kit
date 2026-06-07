#!/usr/bin/env python3
# download-ellington-agendas.py
# Download municipal meeting agendas, minutes, and Zoom recordings from Ellington CT
# for meetings whose date falls within the past N days (and up to 7 days ahead).
#
# USAGE:
#   python3 scripts/download-ellington-agendas.py [options]
#
# REQUIREMENTS:
#   - Python 3.6+  (no third-party packages needed)
#   - yt-dlp installed (for Zoom video downloads only): pip install yt-dlp
#   - Internet connection
#
# WHAT IT DOES:
#   1. Fetches the Ellington CT agendas-and-minutes page and discovers all 34
#      boards/committees with their Finalsite collection IDs
#   2. Fetches each board's agenda and minutes collection from the Finalsite API
#   3. Parses meeting dates from document titles ("Month DD, YYYY Description")
#      and filters to the date window
#   4. Downloads PDFs to beat-archive/ellington-agendas/YYYY-MM/
#   5. Fetches Board of Selectmen and Board of Finance dedicated pages for
#      Zoom recording links, filters to the date window
#   6. Attempts to download Zoom recordings with yt-dlp
#   7. Appends a download log to beat-archive/ellington-agendas/download-log.txt
#
# SITE STRUCTURE:
#   CMS: Finalsite (www.ellington-ct.gov)
#
#   Agendas hub: /government/agendas-and-minutes
#     - Each board is an accordion panel with a Finalsite tab element
#     - Document lists loaded via AJAX: GET /fs/elements/{element_id}
#                                         ?resource_collection={collection_id}
#     - PDFs hosted at: https://resources.finalsite.net/images/v{version}/...
#     - Document titles include the meeting date: "Month DD, YYYY Description"
#
#   Zoom recordings:
#     Board of Selectmen:  /government/bos
#     Board of Finance:    /government/board-of-finance
#     Recordings linked as: href="https://us02web.zoom.us/rec/share/TOKEN"
#     Date in link text: "Month DDth Description" (current year, no year shown)
#                     or "Month DD, YYYY Description" (prior years, year shown)
#
# BOARDS (34 total):
#   Ad Hoc Committee - Comprehensive Lighting Project
#   Ad Hoc Committee on Diversity and Inclusion
#   Ad Hoc Emergency Services Committee
#   Board of Assessment Appeals
#   Board of Finance, Board of Selectmen
#   BOS Personnel Committee, BOS Personnel Policies Committee
#   Conservation Commission
#   Council for Developing Positive Youth Culture (Ad Hoc)
#   Crystal Lake Milfoil Committee (Ad Hoc)
#   Design Review Board, Economic Development Commission
#   Ellington Beautification Committee, Ellington Trails Committee
#   Emergency Management Advisory Council, Ethics Commission
#   Hall Memorial Library Board of Trustees, Housing Authority
#   Human Services Commission, Inland Wetlands Agency
#   Insurance Advisory Board, Opioid Settlement Committee
#   Parks and Recreation Commission, Patriotic Committee
#   Permanent Building Committee, Planning & Zoning Commission
#   Shared Services Commission, Town Meeting, Town Ordinance Committee
#   Town Policies Committee, Tree Warden
#   Water Pollution Control Authority, Zoning Board of Appeals

import argparse
import datetime
import html as html_module
import os
import re
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

# --- Configuration ---
BASE_URL = "https://www.ellington-ct.gov"
AGENDAS_HUB = f"{BASE_URL}/government/agendas-and-minutes"
BOS_PAGE = f"{BASE_URL}/government/bos"
BOF_PAGE = f"{BASE_URL}/government/board-of-finance"
ELEMENTS_API = f"{BASE_URL}/fs/elements"
OUTPUT_DIR = "beat-archive/ellington-agendas"
DAYS_BACK = 4
DAYS_AHEAD = 7
API_DELAY = 0.25

UA = "Mozilla/5.0 (X11; Linux x86_64; rv:120.0) Gecko/20100101 Firefox/120.0"

# Month name → number
_MONTHS = {
    "january": 1, "february": 2, "march": 3, "april": 4, "may": 5, "june": 6,
    "july": 7, "august": 8, "september": 9, "october": 10, "november": 11, "december": 12,
}
# "April 16, 2026" or "April 16th, 2026" or "April 16th 2026"
_DATE_YEAR_RE = re.compile(
    r"\b(january|february|march|april|may|june|july|august|september|october|november|december)"
    r"\s+(\d{1,2})(?:st|nd|rd|th)?[,\s]+(\d{4})\b",
    re.IGNORECASE,
)
# "April 13th" (ordinal, no year — used for current-year BOS recordings)
_DATE_ORDINAL_RE = re.compile(
    r"\b(january|february|march|april|may|june|july|august|september|october|november|december)"
    r"\s+(\d{1,2})(?:st|nd|rd|th)\b",
    re.IGNORECASE,
)


# --- HTTP helpers ---

def _request(url, params=None, ajax=False):
    """Build and return a urllib Request."""
    if params:
        url += "?" + urllib.parse.urlencode(params)
    headers = {"User-Agent": UA, "Accept": "text/html,*/*"}
    if ajax:
        headers["X-Requested-With"] = "XMLHttpRequest"
    return urllib.request.Request(url, headers=headers)


def fetch_html(url, params=None, ajax=False):
    """GET url and return decoded HTML, or None on error."""
    req = _request(url, params=params, ajax=ajax)
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            raw = r.read()
            charset = r.headers.get_content_charset() or "utf-8"
            return raw.decode(charset, errors="replace")
    except urllib.error.HTTPError as e:
        print(f"  HTTP {e.code} — {req.full_url}", file=sys.stderr)
        return None
    except Exception as e:
        print(f"  ERROR fetching {req.full_url}: {e}", file=sys.stderr)
        return None


def download_file(url, dest_path):
    """Download url to dest_path. Returns True on success."""
    req = urllib.request.Request(
        url, headers={"User-Agent": UA, "Accept": "application/pdf,*/*"}
    )
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            with open(dest_path, "wb") as f:
                f.write(r.read())
        return True
    except Exception as e:
        print(f"  WARNING: {e}", file=sys.stderr)
        return False


# --- Date helpers ---

def parse_doc_date(title):
    """
    Parse date from a Finalsite document title like "March 9, 2026 Regular Meeting".
    Returns datetime.date or None.
    """
    m = _DATE_YEAR_RE.search(title)
    if not m:
        return None
    try:
        return datetime.date(int(m.group(3)), _MONTHS[m.group(1).lower()], int(m.group(2)))
    except (ValueError, KeyError):
        return None


def parse_recording_date(text, today):
    """
    Parse date from a Zoom recording link label.
    Handles "April 13, 2026 Regular Meeting" (with year) and
    "April 13th Regular Meeting" (no year — assumes current year).
    Returns datetime.date or None.
    """
    m = _DATE_YEAR_RE.search(text)
    if m:
        try:
            return datetime.date(int(m.group(3)), _MONTHS[m.group(1).lower()], int(m.group(2)))
        except (ValueError, KeyError):
            return None
    m2 = _DATE_ORDINAL_RE.search(text)
    if m2:
        try:
            return datetime.date(today.year, _MONTHS[m2.group(1).lower()], int(m2.group(2)))
        except (ValueError, KeyError):
            return None
    return None


# --- Finalsite board/collection discovery ---

def parse_board_map(hub_html):
    """
    Parse the agendas-and-minutes hub page to extract board info.

    Returns a list of dicts:
      {name, element_id, agenda_col, minutes_col}

    element_id is the Finalsite element ID for the resource list element.
    agenda_col / minutes_col are the Finalsite collection IDs (or None if absent).
    """
    # Extract (panel_id, board_name) from the tab navigation
    tabs = re.findall(r"href='#fs-panel-(\d+)'[^>]*>([^<]+)</a>", hub_html)
    seen = set()
    panels = []
    for pid, name in tabs:
        if pid not in seen:
            seen.add(pid)
            panels.append((pid, html_module.unescape(name.strip())))

    boards = []
    for i, (panel_id, board_name) in enumerate(panels):
        p_start = hub_html.find(f'id="fs-panel-{panel_id}"')
        if p_start < 0:
            continue
        # End of this panel = start of next panel
        p_end = len(hub_html)
        for j in range(i + 1, len(panels)):
            nxt = hub_html.find(f'id="fs-panel-{panels[j][0]}"', p_start + 1)
            if nxt > p_start:
                p_end = min(p_end, nxt)
                break
        section = hub_html[p_start:p_end]

        # Nested resource-list element ID
        nested = re.findall(
            r'id="fsEl_(\d+)"[^>]+data-source-element-id="\d+"', section
        )
        el_id = nested[0] if nested else None

        # Collection IDs by type label
        cols = re.findall(
            r'data-resource-collection-id="(\d+)"[^>]*href="#">([^<]+)</a>', section
        )
        agenda_col = next((c for c, n in cols if n.strip() == "Agendas"), None)
        minutes_col = next((c for c, n in cols if n.strip() == "Minutes"), None)

        if el_id and (agenda_col or minutes_col):
            boards.append({
                "name": board_name,
                "element_id": el_id,
                "agenda_col": agenda_col,
                "minutes_col": minutes_col,
            })

    return boards


def fetch_collection_docs(el_id, col_id):
    """
    Fetch the document list for a Finalsite resource collection.

    Calls GET /fs/elements/{el_id}?resource_collection={col_id}
    Returns list of {title, url}.
    """
    html = fetch_html(f"{ELEMENTS_API}/{el_id}", params={"resource_collection": col_id}, ajax=True)
    if not html:
        return []
    docs = []
    for m in re.finditer(r"<a\b([^>]+)>", html):
        attrs = m.group(1)
        title_m = re.search(r'data-resource-title="([^"]+)"', attrs)
        href_m = re.search(r'href="(https://resources\.finalsite\.net[^"]+)"', attrs)
        if title_m and href_m:
            docs.append({
                "title": html_module.unescape(title_m.group(1)),
                "url": href_m.group(1),
            })
    return docs


# --- Zoom recording discovery ---

def fetch_zoom_recordings(page_url, board_name, cutoff, future_limit, today):
    """
    Scrape Zoom recording links from a board's dedicated page.
    Returns list of {board, date, title, url}.
    """
    html = fetch_html(page_url)
    if not html:
        return []
    zoom_pat = re.compile(
        r'href="(https?://(?:[\w-]+\.)?zoom\.us/rec/[^"]+)"[^>]*>([^<]+)</a>',
        re.IGNORECASE,
    )
    recordings = []
    seen_urls = set()
    for m in zoom_pat.finditer(html):
        url = m.group(1)
        text = m.group(2).strip()
        if url in seen_urls:
            continue
        seen_urls.add(url)
        d = parse_recording_date(text, today)
        if d and cutoff <= d <= future_limit:
            recordings.append({
                "board": board_name,
                "date": d,
                "title": text,
                "url": url,
            })
    return recordings


# --- Zoom download helpers ---

def zoom_archive_key(url):
    """Return a short unique key for a Zoom recording URL."""
    # Use the last path segment of the URL path (the share token)
    return re.sub(r"[?#].*$", "", url).rstrip("/").split("/")[-1]


def is_in_zoom_archive(archive_path, url):
    """Return True if this Zoom recording is already in the download archive."""
    if not os.path.exists(archive_path):
        return False
    key = zoom_archive_key(url)
    with open(archive_path) as f:
        return any(key in line for line in f)


def download_zoom_recording(url, title, rec_date, board, output_dir, archive_path):
    """
    Download a Zoom recording with yt-dlp.
    Returns 'downloaded', 'skipped', or 'failed'.
    """
    month_dir = os.path.join(output_dir, rec_date.strftime("%Y-%m"))
    os.makedirs(month_dir, exist_ok=True)
    date_str = rec_date.strftime("%Y-%m-%d")
    board_slug = slugify(board, max_len=40)
    title_slug = slugify(title, max_len=50)
    outtmpl = os.path.join(month_dir, f"{date_str}-{board_slug}-{title_slug}.%(ext)s")

    cmd = [
        "yt-dlp",
        "--no-playlist",
        "--merge-output-format", "mp4",
        "-o", outtmpl,
        "-q", "--no-warnings",
        url,
    ]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=3600)
        if result.returncode == 0:
            # Mark as archived
            with open(archive_path, "a") as af:
                af.write(f"zoom {zoom_archive_key(url)}  {url}\n")
            return "downloaded"
        # yt-dlp might fail if Zoom requires passcode — report as failed
        if result.stderr:
            print(f"  NOTE: {result.stderr.strip()[:120]}", file=sys.stderr)
        return "failed"
    except subprocess.TimeoutExpired:
        print(f"  WARNING: yt-dlp timed out for {url}", file=sys.stderr)
        return "failed"
    except FileNotFoundError:
        print(
            "  ERROR: yt-dlp not found. Install with: pip install yt-dlp",
            file=sys.stderr,
        )
        return "failed"


# --- Utilities ---

def slugify(text, max_len=55):
    text = text.lower().strip()
    text = re.sub(r"[/\\&]", "-", text)
    text = re.sub(r"[^\w\s-]", "", text)
    text = re.sub(r"[\s_]+", "-", text)
    text = re.sub(r"-{2,}", "-", text)
    return text.strip("-")[:max_len]


def strip_date_prefix(title):
    """Remove leading date ('Month DD, YYYY') from a document title."""
    return _DATE_YEAR_RE.sub("", title, count=1).strip(" ,.-")


def make_pdf_path(board, doc_type, meeting_date, title, output_dir):
    date_str = meeting_date.strftime("%Y-%m-%d")
    month_dir = os.path.join(output_dir, meeting_date.strftime("%Y-%m"))
    os.makedirs(month_dir, exist_ok=True)
    board_slug = slugify(board, max_len=40)
    title_slug = slugify(strip_date_prefix(title), max_len=40)
    fname = f"{date_str}-{board_slug}-{doc_type}-{title_slug}.pdf"
    return os.path.join(month_dir, fname)


# --- Main ---

def main():
    parser = argparse.ArgumentParser(
        description=(
            "Download Ellington CT municipal agendas, minutes, and Zoom recordings "
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
        help="Only include boards/recording titles containing NAME (case-insensitive)",
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
        help="Download PDFs only, skip Zoom recordings",
    )
    parser.add_argument(
        "--videos-only", action="store_true",
        help="Download Zoom recordings only, skip PDFs",
    )
    args = parser.parse_args()

    today = datetime.date.today()
    cutoff = today - datetime.timedelta(days=args.days)
    future_limit = today + datetime.timedelta(days=args.ahead)

    print(f"Date window : {cutoff} to {future_limit}")
    print(f"Hub page    : {AGENDAS_HUB}")
    if not args.dry_run:
        print(f"Output dir  : {args.output_dir}")
    print()

    all_docs: list = []
    recordings: list = []

    # --- Step 1: PDFs from Finalsite ---
    if not args.videos_only:
        print("Fetching agendas hub to discover boards and collection IDs...")
        hub_html = fetch_html(AGENDAS_HUB)
        if not hub_html:
            print("ERROR: Could not fetch the hub page.", file=sys.stderr)
            sys.exit(1)

        boards = parse_board_map(hub_html)
        if not boards:
            print(
                "ERROR: No boards found — page structure may have changed.",
                file=sys.stderr,
            )
            sys.exit(1)
        print(f"  Found {len(boards)} board(s).")
        print("Fetching document collections...")

        board_filter = args.board.lower() if args.board else None

        for b in boards:
            if board_filter and board_filter not in b["name"].lower():
                continue

            for col_type, col_id in [("agenda", b["agenda_col"]), ("minutes", b["minutes_col"])]:
                if col_type == "agenda" and args.no_agendas:
                    continue
                if col_type == "minutes" and args.no_minutes:
                    continue
                if not col_id:
                    continue

                docs = fetch_collection_docs(b["element_id"], col_id)
                time.sleep(API_DELAY)

                for doc in docs:
                    d = parse_doc_date(doc["title"])
                    if not d:
                        continue
                    if cutoff <= d <= future_limit:
                        all_docs.append({
                            "board": b["name"],
                            "meeting_date": d,
                            "doc_type": col_type,
                            "title": doc["title"],
                            "url": doc["url"],
                        })

        all_docs.sort(key=lambda x: (x["meeting_date"], x["board"]), reverse=True)
        print(f"  Found {len(all_docs)} document(s) in date window.")
        print()

    # --- Step 2: Zoom recordings from BOS and BOF pages ---
    if not args.docs_only:
        # Recordings must be past events — clamp future limit to today
        rec_future_limit = min(future_limit, today)
        print("Fetching BOS and BOF pages for Zoom recordings...")
        for page_url, board_label in [
            (BOS_PAGE, "Board of Selectmen"),
            (BOF_PAGE, "Board of Finance"),
        ]:
            if board_filter := (args.board.lower() if args.board else None):
                if board_filter not in board_label.lower():
                    continue
            recs = fetch_zoom_recordings(page_url, board_label, cutoff, rec_future_limit, today)
            recordings.extend(recs)
        recordings.sort(key=lambda x: (x["date"], x["board"]), reverse=True)
        print(f"  Found {len(recordings)} Zoom recording(s) in window.")
        print()

    if not all_docs and not recordings:
        print("No documents or recordings found in the date window.")
        return

    # --- Dry-run listing ---
    if args.dry_run:
        if all_docs:
            print(f"{'Board':<48} {'Date':<12} Type")
            print("-" * 75)
            for d in all_docs:
                print(
                    f"{d['board'][:47]:<48} "
                    f"{d['meeting_date']!s:<12} "
                    f"{d['doc_type']}"
                )
            print()
        if recordings:
            print(f"{'Board':<24} {'Date':<12} Title")
            print("-" * 80)
            for r in recordings:
                print(
                    f"{r['board'][:23]:<24} "
                    f"{r['date']!s:<12} "
                    f"{r['title'][:42]}"
                )
            print()
        total = len(all_docs) + len(recordings)
        print(f"{total} item(s) matched. Re-run without --dry-run to download.")
        return

    # --- Step 3: Download PDFs ---
    os.makedirs(args.output_dir, exist_ok=True)
    log_path = os.path.join(args.output_dir, "download-log.txt")
    log_lines = []
    downloaded = skipped = failed = 0

    if all_docs:
        for d in all_docs:
            dest = make_pdf_path(
                d["board"], d["doc_type"], d["meeting_date"],
                d["title"], args.output_dir,
            )
            label = os.path.basename(dest)
            if os.path.exists(dest):
                print(f"  skip (exists)  {label}")
                skipped += 1
                continue

            print(f"  [{d['meeting_date']}] {d['board'][:50]} — {d['doc_type']}")
            print(f"  downloading    {label}")
            if download_file(d["url"], dest):
                downloaded += 1
                log_lines.append(
                    f"{datetime.datetime.now().isoformat()}  OK       {dest}"
                )
            else:
                failed += 1
                log_lines.append(
                    f"{datetime.datetime.now().isoformat()}  FAILED   {d['url']}"
                )
                if os.path.exists(dest):
                    os.remove(dest)
            time.sleep(API_DELAY)
        print()

    # --- Step 4: Download Zoom recordings ---
    if recordings:
        archive_path = os.path.join(args.output_dir, "zoom-archive.txt")
        print(f"Downloading {len(recordings)} Zoom recording(s)...")
        print("  NOTE: Zoom share links may require a passcode; yt-dlp may not succeed.")

        for rec in recordings:
            print(f"  [{rec['date']}] {rec['board']} — {rec['title']}")
            if is_in_zoom_archive(archive_path, rec["url"]):
                print(f"  skip (archive)")
                skipped += 1
                continue

            print(f"  downloading    {rec['url'][:80]}")
            status = download_zoom_recording(
                rec["url"], rec["title"], rec["date"],
                rec["board"], args.output_dir, archive_path,
            )
            if status == "downloaded":
                downloaded += 1
                log_lines.append(
                    f"{datetime.datetime.now().isoformat()}  OK       zoom:{rec['url']}"
                )
            else:
                failed += 1
                log_lines.append(
                    f"{datetime.datetime.now().isoformat()}  FAILED   zoom:{rec['url']}"
                )
        print()

    if log_lines:
        with open(log_path, "a") as f:
            f.write("\n".join(log_lines) + "\n")

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
#    python3 scripts/download-ellington-agendas.py --dry-run
#
# 2. Narrow to one board:
#    python3 scripts/download-ellington-agendas.py --board "Board of Selectmen"
#
# 3. PDFs only (no Zoom downloads):
#    python3 scripts/download-ellington-agendas.py --docs-only
#
# 4. Zoom recordings only:
#    python3 scripts/download-ellington-agendas.py --videos-only
#
# 5. Agendas only (skip minutes):
#    python3 scripts/download-ellington-agendas.py --no-minutes
#
# 6. Change the lookback window:
#    python3 scripts/download-ellington-agendas.py --days 14
#
# 7. Run on a schedule (cron — 7 AM daily):
#    0 7 * * * cd /path/to/repo && python3 scripts/download-ellington-agendas.py
#
# NOTES:
#   - Ellington uses Finalsite CMS. Documents are loaded via the Finalsite
#     element API: GET /fs/elements/{element_id}?resource_collection={col_id}
#     Board/collection IDs are discovered dynamically from the hub page, so
#     new boards added to the site will be picked up automatically.
#   - Document dates are parsed from titles in the format "Month DD, YYYY Description"
#     (e.g. "April 13, 2026 Regular Meeting"). Documents whose titles don't
#     include a parseable date are silently skipped.
#   - Zoom recordings are linked only on the Board of Selectmen (/government/bos)
#     and Board of Finance (/government/board-of-finance) dedicated pages.
#     Other boards do not appear to post Zoom recordings.
#   - BOS Zoom links for the current year use ordinal dates without a year
#     ("April 13th Regular Meeting"). These are assumed to be in the current
#     calendar year when date-window filtering is applied.
#   - Zoom recordings with /rec/share/ URLs may be passcode-protected.
#     yt-dlp will attempt a download; if it fails, the URL is logged so you
#     can access the recording manually in a browser.
#   - yt-dlp writes a zoom-archive.txt file so recordings are not retried
#     unnecessarily on subsequent runs.
