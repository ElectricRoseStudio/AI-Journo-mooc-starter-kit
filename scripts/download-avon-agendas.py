#!/usr/bin/env python3
# download-avon-agendas.py
# Download municipal meeting agendas, minutes, and video recordings from Avon CT.
#
# USAGE:
#   python3 scripts/download-avon-agendas.py [options]
#
# REQUIREMENTS:
#   - Python 3.6+  (no third-party packages needed)
#   - Internet connection
#
# WHAT IT DOES:
#   1. Fetches avonct.gov/government/boards_committees/index.php to discover boards
#   2. Fetches each board's agendas_minutes.php page
#   3. Parses table rows to extract meeting date + agenda/minutes links + video links
#   4. Filters to documents whose meeting date falls within the date window
#   5. Downloads PDFs to beat-archive/avon-agendas/YYYY-MM/
#   6. Saves GoToMeeting recording URLs as .url shortcut files (openable in any browser)
#   7. Appends a download log to beat-archive/avon-agendas/download-log.txt
#
# SITE STRUCTURE (Revize CMS):
#   Index:  https://www.avonct.gov/government/boards_committees/index.php
#   Board:  https://www.avonct.gov/government/boards_committees/{slug}/agendas_minutes.php
#
# DOCUMENT LINKS — two formats appear on the same page:
#   Full path:  Documents/Government/Boards and Committees/{Board}/Agendas and Minutes/
#                {YYYY}/{Type}/{filename}.pdf  (2025 and older, no ?t= required)
#   Short path: {PREFIX}/{filename}.pdf?t={timestamp}  (2026+, require ?t= to distinguish
#                from nav links; prefix = IWC, ZBA, Planning, etc.)
#
# VIDEO RECORDINGS (GoToMeeting):
#   Town Council, Planning & Zoning, Affordable Housing, and Clean Energy Commission
#   post GoToMeeting transcript links alongside each meeting's agenda/minutes.
#   These are web-based recordings (not downloadable files). The script saves each
#   as a .url Internet Shortcut file that can be opened in any browser.
#   Note: GoToMeeting recordings expire after ~90 days per site policy.
#   Use --no-recordings to skip them.
#
# NOTE: No Cloudflare or JavaScript challenge — plain urllib requests work.

import argparse
import datetime
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

# --- Configuration ---
BASE_URL = "https://www.avonct.gov"
INDEX_URL = f"{BASE_URL}/government/boards_committees/index.php"
BOARD_PAGE_PATTERN = "government/boards_committees/{slug}/agendas_minutes.php"
OUTPUT_DIR = "beat-archive/avon-agendas"
DAYS_BACK = 4
DAYS_AHEAD = 7
PAGE_DELAY = 0.5
DOWNLOAD_DELAY = 0.8

UA = "Avon-CT-Agendas-Downloader/1.0 (journalism research)"

# Matches slug-style board paths in the index page
_BOARD_SLUG_RE = re.compile(
    r'href="government/boards_committees/([a-z0-9_]+)/index\.php"',
    re.IGNORECASE,
)

# Full-path documents: Documents/Government/Boards and Committees/...
# These may or may not have a ?t= cache-busting timestamp.
_FULL_DOC_RE = re.compile(
    r'href="(Documents/Government/Boards[^"]*\.(?:pdf|docx?)[^"]*)"',
    re.IGNORECASE,
)

# Short-path CMS documents (2026+): require ?t= to exclude static nav links.
# Covers IWC/..., ZBA/..., Planning/..., and bare filenames like "May19_2026.pdf".
# Excludes Documents/Departments/ (site-wide nav links) and external URLs.
_SHORT_DOC_RE = re.compile(
    r'href="((?!https?://)(?!Documents/)[^"]*\.(?:pdf|docx?)\?t=\d{8,}[^"]*)"',
    re.IGNORECASE,
)

# GoToMeeting transcript links (video recordings)
_GOTOMEETING_RE = re.compile(
    r'href="(https://transcripts\.gotomeeting\.com/[^"]+)"',
    re.IGNORECASE,
)

# Month name → int
_MONTH_NAMES = {
    "jan": 1, "january": 1,
    "feb": 2, "february": 2,
    "mar": 3, "march": 3,
    "apr": 4, "april": 4,
    "may": 5,
    "jun": 6, "june": 6,
    "jul": 7, "july": 7,
    "aug": 8, "august": 8,
    "sep": 9, "sept": 9, "september": 9,
    "oct": 10, "october": 10,
    "nov": 11, "november": 11,
    "dec": 12, "december": 12,
}

# MM/DD/YY or MM/DD/YYYY (as used in table date cells: "05/07/26")
_DATE_MDY_RE = re.compile(
    r'(?<![/\d])(\d{1,2})[./](\d{1,2})[./](\d{2}(?:\d{2})?)(?!\d)'
)

# Month-word first: may27_2025, april_17_2025, "March 18, 2026", jan_2025_15
# The day-to-year separator allows multiple chars ("18, 2026" has comma then space).
_MONTH_FIRST_RE = re.compile(
    r'(?<![a-z])(jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|june?|'
    r'july?|aug(?:ust)?|sep(?:t(?:ember)?)?|oct(?:ober)?|nov(?:ember)?|dec(?:ember)?)'
    r'[\s_\-]?(\d{1,2})[\s_\-,]*(\d{4})',
    re.IGNORECASE,
)

# Year-month-day: 2026_May5, 2025_may_13, 2025_03_05
# Separator between month and day is optional to handle e.g. "2026_May5".
_YEAR_FIRST_RE = re.compile(
    r'(?<!\d)(20\d{2})[\s_\-]'
    r'(jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|june?|'
    r'july?|aug(?:ust)?|sep(?:t(?:ember)?)?|oct(?:ober)?|nov(?:ember)?|dec(?:ember)?|'
    r'0?[1-9]|1[0-2])'
    r'[\s_\-]?(\d{1,2})(?!\d)',
    re.IGNORECASE,
)


# --- HTTP helpers ---

def fetch_html(url, retries=2):
    req = urllib.request.Request(
        url,
        headers={"User-Agent": UA, "Accept": "text/html,*/*"},
    )
    for attempt in range(retries + 1):
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                charset = r.headers.get_content_charset() or "utf-8"
                return r.read().decode(charset, errors="replace")
        except urllib.error.HTTPError as e:
            if e.code == 404:
                return None
            print(f"  HTTP {e.code} fetching {url}", file=sys.stderr)
            if attempt < retries:
                time.sleep(1)
        except urllib.error.URLError as e:
            print(f"  ERROR fetching {url}: {e}", file=sys.stderr)
            if attempt < retries:
                time.sleep(1)
    return None


def download_pdf(url, dest_path):
    """Download url → dest_path, following redirects. Returns True on success."""
    # Encode spaces and other non-ASCII characters in the path portion of the URL
    parsed = urllib.parse.urlsplit(url)
    encoded_path = urllib.parse.quote(parsed.path, safe="/:@!$&'()*+,;=")
    encoded_query = urllib.parse.quote(parsed.query, safe="=&+%")
    safe_url = urllib.parse.urlunsplit(parsed._replace(path=encoded_path, query=encoded_query))
    req = urllib.request.Request(
        safe_url,
        headers={"User-Agent": UA, "Accept": "application/pdf, */*"},
    )
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            with open(dest_path, "wb") as f:
                f.write(r.read())
        return True
    except Exception as e:
        print(f"  WARNING: {e}", file=sys.stderr)
        return False


def save_gotomeeting_url(recording_url, dest_path):
    """Save a GoToMeeting URL as a Windows .url Internet Shortcut (openable in any browser)."""
    try:
        with open(dest_path, "w") as f:
            f.write(f"[InternetShortcut]\nURL={recording_url}\n")
        return True
    except Exception as e:
        print(f"  WARNING: {e}", file=sys.stderr)
        return False


# --- Board discovery ---

def discover_boards(index_html):
    """Return list of board slug strings from the boards index page."""
    slugs = []
    seen = set()
    for m in _BOARD_SLUG_RE.finditer(index_html):
        slug = m.group(1).lower()
        if slug not in seen:
            seen.add(slug)
            slugs.append(slug)
    return slugs


# --- Document parsing ---

def slug_to_board_name(slug):
    """Convert a URL slug to a display name: 'town_council' → 'Town Council'."""
    return slug.replace("_", " ").title()


def board_name_from_path(doc_path, fallback=None):
    """
    Extract the board name from a full-path document URL.
    Returns fallback (or 'Unknown Board') for short-path docs.
    """
    m = re.search(
        r"Boards\s+and\s+Committees/([^/]+)/(?:Agendas?\s+and\s+Minutes|Approved)",
        doc_path,
        re.IGNORECASE,
    )
    if m:
        return m.group(1).strip()
    return fallback or "Unknown Board"


def classify_doc_type(doc_path):
    """
    Fallback classification from URL path when link text isn't 'Agenda'/'Minutes'.
    Returns 'agenda', 'minutes', or 'other'.
    """
    lower = doc_path.lower()
    if re.search(r"/agendas?/", lower):
        return "agenda"
    if "/minutes/" in lower:
        return "minutes"
    fname = os.path.basename(lower.split("?")[0])
    if re.search(r"\bagenda\b|\ba\s+\d", fname):
        return "agenda"
    if re.search(r"(?:^|[_\s\-])minutes", fname):
        return "minutes"
    return "other"


def extract_year_from_path(doc_path):
    """Return the four-digit year from the URL path (e.g. …/2026/…), or None."""
    m = re.search(r"/(\d{4})/", doc_path)
    if m:
        year = int(m.group(1))
        if 2000 <= year <= 2040:
            return year
    return None


def parse_date_from_cell(text):
    """
    Parse a meeting date from a table cell like '05/07/26\nRegular Meeting'.
    Handles MM/DD/YY and MM/DD/YYYY formats. Returns datetime.date or None.
    """
    m = _DATE_MDY_RE.search(text)
    if m:
        mm, dd = int(m.group(1)), int(m.group(2))
        yy_s = m.group(3)
        yy = int(yy_s)
        yyyy = 2000 + yy if len(yy_s) == 2 else yy
        if 1 <= mm <= 12 and 1 <= dd <= 31 and 2000 <= yyyy <= 2040:
            try:
                return datetime.date(yyyy, mm, dd)
            except ValueError:
                pass
    return None


def parse_date_from_filename(filename):
    """
    Best-effort date extraction from a PDF filename.
    Tries multiple patterns; returns datetime.date or None.
    """
    stem = os.path.splitext(filename.split("?")[0])[0]

    # Try month-name-first: may27_2025, april_17_2025
    for m in _MONTH_FIRST_RE.finditer(stem):
        month_word = m.group(1).lower()[:3]
        month = _MONTH_NAMES.get(month_word)
        if not month:
            continue
        day = int(m.group(2))
        year = int(m.group(3))
        if 1 <= day <= 31 and 2000 <= year <= 2040:
            try:
                return datetime.date(year, month, day)
            except ValueError:
                pass

    # Try year-first: 2026_May5, 2025_may_13, 2025_03_05
    for m in _YEAR_FIRST_RE.finditer(stem):
        year = int(m.group(1))
        month_part = m.group(2)
        day = int(m.group(3))
        if month_part.isdigit():
            month = int(month_part)
        else:
            month = _MONTH_NAMES.get(month_part.lower()[:3])
        if not month:
            continue
        if 1 <= day <= 31 and 1 <= month <= 12:
            try:
                return datetime.date(year, month, day)
            except ValueError:
                pass

    # Try numeric: "05 07 26", "3.23.26", "1.16.2025"
    for m in _DATE_MDY_RE.finditer(stem):
        mm, dd = int(m.group(1)), int(m.group(2))
        yy_s = m.group(3)
        yy = int(yy_s)
        yyyy = 2000 + yy if len(yy_s) == 2 else yy
        if 1 <= mm <= 12 and 1 <= dd <= 31 and 2000 <= yyyy <= 2040:
            try:
                return datetime.date(yyyy, mm, dd)
            except ValueError:
                pass

    return None


def parse_board_page(html_text, board_slug):
    """
    Extract agenda/minutes docs and GoToMeeting video links from a board page.

    Uses row-based table parsing: meeting dates come from the first <td> of each
    row (format MM/DD/YY), and link text ("Agenda"/"Minutes"/"Video") determines
    document type. This correctly handles both the old full-path format and the
    newer short-path format (IWC/..., ZBA/..., Planning/...) used since 2026.

    Returns list of dicts: {board, slug, doc_type, year, date, href, filename,
                             is_gotomeeting}.
    """
    results = []
    seen_paths = set()

    # Start from the agenda section marker to skip site-wide nav links
    start = html_text.find("Agenda Minutes List Starts")
    content = html_text[start:] if start >= 0 else html_text

    fallback_name = slug_to_board_name(board_slug)

    for row_m in re.finditer(r"<tr\b[^>]*>(.*?)</tr>", content, re.DOTALL | re.IGNORECASE):
        row = row_m.group(1)

        # Get meeting date from the first <td> (e.g. "05/07/26\nRegular Meeting")
        first_cell = re.search(r"<td\b[^>]*>(.*?)</td>", row, re.DOTALL | re.IGNORECASE)
        row_date = None
        if first_cell:
            cell_text = re.sub(r"<[^>]+>", "", first_cell.group(1)).strip()
            row_date = parse_date_from_cell(cell_text)

        # Process each anchor link in the row
        for link_m in re.finditer(
            r'href="([^"]+)"[^>]*>([^<]*)</a>', row, re.IGNORECASE
        ):
            href = link_m.group(1).strip()
            link_text = link_m.group(2).strip()

            # GoToMeeting video recording
            if "transcripts.gotomeeting.com" in href:
                if href not in seen_paths:
                    seen_paths.add(href)
                    results.append({
                        "board": fallback_name,
                        "slug": board_slug,
                        "doc_type": "recording",
                        "year": row_date.year if row_date else None,
                        "date": row_date,
                        "href": href,
                        "filename": None,
                        "is_gotomeeting": True,
                    })
                continue

            # Skip external links
            if href.startswith("http://") or href.startswith("https://"):
                continue

            # Only PDF/DOC/DOCX files
            if not re.search(r"\.(?:pdf|docx?)(?:\?|$)", href, re.IGNORECASE):
                continue

            # Short-path docs must have ?t= to distinguish from static nav links
            if not re.match(r"Documents/", href, re.IGNORECASE) and "?t=" not in href:
                continue

            # Skip site-wide navigation docs (Documents/Departments/)
            if re.match(r"Documents/Departments/", href, re.IGNORECASE):
                continue

            path_only = href.split("?")[0]
            if path_only in seen_paths:
                continue
            seen_paths.add(path_only)

            year = extract_year_from_path(href)
            filename = os.path.basename(urllib.parse.unquote(path_only))

            # Use link text for doc_type; fall back to path/filename classification
            lt = link_text.lower()
            if lt in ("agenda", "agendas"):
                doc_type = "agenda"
            elif lt in ("minutes", "minute"):
                doc_type = "minutes"
            elif lt == "packet":
                doc_type = "packet"
            else:
                doc_type = classify_doc_type(href)

            # Row date is most reliable; filename date is fallback
            meeting_date = row_date or parse_date_from_filename(filename)

            board = board_name_from_path(href, fallback=fallback_name)

            results.append({
                "board": board,
                "slug": board_slug,
                "doc_type": doc_type,
                "year": year,
                "date": meeting_date,
                "href": href,
                "filename": filename,
                "is_gotomeeting": False,
            })

    # Second pass: catch GoToMeeting links outside table rows (e.g. in <li> lists).
    # Some boards (Clean Energy Commission) post recordings in a meeting-schedule list
    # before the agenda table. Use link text to parse the meeting date.
    for link_m in re.finditer(
        r'href="(https://transcripts\.gotomeeting\.com/[^"]+)"[^>]*>([^<]+)</a>',
        html_text, re.IGNORECASE,
    ):
        href = link_m.group(1).strip()
        if href in seen_paths:
            continue  # already captured by row-based pass
        seen_paths.add(href)
        link_text = link_m.group(2).strip()
        meeting_date = parse_date_from_filename(link_text)
        results.append({
            "board": fallback_name,
            "slug": board_slug,
            "doc_type": "recording",
            "year": meeting_date.year if meeting_date else None,
            "date": meeting_date,
            "href": href,
            "filename": None,
            "is_gotomeeting": True,
        })

    return results


# --- File naming ---

def slugify(text, max_len=55):
    text = text.lower().strip()
    text = re.sub(r"[&/\\]", "-", text)
    text = re.sub(r"\s+-\s+", "-", text)
    text = re.sub(r"[^\w\s-]", "", text)
    text = re.sub(r"[\s_]+", "-", text)
    text = re.sub(r"-{2,}", "-", text)
    return text.strip("-")[:max_len]


def make_dest_path(doc, output_dir, counter=0):
    meeting_date = doc["date"]
    year = doc["year"]

    if meeting_date:
        month_dir = os.path.join(output_dir, meeting_date.strftime("%Y-%m"))
        date_prefix = meeting_date.strftime("%Y-%m-%d")
    elif year:
        month_dir = os.path.join(output_dir, str(year))
        date_prefix = str(year)
    else:
        month_dir = os.path.join(output_dir, "undated")
        date_prefix = "undated"

    os.makedirs(month_dir, exist_ok=True)
    board_slug_name = slugify(doc["board"])
    suffix = f"-{counter}" if counter > 0 else ""

    if doc.get("is_gotomeeting"):
        return os.path.join(
            month_dir, f"{date_prefix}-{board_slug_name}-recording{suffix}.url"
        )

    doc_slug = slugify(os.path.splitext(doc["filename"].split("?")[0])[0])
    ext = os.path.splitext(doc["filename"].split("?")[0])[1].lower() or ".pdf"
    return os.path.join(
        month_dir, f"{date_prefix}-{board_slug_name}-{doc_slug}{suffix}{ext}"
    )


# --- Main ---

def main():
    parser = argparse.ArgumentParser(
        description="Download Avon CT municipal agendas, minutes, and video recordings.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
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
        help="Only fetch boards whose name contains NAME (case-insensitive)",
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
        "--no-recordings", action="store_true",
        help="Skip GoToMeeting video recording links",
    )
    parser.add_argument(
        "--include-other", action="store_true",
        help="Also download files not classified as agenda, minutes, or recording",
    )
    args = parser.parse_args()

    today = datetime.date.today()
    cutoff = today - datetime.timedelta(days=args.days)
    future_limit = today + datetime.timedelta(days=args.ahead)

    print(f"Date window : {cutoff} to {future_limit}")
    print(f"Index page  : {INDEX_URL}")
    if not args.dry_run:
        print(f"Output dir  : {args.output_dir}")
    print()

    # --- Step 1: discover boards ---
    print("Fetching boards index...")
    index_html = fetch_html(INDEX_URL)
    if not index_html:
        print("ERROR: Could not fetch the boards index page.", file=sys.stderr)
        sys.exit(1)

    all_slugs = discover_boards(index_html)
    if not all_slugs:
        print("WARNING: No board slugs found — page structure may have changed.",
              file=sys.stderr)
        sys.exit(1)

    print(f"  Found {len(all_slugs)} board/committee page(s).")

    if args.board:
        filter_str = args.board.lower()
        slug_filtered = [s for s in all_slugs if filter_str in s.lower()]
        if slug_filtered:
            all_slugs = slug_filtered
            print(f"  Pre-filtered to {len(all_slugs)} board(s) by slug match.")

    print()

    # --- Step 2: fetch each board page and collect docs ---
    all_docs = []

    print(f"Fetching board pages ({len(all_slugs)} total)...")
    for i, slug in enumerate(all_slugs, 1):
        board_url = f"{BASE_URL}/{BOARD_PAGE_PATTERN.format(slug=slug)}"
        print(f"  [{i:>2}/{len(all_slugs)}] {slug}...", end=" ", flush=True)

        html_text = fetch_html(board_url)
        if html_text is None:
            print("skipped (no agendas page)")
            time.sleep(PAGE_DELAY)
            continue

        docs = parse_board_page(html_text, slug)

        if docs and args.board:
            board_name = docs[0]["board"].lower()
            if args.board.lower() not in board_name and args.board.lower() not in slug.lower():
                print(f"skipped (board filter: '{docs[0]['board']}')")
                time.sleep(PAGE_DELAY)
                continue

        # Filter by doc type and date window
        in_window = []
        for doc in docs:
            if args.no_agendas and doc["doc_type"] == "agenda":
                continue
            if args.no_minutes and doc["doc_type"] == "minutes":
                continue
            if args.no_recordings and doc["doc_type"] == "recording":
                continue
            if doc["doc_type"] == "packet" and not args.include_other:
                continue
            if doc["doc_type"] == "other" and not args.include_other:
                continue

            meeting_date = doc["date"]
            if meeting_date:
                if cutoff <= meeting_date <= future_limit:
                    in_window.append(doc)
            elif doc["year"]:
                if doc["year"] >= cutoff.year:
                    in_window.append(doc)

        print(f"{len(in_window)} doc(s) in window  (of {len(docs)} on page)")
        all_docs.extend(in_window)
        time.sleep(PAGE_DELAY)

    all_docs.sort(
        key=lambda x: (x["date"] or datetime.date(x["year"] or 1900, 1, 1), x["board"]),
        reverse=True,
    )

    print()
    boards_represented = len({d["board"] for d in all_docs})
    print(f"Found {len(all_docs)} document(s) across {boards_represented} board(s).")
    print()

    if not all_docs:
        print("No documents found within the date window.")
        sys.exit(0)

    if args.dry_run:
        print(f"{'Board':<42} {'Date':<12} Type")
        print("-" * 62)
        for doc in all_docs:
            dt = str(doc["date"]) if doc["date"] else (str(doc["year"]) if doc["year"] else "?")
            print(f"{doc['board'][:41]:<42} {dt:<12} {doc['doc_type']}")
        print(f"\n{len(all_docs)} document(s). Re-run without --dry-run to download.")
        return

    # --- Step 3: download ---
    os.makedirs(args.output_dir, exist_ok=True)
    log_path = os.path.join(args.output_dir, "download-log.txt")
    log_lines = []
    downloaded = skipped = failed = 0
    seen_dests = {}

    for doc in all_docs:
        dest = make_dest_path(doc, args.output_dir)
        count = seen_dests.get(dest, 0)
        seen_dests[dest] = count + 1
        if count > 0:
            dest = make_dest_path(doc, args.output_dir, counter=count)

        label = os.path.basename(dest)

        if os.path.exists(dest):
            print(f"  skip (exists)  {label}")
            skipped += 1
            continue

        dt = str(doc["date"]) if doc["date"] else (str(doc["year"]) if doc["year"] else "?")
        print(f"  [{dt}] {doc['board']} — {doc['doc_type']}")
        print(f"  downloading    {label}")

        if doc.get("is_gotomeeting"):
            if save_gotomeeting_url(doc["href"], dest):
                downloaded += 1
                log_lines.append(
                    f"{datetime.datetime.now().isoformat()}  OK (url)  {dest}"
                )
            else:
                failed += 1
                log_lines.append(
                    f"{datetime.datetime.now().isoformat()}  FAILED   {doc['href']}"
                )
        else:
            full_url = f"{BASE_URL}/{doc['href']}"
            if download_pdf(full_url, dest):
                downloaded += 1
                log_lines.append(
                    f"{datetime.datetime.now().isoformat()}  OK       {dest}"
                )
            else:
                failed += 1
                log_lines.append(
                    f"{datetime.datetime.now().isoformat()}  FAILED   {full_url}"
                )
                if os.path.exists(dest):
                    os.remove(dest)

        time.sleep(DOWNLOAD_DELAY)

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
# 1. Preview without downloading (past 30 days):
#    python3 scripts/download-avon-agendas.py --dry-run
#
# 2. Include GoToMeeting video recordings in the preview:
#    python3 scripts/download-avon-agendas.py --dry-run
#    (recordings are included by default; use --no-recordings to skip them)
#
# 3. Extend the lookback window (e.g., 1 year):
#    python3 scripts/download-avon-agendas.py --days 365 --dry-run
#
# 4. Filter to a specific board:
#    python3 scripts/download-avon-agendas.py --board "town council" --dry-run
#    python3 scripts/download-avon-agendas.py --board "planning" --dry-run
#
# 5. Agendas only (skip minutes and recordings):
#    python3 scripts/download-avon-agendas.py --no-minutes --no-recordings
#
# 6. Skip video recordings:
#    python3 scripts/download-avon-agendas.py --no-recordings
#
# 7. Include supporting documents (classified as "other"):
#    python3 scripts/download-avon-agendas.py --include-other
#
# 8. Save files to a custom directory:
#    python3 scripts/download-avon-agendas.py --output-dir ~/Downloads/avon-meetings
#
# 9. Run on a schedule (cron — 8 AM daily):
#    0 8 * * * cd /path/to/repo && python3 scripts/download-avon-agendas.py
#
# SITE NOTES:
#   - avonct.gov uses Revize CMS with no Cloudflare protection; plain urllib works.
#   - Full-path document links redirect from avonct.gov to cms2.revize.com.
#     urllib follows the 302 redirect automatically.
#   - Since 2026, some boards (IWC, ZBA, P&Z) use short relative paths for documents
#     (e.g. "IWC/2026_May5_RegularMeeting.pdf?t=...") instead of the full
#     "Documents/Government/Boards and Committees/..." path. The ?t= cache-busting
#     timestamp distinguishes meeting documents from static nav links.
#   - Meeting dates come from the first column of each table row (MM/DD/YY format),
#     which is more reliable than parsing dates from filenames.
#   - GoToMeeting recordings are ephemeral (expire ~90 days per site policy).
#     The script saves them as .url Internet Shortcut files.
#   - Boards with GoToMeeting recordings (as of 2026):
#       Town Council, Planning & Zoning Commission,
#       Affordable Housing Task Force Committee, Avon Clean Energy Commission
#
# BOARDS (~15 as of 2026):
#   Affordable Housing Task Force Committee, AVFD Fire Station Building Committee,
#   Avon Clean Energy Commission, Avon Water Pollution Control Authority,
#   Board of Assessment Appeals, Board of Education, Board of Finance,
#   Building Code Board of Appeals, Committee on Aging, Inland Wetlands Commission,
#   Planning & Zoning Commission, Recreation & Parks Committee, Town Council,
#   Youth Services Advisory Board, Zoning Board of Appeals
