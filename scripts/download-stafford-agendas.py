#!/usr/bin/env python3
# download-stafford-agendas.py
# Download municipal meeting agendas and minutes from Stafford CT for meetings
# whose date falls within the past N days (and up to 7 days ahead), plus
# Zoom meeting recordings where publicly accessible.
#
# USAGE:
#   python3 scripts/download-stafford-agendas.py [options]
#
# REQUIREMENTS:
#   - Python 3.6+  (no third-party packages needed)
#   - yt-dlp installed (for Zoom video downloads): pip install yt-dlp
#   - Internet connection
#
# WHAT IT DOES:
#   1. Fetches the agendas/minutes page for each of 33 boards and commissions
#   2. Parses meeting rows for PDF document URLs and meeting dates (MM/DD/YY format)
#   3. Downloads PDFs to beat-archive/stafford-agendas/YYYY-MM/
#   4. Fetches the town's recordings page for Zoom meeting links
#   5. Parses meeting dates from recording titles
#   6. Downloads public Zoom recordings (no passcode) using yt-dlp
#   7. Appends a download log to beat-archive/stafford-agendas/download-log.txt
#
# SITE STRUCTURE (Revize CMS):
#   Board pages:  https://www.staffordct.org/{path}/agendas___minutes.php
#   Document URLs: staffordct.org/Document%20Center/Agendas%20%26%20Minutes/...
#     Each board page lists meetings as <tr> rows with:
#       - First <td>: date in MM/DD/YY format + meeting type
#       - Second <td>: Agenda and Minutes PDF links
#   Recordings:  https://www.staffordct.org/government/recordings.php
#     Zoom recordings listed in a <ul> with board headings and dated entries.
#     Passcode-protected recordings cannot be downloaded automatically and are
#     listed as skipped. Google Drive links are also skipped.

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

YT_DLP_NODE = "node:/home/richkirby/.local/bin/yt-dlp-node"  # yt-dlp needs Node 22+; symlink kept current by scripts/update-yt-dlp-node.sh

# --- Configuration ---
BASE_URL = "https://www.staffordct.org"
RECORDINGS_URL = f"{BASE_URL}/government/recordings.php"
OUTPUT_DIR = "beat-archive/stafford-agendas"
DAYS_BACK = 4
DAYS_AHEAD = 7
DELAY_SECONDS = 0.5

UA = "Mozilla/5.0 (X11; Linux x86_64; rv:120.0) Gecko/20100101 Firefox/120.0"

# All 33 boards/commissions with confirmed agendas pages.
# Board name → relative page path.
BOARDS = [
    ("Board of Selectmen",
     "government/selectmen/agendas___minutes.php"),
    ("Water Pollution Control Authority",
     "departments/water_pollution_control/agendas___minutes.php"),
    ("300 Year Anniversary Committee",
     "government/boards___commissions/300_year_anniversary_committee/agendas___minutes.php"),
    ("Agriculture Advisory Commission",
     "government/boards___commissions/agriculture_advisory_commission/agendas___minutes.php"),
    ("Annette Hyde Colton Fund",
     "government/boards___commissions/annette_hyde_colton_fund/agendas___minutes.php"),
    ("ARPA Commission",
     "government/boards___commissions/arpa_commission/agendas___minutes.php"),
    ("Arts Commission",
     "government/boards___commissions/arts_commission/agendas___minutes.php"),
    ("Board of Assessment Appeals",
     "government/boards___commissions/board_of_assessment_appeals/agendas___minutes.php"),
    ("Board of Education",
     "government/boards___commissions/board_of_education/agendas___minutes.php"),
    ("Board of Finance",
     "government/boards___commissions/board_of_finance/agendas___minutes.php"),
    ("Cemetery Association",
     "government/boards___commissions/cemetery_association/agendas___minutes.php"),
    ("Conservation Commission",
     "government/boards___commissions/conservation_commission/agendas___minutes.php"),
    ("Economic Development Commission",
     "government/boards___commissions/economic_development_commission/agendas___minutes.php"),
    ("Emergency Services Commission",
     "government/boards___commissions/emergency_services_commission/agenda_minutes.php"),
    ("Energy Advisory Committee",
     "government/boards___commissions/energy_advisory_committee/agendas___minutes.php"),
    ("Events & Celebrations Committee",
     "government/boards___commissions/events___celebrations_committee/agendas___minutes.php"),
    ("Family Services Advisory Board",
     "government/boards___commissions/family_services_advisory_board/agendas___minutes.php"),
    ("Flag Pole Committee",
     "government/boards___commissions/flag_pole_committee/agendas_and_minutes.php"),
    ("Hyde Park Commission",
     "government/boards___commissions/hyde_park_commission/agendas___minutes.php"),
    ("Inland Wetlands Commission",
     "government/boards___commissions/inland_wetlands_commission/agendas___minutes.php"),
    ("Library Board",
     "government/boards___commissions/library_board/agendas___minutes.php"),
    ("North Central District Board of Health",
     "government/boards___commissions/north_central_district_board_of_health/agendas___minutes.php"),
    ("Pension Committee",
     "government/boards___commissions/pension_committee/agendas___minutes.php"),
    ("Planning & Zoning Commission",
     "government/boards___commissions/planning___zoning_commission/agendas___minutes.php"),
    ("Recreation Commission",
     "government/boards___commissions/recreation_commission/agendas___minutes.php"),
    ("Safety Committee",
     "government/boards___commissions/safety_committee/agendas___minutes.php"),
    ("Service District Commission",
     "government/boards___commissions/service_district_commission/agendas___minutes.php"),
    ("Stafford Brownfields Advisory Board",
     "government/boards___commissions/stafford_brownfields_advisory_board/agendas___minutes.php"),
    ("Stafford Historic Advisory Commission",
     "government/boards___commissions/stafford_historic_advisory_commission/agendas___minutes.php"),
    ("Stafford Housing Authority",
     "government/boards___commissions/stafford_housing_authority/agendas___minutes.php"),
    ("Veterans Advisory Committee",
     "government/boards___commissions/veterans_advisory_committee/agendas___minutes.php"),
    ("Wall of Honor Phase IV Committee",
     "government/boards___commissions/wall_of_honor_phase_iv/agendas___minutes.php"),
    ("Zoning Board of Appeals",
     "government/boards___commissions/zoning_board_of_appeals/agenda___minutes.php"),
]

# Date regex in link title text: "January 7, 2026" or "March 25, 2025"
_REC_DATE_RE = re.compile(r'([A-Za-z]+ \d{1,2},?\s*\d{4})')


# --- HTTP helpers ---

def fetch_html(url):
    """GET url and return decoded HTML, or None."""
    req = urllib.request.Request(
        url,
        headers={"User-Agent": UA, "Accept": "text/html,*/*"},
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            raw = r.read()
            charset = r.headers.get_content_charset() or "utf-8"
            return raw.decode(charset, errors="replace")
    except urllib.error.HTTPError as e:
        if e.code != 404:
            print(f"  HTTP {e.code} — {url}", file=sys.stderr)
        return None
    except Exception as e:
        print(f"  ERROR fetching {url}: {e}", file=sys.stderr)
        return None


def download_file(url, dest_path):
    """Download url to dest_path. Returns True on success."""
    req = urllib.request.Request(
        url,
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


# --- PDF parsing (Revize CMS) ---

def href_to_url(href):
    """
    Convert a Revize relative href like
      "Document Center/Agendas & Minutes/Board/2026/MM-DD-Agenda.pdf?t=..."
    to a fully-qualified, percent-encoded URL.
    """
    clean = href.split("?")[0]          # strip cache-buster
    clean = clean.strip("/")
    return BASE_URL + "/" + urllib.parse.quote(clean, safe="/")


def parse_board_docs(board_name, page_path, cutoff, future_limit):
    """
    Fetch a board's agendas/minutes page and return a list of docs in the
    date window.

    Each doc: {board, meeting_date, doc_type, url, orig_filename}
    """
    url = f"{BASE_URL}/{page_path}"
    html = fetch_html(url)
    if not html:
        return []

    docs = []

    for row_m in re.finditer(r'<tr[^>]*>(.*?)</tr>', html, re.DOTALL):
        row_html = row_m.group(1)

        # First <td>: contains date in MM/DD/YY format
        tds = re.findall(r'<td[^>]*>(.*?)</td>', row_html, re.DOTALL)
        if len(tds) < 2:
            continue

        date_text = re.sub(r'<[^>]+>', '', tds[0]).strip()
        date_m = re.search(r'(\d{1,2})/(\d{1,2})/(\d{2})\b', date_text)
        if not date_m:
            continue

        mm, dd, yy = date_m.groups()
        yyyy = 2000 + int(yy)
        try:
            meeting_date = datetime.date(yyyy, int(mm), int(dd))
        except ValueError:
            continue

        if not (cutoff <= meeting_date <= future_limit):
            continue

        # Find all PDF links in this row
        for link_m in re.finditer(
            r'<[Aa]\s[^>]*href=["\']([^"\']+\.pdf[^"\']*)["\'][^>]*>(.*?)</[Aa]>',
            row_html, re.DOTALL
        ):
            href = link_m.group(1)
            link_text = html_module.unescape(
                re.sub(r'<[^>]+>', '', link_m.group(2))
            ).strip().lower()

            # Skip non-document PDFs (newsletters, etc.) by path
            if "agendas" not in href.lower() and "government" not in href.lower() \
                    and "document center" not in href.lower():
                continue

            if "agenda" in link_text:
                doc_type = "agenda"
            elif "minute" in link_text:
                doc_type = "minutes"
            else:
                doc_type = link_text[:20] or "doc"

            orig_filename = os.path.basename(href.split("?")[0])
            pdf_url = href_to_url(href)

            docs.append({
                "board": board_name,
                "meeting_date": meeting_date,
                "doc_type": doc_type,
                "url": pdf_url,
                "orig_filename": orig_filename,
            })

    return docs


# --- Recording parsing ---

def parse_recordings(page_html, cutoff, future_limit):
    """
    Parse Zoom/Drive recording links from the recordings page.

    Returns a list of dicts:
      {board, title, meeting_date, url, protected}

    Entries with passcode-protected Zoom links have protected=True.
    Google Drive entries are excluded (not machine-downloadable).
    Entries with no parseable date are excluded.
    """
    current_board = "Unknown"
    recordings = []

    for li_m in re.finditer(r'<li[^>]*>(.*?)</li>', page_html, re.DOTALL):
        li_html = li_m.group(1)
        text = html_module.unescape(
            re.sub(r'<[^>]+>', ' ', li_html)
        ).strip()
        text = re.sub(r'\s+', ' ', text)

        # Check for recording link (Zoom or Drive)
        link_m = re.search(
            r'href=["\']([^"\']+(?:zoom\.us|drive\.google)[^"\']*)["\']',
            li_html,
        )
        if link_m:
            rec_url = link_m.group(1)

            # Skip Google Drive (no programmatic download)
            if "drive.google" in rec_url:
                continue

            # Check for passcode hint in the surrounding text
            protected = bool(re.search(
                r'[Pp]ass(?:code|word)\s*[:=]', li_html
            ))

            # Parse date from the link text
            date_m = _REC_DATE_RE.search(text)
            if not date_m:
                continue
            date_str = re.sub(r'\s+', ' ', date_m.group(1).replace(',', ','))
            try:
                meeting_date = datetime.datetime.strptime(
                    date_str.strip(), "%B %d, %Y"
                ).date()
            except ValueError:
                continue

            if not (cutoff <= meeting_date <= future_limit):
                continue

            recordings.append({
                "board": current_board,
                "title": text,
                "meeting_date": meeting_date,
                "url": rec_url,
                "protected": protected,
            })

        else:
            # No link — may be a board name heading
            clean = text.strip().strip('\xa0').strip()
            if clean and len(clean) > 3 and not clean.startswith('\xa0'):
                current_board = clean

    return recordings


def is_in_yt_archive(archive_path, rec_url):
    """Check if a URL was already downloaded via yt-dlp archive."""
    if not os.path.exists(archive_path):
        return False
    # yt-dlp stores "zoom <id>" or the full URL in the archive
    with open(archive_path) as f:
        content = f.read()
    # Extract the share ID from the URL for matching
    url_id = rec_url.split("/")[-1].split("?")[0]
    return url_id in content


def download_zoom(rec_url, title, meeting_date, board, output_dir, archive_path):
    """
    Download a public Zoom recording with yt-dlp.
    Returns 'downloaded', 'skipped', or 'failed'.
    """
    month_dir = os.path.join(output_dir, meeting_date.strftime("%Y-%m"))
    os.makedirs(month_dir, exist_ok=True)
    date_str = meeting_date.strftime("%Y-%m-%d")
    board_slug = slugify(board, 35)
    url_id = rec_url.split("/")[-1].split("?")[0][:20]
    outtmpl = os.path.join(
        month_dir, f"{date_str}-{board_slug}-{url_id}.%(ext)s"
    )
    cmd = [
        "yt-dlp", "--js-runtimes", YT_DLP_NODE,
        "--no-playlist",
        "--merge-output-format", "mp4",
        "--download-archive", archive_path,
        "-o", outtmpl,
        "-q", "--no-warnings",
        rec_url,
    ]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=3600)
        return "downloaded" if result.returncode == 0 else "failed"
    except subprocess.TimeoutExpired:
        print(f"  WARNING: yt-dlp timed out for {rec_url}", file=sys.stderr)
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


def make_dest_path(board, meeting_date, orig_filename, output_dir):
    month_dir = os.path.join(output_dir, meeting_date.strftime("%Y-%m"))
    os.makedirs(month_dir, exist_ok=True)
    date_str = meeting_date.strftime("%Y-%m-%d")
    board_slug = slugify(board, max_len=40)
    fname = f"{date_str}-{board_slug}-{orig_filename}"
    return os.path.join(month_dir, fname)


# --- Main ---

def main():
    parser = argparse.ArgumentParser(
        description=(
            "Download Stafford CT municipal agendas, minutes, and Zoom recordings "
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
        help="Only include boards/meeting names containing NAME (case-insensitive)",
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
        help="Download PDFs only, skip video recordings",
    )
    parser.add_argument(
        "--videos-only", action="store_true",
        help="Download video recordings only, skip PDFs",
    )
    args = parser.parse_args()

    now = datetime.datetime.now()
    if (now.weekday() == 5 and now.hour >= 18) or (now.weekday() == 6 and now.hour < 12):  # Saturday night, Sunday morning
        print("Skipping — no downloads on Saturday nights or Sunday mornings.")
        sys.exit(0)

    today = datetime.date.today()
    cutoff = today - datetime.timedelta(days=args.days)
    future_limit = today + datetime.timedelta(days=args.ahead)

    print(f"Date window : {cutoff} to {future_limit}")
    print(f"Site        : {BASE_URL}")
    if not args.dry_run:
        print(f"Output dir  : {args.output_dir}")
    print()

    board_filter = args.board.lower() if args.board else None
    boards_to_scan = [
        (name, path) for (name, path) in BOARDS
        if board_filter is None or board_filter in name.lower()
    ]

    all_docs: list = []
    recordings: list = []

    # --- Step 1: PDFs from board agendas pages ---
    if not args.videos_only:
        print(f"Scanning {len(boards_to_scan)} board page(s) for documents...")
        for i, (board_name, page_path) in enumerate(boards_to_scan, 1):
            print(f"  [{i:2d}/{len(boards_to_scan)}] {board_name}...", end="", flush=True)
            docs = parse_board_docs(board_name, page_path, cutoff, future_limit)

            # Apply doc type filters
            if args.no_agendas:
                docs = [d for d in docs if d["doc_type"] != "agenda"]
            if args.no_minutes:
                docs = [d for d in docs if d["doc_type"] != "minutes"]

            print(f" {len(docs)} doc(s)")
            all_docs.extend(docs)
            time.sleep(DELAY_SECONDS)

        all_docs.sort(key=lambda x: (x["meeting_date"], x["board"]), reverse=True)
        print(f"  Total: {len(all_docs)} document(s) in window.\n")

    # --- Step 2: Zoom recordings ---
    if not args.docs_only:
        print("Fetching recordings page...")
        rec_html = fetch_html(RECORDINGS_URL)
        if rec_html:
            recordings = parse_recordings(rec_html, cutoff, future_limit)
            if board_filter:
                recordings = [
                    r for r in recordings
                    if board_filter in r["board"].lower()
                    or board_filter in r["title"].lower()
                ]
            print(f"  Found {len(recordings)} recording(s) in window.")
            protected = [r for r in recordings if r["protected"]]
            if protected:
                print(
                    f"  NOTE: {len(protected)} recording(s) are passcode-protected "
                    "and will be skipped."
                )
        print()

    if not all_docs and not recordings:
        print("No documents or recordings found in the date window.")
        return

    # --- Dry-run listing ---
    if args.dry_run:
        if all_docs:
            print(f"{'Board':<42} {'Date':<12} Type     Filename")
            print("-" * 85)
            for d in all_docs:
                print(
                    f"{d['board'][:41]:<42} "
                    f"{d['meeting_date']!s:<12} "
                    f"{d['doc_type']:<8} "
                    f"{d['orig_filename']}"
                )
            print()
        if recordings:
            print(f"{'Board':<35} {'Date':<12} {'Prot':<5} Title")
            print("-" * 80)
            for rec in recordings:
                prot = "YES" if rec["protected"] else "no"
                print(
                    f"{rec['board'][:34]:<35} "
                    f"{rec['meeting_date']!s:<12} "
                    f"{prot:<5} "
                    f"{rec['title'][:50]}"
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
            dest = make_dest_path(
                d["board"], d["meeting_date"], d["orig_filename"], args.output_dir
            )
            label = os.path.basename(dest)

            if os.path.exists(dest):
                print(f"  skip (exists)  {label}")
                skipped += 1
                continue

            print(f"  [{d['meeting_date']}] {d['board'][:45]} — {d['doc_type']}")
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

            time.sleep(DELAY_SECONDS)

        print()

    # --- Step 4: Download Zoom recordings ---
    if recordings:
        archive_path = os.path.join(args.output_dir, "yt-archive.txt")
        public_recs = [r for r in recordings if not r["protected"]]
        protected_recs = [r for r in recordings if r["protected"]]

        if protected_recs:
            print(f"Skipping {len(protected_recs)} passcode-protected recording(s):")
            for r in protected_recs:
                print(f"  [{r['meeting_date']}] {r['board']} — {r['title'][:60]}")
                print(f"    {r['url'][:80]}")
            print()

        if public_recs:
            print(f"Downloading {len(public_recs)} public Zoom recording(s)...")
            for rec in public_recs:
                url_id = rec["url"].split("/")[-1].split("?")[0][:20]
                print(f"  [{rec['meeting_date']}] {rec['board']} — {rec['title'][:50]}")

                if is_in_yt_archive(archive_path, rec["url"]):
                    print(f"  skip (archive) {url_id}")
                    skipped += 1
                    continue

                print(f"  downloading    {url_id}")
                status = download_zoom(
                    rec["url"], rec["title"], rec["meeting_date"],
                    rec["board"], args.output_dir, archive_path,
                )
                if status == "downloaded":
                    downloaded += 1
                    log_lines.append(
                        f"{datetime.datetime.now().isoformat()}  OK       zoom:{url_id}  {rec['title']}"
                    )
                else:
                    failed += 1
                    log_lines.append(
                        f"{datetime.datetime.now().isoformat()}  FAILED   {rec['url']}"
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
#    python3 scripts/download-stafford-agendas.py --dry-run
#
# 2. Narrow to one board:
#    python3 scripts/download-stafford-agendas.py --board "Selectmen"
#
# 3. PDFs only (no video downloads):
#    python3 scripts/download-stafford-agendas.py --docs-only
#
# 4. Videos only:
#    python3 scripts/download-stafford-agendas.py --videos-only
#
# 5. Agendas only (skip minutes):
#    python3 scripts/download-stafford-agendas.py --no-minutes
#
# 6. Change the lookback window:
#    python3 scripts/download-stafford-agendas.py --days 7
#
# 7. Save files somewhere else:
#    python3 scripts/download-stafford-agendas.py --output-dir ~/Downloads/stafford
#
# 8. Run on a schedule (cron — 7 AM daily):
#    0 7 * * * cd /path/to/repo && python3 scripts/download-stafford-agendas.py
#
# NOTES:
#   - Stafford CT uses Revize CMS. Each board has its own agendas/minutes page;
#     all 33 are fetched individually. Documents are stored in a "Document Center"
#     with URL-encoded paths served directly as PDFs.
#   - Meeting dates are in MM/DD/YY format in the first table column of each row.
#   - Zoom recordings are available for Board of Selectmen, Board of Finance, and
#     a few information meetings. Only recordings without passcode protection are
#     downloaded automatically; protected recordings are listed with their URL so
#     they can be accessed manually.
#   - Google Drive recording links are listed for reference but cannot be
#     downloaded programmatically.
#   - The --ahead flag (default: 7 days) captures agendas posted early for
#     upcoming meetings.
