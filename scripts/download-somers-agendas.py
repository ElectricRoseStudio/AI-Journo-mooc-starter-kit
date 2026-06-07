#!/usr/bin/env python3
# download-somers-agendas.py
# Download municipal meeting agendas, minutes, and YouTube recordings from Somers CT
# for meetings whose date falls within the past N days (and up to 7 days ahead).
#
# USAGE:
#   python3 scripts/download-somers-agendas.py [options]
#
# REQUIREMENTS:
#   - Python 3.6+  (no third-party packages needed)
#   - yt-dlp installed (for video downloads only): pip install yt-dlp
#   - Internet connection
#
# WHAT IT DOES:
#   1. Fetches each board/commission's agendas-and-minutes PHP page (28 pages)
#   2. Parses meeting rows: date, agenda PDF, minutes PDF, and YouTube recording link
#   3. Filters by date window (meeting date, MM/DD/YYYY)
#   4. Downloads PDFs to beat-archive/somers-agendas/YYYY-MM/
#   5. Downloads YouTube recordings with yt-dlp
#   6. Appends a download log to beat-archive/somers-agendas/download-log.txt
#
# SITE STRUCTURE:
#   CMS: Revize (www.somersct.gov)
#
#   Hub page: https://www.somersct.gov/boards_commissions/agendas_and_minutes.php
#   Each board has a dedicated page, e.g.:
#     https://www.somersct.gov/boards_commissions/board_of_selectman/agendas_minutes.php
#
#   Meeting row format (HTML table):
#     <span class="agenda-date">MM/DD/YYYY</span>
#     <span class="agenda-name">Meeting Name</span>
#     <td class="agenda_doc"><A href="Documents/Boards Commissions/BOARD/...">Agenda</A></td>
#     <td class="minutes_doc"><A href="Documents/Boards Commissions/BOARD/...">Minutes</A></td>
#     <td class="video_url"><A href="https://www.youtube.com/watch?v=ID">Video</A></td>
#
#   Documents are at: https://www.somersct.gov/Documents/Boards Commissions/{board}/...
#   (base href is https://www.somersct.gov/ so paths are relative to site root)
#   Document filename prefix: YYYYMMDD (e.g., 20260430-BOS-Special-Agenda.pdf)
#
#   YouTube recording URLs appear as watch?v=, /live/, or youtu.be/ links.
#   Boards with recorded meetings include Board of Selectmen, Board of Finance,
#   Planning Commission, and Zoning Commission.
#
# BOARDS (28 pages):
#   America250 Planning Committee, Advisory Committee for Seniors,
#   Board of Assessment Appeals, Board of Education, Board of Finance,
#   Board of Selectmen, Capital Improvement Projects Committee,
#   Cemetery Committee, Charter Revision Commission, Conservation Commission,
#   Cultural Commission, Design Advisory Board,
#   Economic Development Commission, Emergency Preparedness Advisory Council,
#   Ethics Commission, Housing Authority, Library Trustees,
#   Open Space & Trails Committee, Pension Commission, Planning Commission,
#   Prison Liaison/Public Safety Committee, Recreation Commission,
#   Somersville Mill Strategic Planning (Ad Hoc),
#   Water Pollution Control Authority,
#   Youth Services Bureau Advisory Board,
#   Zoning Board of Appeals, Zoning Commission,
#   Veterans Memorial Park Ad Hoc Committee

import argparse
import collections
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
BASE_URL = "https://www.somersct.gov"

# All board/commission agendas-and-minutes pages. Board name is extracted
# dynamically from document paths found on each page.
BOARD_PAGES = [
    "boards_commissions/america250_planning_committee.php",
    "boards_commissions/advisory_committee_for_seniors/agendas_minutes.php",
    "boards_commissions/board_of_assessment_appeals/agendas_minutes.php",
    "boards_commissions/agenda___minutes.php",                    # Board of Education
    "boards_commissions/board_of_finance/agendas_minutes.php",
    "boards_commissions/board_of_selectman/agendas_minutes.php",
    "boards_commissions/capital_improvement_projects_committee/agendas_minutes.php",
    "boards_commissions/cemetery_committee/agendas_minutes.php",
    "boards_commissions/charter_revision_commission/agendas_minutes.php",
    "boards_commissions/conservation_commission/agendas_minutes.php",
    "boards_commissions/cultural_commission/agendas_minutes.php",
    "boards_commissions/design_advisory_board/agendas_minutes.php",
    "boards_commissions/economic_development_commission/agendas_minutes.php",
    "boards_commissions/emergency_preparedness_advisory_council/agendas_minutes.php",
    "boards_commissions/ethics_commission/agendas_minutes.php",
    "boards_commissions/agendas___minutes.php",                   # Housing Authority
    "boards_commissions/library_trustees/agendas_minutes.php",
    "boards_commissions/open_space_trails_committee/agendas_minutes.php",
    "boards_commissions/pension_commission/agendas_minutes.php",
    "boards_commissions/planning_commission/agendas_minutes.php",
    "boards_commissions/prison_liaison_public_safety_committee/agenda_minutes.php",
    "boards_commissions/recreation_commission/agendas_minutes.php",
    "boards_commissions/somersville_mill_strategic_planning_ad_hoc_committee/agendas_minutes.php",
    "boards_commissions/water_pollution_control_authority/agendas_minutes.php",
    "boards_commissions/agendas___minutes_.php",                  # Youth Services Bureau Advisory Board
    "boards_commissions/zoning_board_of_appeals/agendas_minutes.php",
    "boards_commissions/zoning_commission/agendas_minutes.php",
    "boards_commissions/veterans_mem_park_ad_hoc_committee_agenda_minutes.php",
]

OUTPUT_DIR = "beat-archive/somers-agendas"
DAYS_BACK = 4
DAYS_AHEAD = 7
DELAY_SECONDS = 0.5

UA = "Mozilla/5.0 (X11; Linux x86_64; rv:120.0) Gecko/20100101 Firefox/120.0"

# Board name extracted from document path: "Documents/Boards Commissions/BOARD NAME/..."
# Excludes " ? < > to avoid matching across href attribute boundaries or into HTML.
_BOARD_NAME_RE = re.compile(r'Documents/Boards Commissions/([^/"?#<>]+)/', re.IGNORECASE)

# Meeting table row
_ROW_RE = re.compile(
    r'<tr>\s*<td[^>]*>\s*<a[^>]+class="agenda-link"[^>]*>'
    r'<span class="agenda-date">([^<]+)</span>\s*'
    r'<span class="agenda-name">([^<]+)</span></a>\s*</td>'
    r'(.*?)</tr>',
    re.DOTALL,
)
_AGENDA_RE = re.compile(r'class="agenda_doc"[^>]*>.*?href="([^"?#]+)', re.DOTALL)
_MINUTES_RE = re.compile(r'class="minutes_doc"[^>]*>.*?href="([^"?#]+)', re.DOTALL)
_VIDEO_RE = re.compile(r'class="video_url"[^>]*>.*?href="([^"]+)"', re.DOTALL)


# --- HTTP helpers ---

def fetch_html(url):
    """GET url and return decoded HTML, or None on error."""
    req = urllib.request.Request(
        url, headers={"User-Agent": UA, "Accept": "text/html,*/*"}
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            raw = r.read()
            charset = r.headers.get_content_charset() or "utf-8"
            return raw.decode(charset, errors="replace")
    except urllib.error.HTTPError as e:
        print(f"  HTTP {e.code} — {url}", file=sys.stderr)
        return None
    except Exception as e:
        print(f"  ERROR fetching {url}: {e}", file=sys.stderr)
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


# --- Page parsing ---

def parse_board_name(html):
    """Extract board name from document paths, using the most common match.

    Using most-common rather than first-match handles cases where a single
    document is mis-filed under a different board's folder.
    """
    names = [m.strip() for m in _BOARD_NAME_RE.findall(html)]
    if not names:
        return None
    return collections.Counter(names).most_common(1)[0][0]


def parse_meetings(html):
    """
    Parse meeting rows from a board's agendas-and-minutes page.

    Returns list of dicts:
      {board, meeting_date, meeting_name, agenda_url, minutes_url, video_url}
    """
    board_name = parse_board_name(html) or "Unknown Board"
    meetings = []

    for m in _ROW_RE.finditer(html):
        date_str = m.group(1).strip()   # MM/DD/YYYY
        mtg_name = html_module.unescape(m.group(2).strip())
        rest = m.group(3)

        # Parse date
        try:
            meeting_date = datetime.datetime.strptime(date_str, "%m/%d/%Y").date()
        except ValueError:
            continue

        # Extract document URLs (strip query strings)
        agenda_m = _AGENDA_RE.search(rest)
        minutes_m = _MINUTES_RE.search(rest)
        video_m = _VIDEO_RE.search(rest)

        agenda_url = agenda_m.group(1).strip() if agenda_m else None
        minutes_url = minutes_m.group(1).strip() if minutes_m else None
        video_url = video_m.group(1).strip() if video_m else None

        if agenda_url or minutes_url or video_url:
            meetings.append({
                "board": board_name,
                "meeting_date": meeting_date,
                "meeting_name": mtg_name,
                "agenda_url": agenda_url,
                "minutes_url": minutes_url,
                "video_url": video_url,
            })

    return meetings


def make_doc_url(relative_path):
    """
    Convert a relative document path to a full URL, URL-encoding spaces.
    Input: "Documents/Boards Commissions/Board of Selectmen/..."
    Output: "https://www.somersct.gov/Documents/Boards%20Commissions/..."
    """
    return BASE_URL + "/" + urllib.parse.quote(relative_path, safe="/")


# --- YouTube helpers ---

def extract_yt_id(url):
    """Extract YouTube video ID from a watch, live, or youtu.be URL."""
    m = re.search(
        r"(?:youtube\.com/(?:watch\?v=|live/)|youtu\.be/)([A-Za-z0-9_-]{10,12})",
        url,
    )
    return m.group(1) if m else None


def is_in_yt_archive(archive_path, video_id):
    """Return True if video_id is already in the yt-dlp download archive."""
    if not os.path.exists(archive_path):
        return False
    needle = f"youtube {video_id}"
    with open(archive_path) as f:
        return any(needle in line for line in f)


def download_yt_video(video_id, url, board, mtg_name, mtg_date, output_dir, archive_path):
    """Download a YouTube video with yt-dlp. Returns 'downloaded', 'skipped', or 'failed'."""
    month_dir = os.path.join(output_dir, mtg_date.strftime("%Y-%m"))
    os.makedirs(month_dir, exist_ok=True)
    date_str = mtg_date.strftime("%Y-%m-%d")
    board_slug = slugify(board, max_len=40)
    name_slug = slugify(mtg_name, max_len=40)
    outtmpl = os.path.join(month_dir, f"{date_str}-{board_slug}-{name_slug}-{video_id}.%(ext)s")

    cmd = [
        "yt-dlp",
        "--no-playlist",
        "--merge-output-format", "mp4",
        "--download-archive", archive_path,
        "-o", outtmpl,
        "-q", "--no-warnings",
        url,
    ]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=3600)
        return "downloaded" if result.returncode == 0 else "failed"
    except subprocess.TimeoutExpired:
        print(f"  WARNING: yt-dlp timed out for {video_id}", file=sys.stderr)
        return "failed"
    except FileNotFoundError:
        print("  ERROR: yt-dlp not found. Install with: pip install yt-dlp", file=sys.stderr)
        return "failed"


# --- Utilities ---

def slugify(text, max_len=55):
    text = text.lower().strip()
    text = re.sub(r"[/\\&]", "-", text)
    text = re.sub(r"[^\w\s-]", "", text)
    text = re.sub(r"[\s_]+", "-", text)
    text = re.sub(r"-{2,}", "-", text)
    return text.strip("-")[:max_len]


def make_pdf_path(board, doc_type, meeting_date, meeting_name, output_dir):
    date_str = meeting_date.strftime("%Y-%m-%d")
    month_dir = os.path.join(output_dir, meeting_date.strftime("%Y-%m"))
    os.makedirs(month_dir, exist_ok=True)
    board_slug = slugify(board, max_len=35)
    name_slug = slugify(meeting_name, max_len=35)
    fname = f"{date_str}-{board_slug}-{name_slug}-{doc_type}.pdf"
    return os.path.join(month_dir, fname)


# --- Main ---

def main():
    parser = argparse.ArgumentParser(
        description=(
            "Download Somers CT municipal agendas, minutes, and YouTube recordings "
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
        help="Only include boards/video titles containing NAME (case-insensitive)",
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

    if datetime.date.today().weekday() in (6, 0):  # Sunday, Monday
        print("Skipping — no downloads on Sunday or Monday.")
        sys.exit(0)

    today = datetime.date.today()
    cutoff = today - datetime.timedelta(days=args.days)
    future_limit = today + datetime.timedelta(days=args.ahead)
    board_filter = args.board.lower() if args.board else None

    print(f"Date window : {cutoff} to {future_limit}")
    print(f"Boards      : {len(BOARD_PAGES)} pages")
    if not args.dry_run:
        print(f"Output dir  : {args.output_dir}")
    print()

    all_docs: list = []
    all_videos: list = []

    # --- Fetch all board pages ---
    print(f"Fetching {len(BOARD_PAGES)} board pages...")
    for page_path in BOARD_PAGES:
        url = f"{BASE_URL}/{page_path}"
        html = fetch_html(url)
        if not html:
            continue

        meetings = parse_meetings(html)
        if not meetings:
            continue

        board_name = meetings[0]["board"]
        if board_filter and board_filter not in board_name.lower():
            continue

        in_window = 0
        for mtg in meetings:
            if not (cutoff <= mtg["meeting_date"] <= future_limit):
                continue
            in_window += 1

            if not args.videos_only:
                if mtg["agenda_url"] and not args.no_agendas:
                    all_docs.append({
                        "board": mtg["board"],
                        "meeting_date": mtg["meeting_date"],
                        "meeting_name": mtg["meeting_name"],
                        "doc_type": "agenda",
                        "url": make_doc_url(mtg["agenda_url"]),
                    })
                if mtg["minutes_url"] and not args.no_minutes:
                    all_docs.append({
                        "board": mtg["board"],
                        "meeting_date": mtg["meeting_date"],
                        "meeting_name": mtg["meeting_name"],
                        "doc_type": "minutes",
                        "url": make_doc_url(mtg["minutes_url"]),
                    })

            if not args.docs_only and mtg["video_url"]:
                vid_id = extract_yt_id(mtg["video_url"])
                if vid_id:
                    all_videos.append({
                        "board": mtg["board"],
                        "meeting_date": mtg["meeting_date"],
                        "meeting_name": mtg["meeting_name"],
                        "video_id": vid_id,
                        "video_url": mtg["video_url"],
                    })

        if in_window:
            print(f"  {board_name}: {in_window} meeting(s) in window")

        time.sleep(DELAY_SECONDS)

    all_docs.sort(key=lambda x: (x["meeting_date"], x["board"]), reverse=True)
    all_videos.sort(key=lambda x: (x["meeting_date"], x["board"]), reverse=True)
    print()
    print(f"  {len(all_docs)} document(s) in window")
    print(f"  {len(all_videos)} recording(s) in window")
    print()

    if not all_docs and not all_videos:
        print("No documents or recordings found in the date window.")
        return

    # --- Dry-run listing ---
    if args.dry_run:
        if all_docs:
            print(f"{'Board':<40} {'Date':<12} {'Meeting':<30} Type")
            print("-" * 90)
            for d in all_docs:
                print(
                    f"{d['board'][:39]:<40} "
                    f"{d['meeting_date']!s:<12} "
                    f"{d['meeting_name'][:29]:<30} "
                    f"{d['doc_type']}"
                )
            print()
        if all_videos:
            print(f"{'Board':<30} {'Date':<12} {'Meeting':<30} Video ID")
            print("-" * 85)
            for v in all_videos:
                print(
                    f"{v['board'][:29]:<30} "
                    f"{v['meeting_date']!s:<12} "
                    f"{v['meeting_name'][:29]:<30} "
                    f"{v['video_id']}"
                )
            print()
        total = len(all_docs) + len(all_videos)
        print(f"{total} item(s) matched. Re-run without --dry-run to download.")
        return

    # --- Download PDFs ---
    os.makedirs(args.output_dir, exist_ok=True)
    log_path = os.path.join(args.output_dir, "download-log.txt")
    log_lines = []
    downloaded = skipped = failed = 0

    if all_docs:
        for d in all_docs:
            dest = make_pdf_path(
                d["board"], d["doc_type"], d["meeting_date"],
                d["meeting_name"], args.output_dir,
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

    # --- Download YouTube videos ---
    if all_videos:
        archive_path = os.path.join(args.output_dir, "yt-archive.txt")
        print(f"Downloading {len(all_videos)} YouTube recording(s)...")
        for v in all_videos:
            vid = v["video_id"]
            print(f"  [{v['meeting_date']}] {v['board'][:45]} — {v['meeting_name']}")

            if is_in_yt_archive(archive_path, vid):
                print(f"  skip (archive) {vid}")
                skipped += 1
                continue

            print(f"  downloading    {vid}  {v['video_url'][:70]}")
            status = download_yt_video(
                vid, v["video_url"], v["board"],
                v["meeting_name"], v["meeting_date"],
                args.output_dir, archive_path,
            )
            if status == "downloaded":
                downloaded += 1
                log_lines.append(
                    f"{datetime.datetime.now().isoformat()}  OK       yt:{vid}  {v['meeting_name']}"
                )
            else:
                failed += 1
                log_lines.append(
                    f"{datetime.datetime.now().isoformat()}  FAILED   yt:{vid}  {v['video_url']}"
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
#    python3 scripts/download-somers-agendas.py --dry-run
#
# 2. Narrow to one board:
#    python3 scripts/download-somers-agendas.py --board "Board of Selectmen"
#
# 3. PDFs only (no video downloads):
#    python3 scripts/download-somers-agendas.py --docs-only
#
# 4. Videos only:
#    python3 scripts/download-somers-agendas.py --videos-only
#
# 5. Agendas only (skip minutes):
#    python3 scripts/download-somers-agendas.py --no-minutes
#
# 6. Change the lookback window:
#    python3 scripts/download-somers-agendas.py --days 14
#
# 7. Run on a schedule (cron — 7 AM daily):
#    0 7 * * * cd /path/to/repo && python3 scripts/download-somers-agendas.py
#
# NOTES:
#   - Somers CT uses Revize CMS. All document links are statically embedded in
#     each board's PHP page — no JavaScript or AJAX required. Each page is
#     fetched once per run.
#   - Document paths contain spaces (e.g., "Documents/Boards Commissions/...").
#     The site's base href is https://www.somersct.gov/, so paths are relative
#     to site root. The script URL-encodes spaces before fetching.
#   - Document filenames begin with YYYYMMDD (e.g., 20260430-BOS-Special-Agenda.pdf).
#     Meeting dates come from the <span class="agenda-date"> field (MM/DD/YYYY).
#   - YouTube recordings are linked per-meeting in the video_url table column.
#     Board of Selectmen and Board of Finance have the most consistent video
#     coverage; Planning Commission and Zoning Commission also record some meetings.
#   - yt-dlp writes a download archive (yt-archive.txt) so videos are not
#     re-downloaded on subsequent runs.
#   - Some board pages are listed under generic filenames (e.g., agenda___minutes.php
#     for Board of Education, agendas___minutes.php for Housing Authority). Board
#     names are extracted from the document file paths at runtime, not from the URL.
#   - The 28-page list in BOARD_PAGES is current as of May 2026. If Somers adds
#     a new board page, add its relative path to BOARD_PAGES.
