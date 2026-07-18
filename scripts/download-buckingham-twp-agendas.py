#!/usr/bin/env python3
# download-buckingham-twp-agendas.py
# Download meeting agendas, minutes, and video recordings from Buckingham
# Township, PA (buckinghampa.org) for documents posted in the last N days.
#
# USAGE:
#   python3 scripts/download-buckingham-twp-agendas.py [options]
#
# REQUIREMENTS:
#   - Python 3.6+  (no third-party packages needed for PDFs)
#   - yt-dlp       (for video: pip install yt-dlp)
#   - Internet connection
#
# WHAT IT DOES:
#   Documents:
#     1. Fetches a single page — /government/meetings/ — which contains all
#        board/commission meeting tables (upcoming + past for every body).
#     2. Parses the page by board-listing section so each past-meeting row is
#        tagged with the correct board name (past rows have no name column).
#     3. Collects PDF links from rows whose meeting date falls within a broad
#        candidate window (MEETING_LOOKBACK days past + any future date), then
#        HEAD-checks Last-Modified on each PDF.
#     4. Downloads PDFs whose Last-Modified falls within the --days window to
#        beat-archive/buckingham-twp-agendas/YYYY-MM/
#
#   Video:
#     5. Collects YouTube embed links from the Board of Supervisors sections.
#     6. Fetches each watch page to read the uploadDate field and filter to
#        those posted within the --days window.
#     7. Downloads matching videos via yt-dlp.
#
# SITE STRUCTURE (same custom CMS as Doylestown Township):
#   Base:      https://www.buckinghampa.org
#   Meetings:  /government/meetings/   (ALL boards on one page)
#   PDFs:      /media/{id}/{filename}.pdf
#   Video:     https://www.youtube.com/embed/{ID}?feature=oembed
#              YouTube channel: @BuckinghamBoardMeetings-wq1dm
#
#   Upcoming rows:    plain <tr> inside .upcoming-meetings table;
#                     board name in <span class="meeting-board">
#   Past-meeting rows: <tr data-year="YYYY"> inside .board-table tables;
#                      no board-name column — name comes from section <h2>/<h3>
#   Document cells:   <td class="col-i col-agenda"> / col-minutes / col-video
#   Meeting date:     <span class="meeting-date">Month D, YYYY ...
#                  or <div  class="meeting-date">Month D, YYYY ...
#
# NOTE: Board of Supervisors Regular Business Meeting and the Reorganization
#   meeting sections are the only bodies with YouTube recordings.
#
# NOTE: Last-Modified on /media/ PDFs is the server upload timestamp and is
#   used as the canonical "posted" date.
#
# NOTE: YouTube upload date is extracted by fetching the watch page with a
#   browser User-Agent and reading the "uploadDate" JSON field.

import argparse
import datetime
import email.utils
import html as htmllib
import os

YT_DLP_NODE = "node:/home/richkirby/.local/bin/yt-dlp-node"  # yt-dlp needs Node 22+; symlink kept current by scripts/update-yt-dlp-node.sh
import re
import subprocess
import sys
import time
import urllib.error
import urllib.request

# --- Configuration ---
BASE_URL   = "https://www.buckinghampa.org"
MEETINGS_PATH = "/government/meetings/"
OUTPUT_DIR = "beat-archive/buckingham-twp-agendas"
DAYS_BACK  = 3
MEETING_LOOKBACK = 90   # candidate past-meeting window in days (pre-HEAD check)

PAGE_DELAY     = 0.5
HEAD_DELAY     = 0.25
DOWNLOAD_DELAY = 0.8

UA = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36"
)

_MONTH_MAP = {
    "January": 1, "February": 2, "March": 3, "April": 4,
    "May": 5, "June": 6, "July": 7, "August": 8,
    "September": 9, "October": 10, "November": 11, "December": 12,
}

_DATE_RE = re.compile(
    r'<(?:span|div)\s+class="meeting-date">([A-Za-z]+ \d{1,2}, \d{4})',
    re.IGNORECASE,
)
_PDF_CELL_RE = re.compile(
    r'<td[^>]+class="[^"]*col-(?:agenda|minutes)[^"]*"[^>]*>(.*?)</td>',
    re.DOTALL | re.IGNORECASE,
)
_PDF_HREF_RE = re.compile(r'href="(/media/[^"]+\.pdf)"', re.IGNORECASE)
_VIDEO_CELL_RE = re.compile(
    r'<td[^>]+class="[^"]*col-video[^"]*"[^>]*>(.*?)</td>',
    re.DOTALL | re.IGNORECASE,
)
_YOUTUBE_EMBED_RE = re.compile(
    r'https://www\.youtube\.com/embed/([A-Za-z0-9_-]+)',
    re.IGNORECASE,
)
# Board name from upcoming-meetings table rows
_BOARD_NAME_RE = re.compile(
    r'class="meeting-board[^"]*"[^>]*>\s*(?:<a[^>]*>)?(.*?)(?:</a>)?\s*</span>',
    re.DOTALL | re.IGNORECASE,
)
# Board section heading inside a board-listing block
_SECTION_TITLE_RE = re.compile(
    r'<h[23][^>]*>(.*?)</h[23]>',
    re.DOTALL | re.IGNORECASE,
)


# --- HTTP helpers ---

def fetch_html(url):
    req = urllib.request.Request(
        url, headers={"User-Agent": UA, "Accept": "text/html,*/*"}
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            if r.status != 200:
                return None
            return r.read().decode(
                r.headers.get_content_charset() or "utf-8", errors="replace"
            )
    except urllib.error.HTTPError as e:
        if e.code != 404:
            print(f"  HTTP {e.code} — {url}", file=sys.stderr)
        return None
    except urllib.error.URLError as e:
        print(f"  ERROR fetching {url}: {e}", file=sys.stderr)
        return None


def head_last_modified(url):
    req = urllib.request.Request(
        url,
        headers={"User-Agent": UA, "Accept": "application/pdf,*/*"},
        method="HEAD",
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            lm = r.headers.get("Last-Modified")
            if lm:
                return email.utils.parsedate_to_datetime(lm).date()
    except Exception:
        pass
    return None


def get_youtube_upload_date(watch_url):
    """Fetch the YouTube watch page and extract uploadDate."""
    req = urllib.request.Request(
        watch_url,
        headers={"User-Agent": UA, "Accept": "text/html,*/*"},
    )
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            page = r.read().decode("utf-8", errors="replace")
        m = re.search(r'"uploadDate"\s*:\s*"(\d{4}-\d{2}-\d{2})', page)
        if m:
            return datetime.date.fromisoformat(m.group(1))
    except Exception as e:
        print(f"  WARNING: could not fetch upload date for {watch_url}: {e}",
              file=sys.stderr)
    return None


def download_pdf(url, dest_path):
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


def download_video(watch_url, dest_path):
    cmd = [
        "yt-dlp", "--js-runtimes", YT_DLP_NODE, "--no-playlist",
        "-f", "bestvideo+bestaudio/best",
        "--merge-output-format", "mp4",
        "-o", dest_path,
        "--no-overwrites", "--quiet", "--no-warnings",
        watch_url,
    ]
    try:
        subprocess.run(cmd, check=True, timeout=3600)
        return True
    except FileNotFoundError:
        print("  ERROR: yt-dlp not found. Install with: pip install yt-dlp",
              file=sys.stderr)
        return False
    except subprocess.CalledProcessError as e:
        print(f"  WARNING: yt-dlp failed ({e})", file=sys.stderr)
        return False
    except subprocess.TimeoutExpired:
        print("  ERROR: yt-dlp timed out after 3600s — partial file kept, will resume next run",
              file=sys.stderr)
        return False


# --- Parsing ---

def parse_meeting_date(text):
    text = htmllib.unescape(re.sub(r"<[^>]+>", "", text)).strip()
    m = re.match(r"([A-Za-z]+)\s+(\d{1,2}),?\s+(\d{4})", text)
    if not m:
        return None
    month = _MONTH_MAP.get(m.group(1))
    if not month:
        return None
    try:
        return datetime.date(int(m.group(3)), month, int(m.group(2)))
    except ValueError:
        return None


def extract_row_docs(row, board_label, meeting_cutoff, today, is_upcoming):
    """
    Parse one <tr> row and return (pdf_list, vid_list).
    Each pdf entry: {url, doc_type, board_label, meeting_date}
    Each vid entry: {video_id, watch_url, meeting_date}
    Returns ([], []) if the row is out of range or has no links.
    """
    # For past-meeting rows, check meeting date bounds
    if not is_upcoming:
        date_m = _DATE_RE.search(row)
        meeting_date = parse_meeting_date(date_m.group(1)) if date_m else None
        if meeting_date is None:
            return [], []
        if meeting_date < meeting_cutoff or meeting_date > today:
            return [], []
    else:
        date_m = _DATE_RE.search(row)
        meeting_date = parse_meeting_date(date_m.group(1)) if date_m else None

    pdfs = []
    vids = []

    for cell in _PDF_CELL_RE.finditer(row):
        if "no-file" in cell.group(0):
            continue
        doc_type = "agenda" if "col-agenda" in cell.group(0) else "minutes"
        for href_m in _PDF_HREF_RE.finditer(cell.group(1)):
            pdfs.append({
                "url":          BASE_URL + href_m.group(1),
                "doc_type":     doc_type,
                "board_label":  board_label,
                "meeting_date": meeting_date,
            })

    for cell in _VIDEO_CELL_RE.finditer(row):
        if "no-file" in cell.group(0):
            continue
        for yt_m in _YOUTUBE_EMBED_RE.finditer(cell.group(1)):
            vids.append({
                "video_id":     yt_m.group(1),
                "watch_url":    f"https://www.youtube.com/watch?v={yt_m.group(1)}",
                "meeting_date": meeting_date,
            })

    return pdfs, vids


def parse_meetings_page(html_text, meeting_cutoff):
    """
    Parse the full /government/meetings/ page.
    Returns (all_pdf_cands, all_vid_cands).
    """
    today = datetime.date.today()
    all_pdfs = []
    all_vids = []
    seen_pdf  = set()
    seen_vid  = set()

    # Split into board-listing sections (each starts with <div class="board-listing")
    sections = re.split(r'(?=<div\s+class="board-listing)', html_text, flags=re.IGNORECASE)

    for section in sections:
        # Determine section/board name from the first h2/h3 in this block
        title_m = _SECTION_TITLE_RE.search(section)
        section_title = (
            htmllib.unescape(re.sub(r"<[^>]+>", "", title_m.group(1))).strip()
            if title_m else ""
        )
        is_upcoming_section = "Upcoming" in section_title or "upcoming-meetings" in section

        # Parse all <tr> rows
        for row in re.finditer(r"<tr(?:\s[^>]*)?>.*?</tr>", section, re.DOTALL | re.IGNORECASE):
            row_html   = row.group(0)
            year_attr  = re.search(r'data-year="(\d{4})"', row_html)
            is_upcoming = (year_attr is None)

            # Upcoming rows: get board label from the meeting-board span in the row
            if is_upcoming:
                board_m = _BOARD_NAME_RE.search(row_html)
                board_label = (
                    htmllib.unescape(re.sub(r"<[^>]+>", "", board_m.group(1))).strip()
                    if board_m else section_title or "Unknown"
                )
            else:
                # Past-meeting rows: board label from the section heading
                board_label = section_title or "Unknown"

            pdfs, vids = extract_row_docs(
                row_html, board_label, meeting_cutoff, today, is_upcoming
            )

            for p in pdfs:
                if p["url"] not in seen_pdf:
                    seen_pdf.add(p["url"])
                    all_pdfs.append(p)

            for v in vids:
                if v["video_id"] not in seen_vid:
                    seen_vid.add(v["video_id"])
                    all_vids.append(v)

    return all_pdfs, all_vids


# --- File naming ---

def slugify(text, max_len=50):
    text = text.lower().strip()
    text = re.sub(r"[/\\&]", "-", text)
    text = re.sub(r"[^\w\s-]", "", text)
    text = re.sub(r"[\s_]+", "-", text)
    text = re.sub(r"-{2,}", "-", text)
    return text.strip("-")[:max_len]


def make_pdf_dest(doc_type, board_label, date_posted, output_dir, counter=0):
    month_dir = os.path.join(output_dir, date_posted.strftime("%Y-%m"))
    os.makedirs(month_dir, exist_ok=True)
    suffix = f"-{counter}" if counter > 0 else ""
    fname = (
        f"{date_posted.strftime('%Y-%m-%d')}-{slugify(board_label)}"
        f"-{doc_type}{suffix}.pdf"
    )
    return os.path.join(month_dir, fname)


def make_video_dest(board_label, meeting_date, output_dir, counter=0):
    ref_date  = meeting_date or datetime.date.today()
    month_dir = os.path.join(output_dir, ref_date.strftime("%Y-%m"))
    os.makedirs(month_dir, exist_ok=True)
    suffix = f"-{counter}" if counter > 0 else ""
    fname = (
        f"{ref_date.strftime('%Y-%m-%d')}-{slugify(board_label)}"
        f"-video{suffix}.mp4"
    )
    return os.path.join(month_dir, fname)


# --- Main ---

def main():
    parser = argparse.ArgumentParser(
        description=(
            "Download Buckingham Township PA meeting agendas, minutes, and "
            "video recordings posted in the past N days."
        )
    )
    parser.add_argument(
        "--days", type=int, default=DAYS_BACK, metavar="N",
        help=f"Look back N days for posted documents (default: {DAYS_BACK})",
    )
    parser.add_argument(
        "--meeting-lookback", type=int, default=MEETING_LOOKBACK, metavar="N",
        help=(
            f"Candidate window: consider past meetings held within N days "
            f"(default: {MEETING_LOOKBACK})"
        ),
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
        "--no-video", action="store_true",
        help="Skip video recordings (PDFs only)",
    )
    parser.add_argument(
        "--video-only", action="store_true",
        help="Download only video recordings (skip PDFs)",
    )
    args = parser.parse_args()

    do_docs  = not args.video_only
    do_video = not args.no_video

    today          = datetime.date.today()
    cutoff         = today - datetime.timedelta(days=args.days)
    meeting_cutoff = today - datetime.timedelta(days=args.meeting_lookback)

    print(f"Posted window     : {cutoff} to {today}")
    print(f"Meeting candidate : {meeting_cutoff} to {today} (+ all upcoming)")
    print(f"Source            : {BASE_URL}{MEETINGS_PATH}")
    if not args.dry_run:
        print(f"Output dir        : {args.output_dir}")
    print()

    # ------------------------------------------------------------------ #
    # Phase 1: fetch and parse the meetings page                           #
    # ------------------------------------------------------------------ #

    url = BASE_URL + MEETINGS_PATH
    print(f"Fetching {url} ...")
    html_text = fetch_html(url)
    if not html_text:
        print("ERROR: Could not fetch the meetings page.", file=sys.stderr)
        sys.exit(1)

    all_pdf_cands, all_vid_cands = parse_meetings_page(html_text, meeting_cutoff)
    print(f"  PDF candidates : {len(all_pdf_cands)}")
    print(f"  Video candidates: {len(all_vid_cands)}")
    print()

    # ------------------------------------------------------------------ #
    # Phase 2: HEAD-check PDFs for Last-Modified                          #
    # ------------------------------------------------------------------ #

    confirmed_pdfs = []
    if do_docs:
        print("Checking Last-Modified dates on PDFs...")
        fname_counters: dict = {}
        for cand in all_pdf_cands:
            lm = head_last_modified(cand["url"])
            time.sleep(HEAD_DELAY)
            if lm is None or lm < cutoff:
                continue
            key = (cand["board_label"], cand["doc_type"], lm)
            fname_counters[key] = fname_counters.get(key, 0) + 1
            cand["last_modified"] = lm
            cand["counter"]       = fname_counters[key] - 1
            confirmed_pdfs.append(cand)

        confirmed_pdfs.sort(key=lambda x: x["last_modified"], reverse=True)
        print(f"  {len(confirmed_pdfs)} PDF(s) posted within {args.days} day(s).")
        print()

    # ------------------------------------------------------------------ #
    # Phase 3: check YouTube upload dates                                  #
    # ------------------------------------------------------------------ #

    confirmed_vids = []
    if do_video and all_vid_cands:
        print("Checking YouTube upload dates...")
        vid_fname_counters: dict = {}
        for cand in all_vid_cands:
            upload_date = get_youtube_upload_date(cand["watch_url"])
            time.sleep(HEAD_DELAY)
            if upload_date is None or upload_date < cutoff:
                continue
            cand["upload_date"] = upload_date
            key = (cand["board_label"], cand["meeting_date"])
            vid_fname_counters[key] = vid_fname_counters.get(key, 0) + 1
            cand["counter"] = vid_fname_counters[key] - 1
            confirmed_vids.append(cand)
            print(f"  [{upload_date}] {cand['board_label']} — "
                  f"meeting {cand['meeting_date']} — {cand['watch_url']}")

        confirmed_vids.sort(key=lambda x: x["upload_date"], reverse=True)
        print(f"  {len(confirmed_vids)} video(s) posted within {args.days} day(s).")
        print()

    total = len(confirmed_pdfs) + len(confirmed_vids)

    if total == 0:
        print("No items found within the date window.")
        return

    # ------------------------------------------------------------------ #
    # Phase 4: report or download                                          #
    # ------------------------------------------------------------------ #

    if args.dry_run:
        if confirmed_pdfs:
            print(f"{'Board':<48} {'Posted':<12} Type")
            print("-" * 72)
            for c in confirmed_pdfs:
                print(f"{c['board_label'][:47]:<48} {c['last_modified']!s:<12} {c['doc_type']}")
        if confirmed_vids:
            print()
            print(f"{'Board':<48} {'Uploaded':<12} Meeting date")
            print("-" * 72)
            for c in confirmed_vids:
                print(f"{c['board_label'][:47]:<48} {c['upload_date']!s:<12} {c['meeting_date']}")
        print(f"\n{total} item(s). Re-run without --dry-run to download.")
        return

    os.makedirs(args.output_dir, exist_ok=True)
    log_path  = os.path.join(args.output_dir, "download-log.txt")
    log_lines = []
    downloaded = skipped = failed = 0

    for c in confirmed_pdfs:
        dest  = make_pdf_dest(
            c["doc_type"], c["board_label"],
            c["last_modified"], args.output_dir, c["counter"]
        )
        label = os.path.basename(dest)
        if os.path.exists(dest):
            print(f"  skip (exists)  {label}")
            skipped += 1
            continue
        print(f"  [posted {c['last_modified']}] {c['board_label']} — {c['doc_type']}")
        print(f"  downloading    {label}")
        if download_pdf(c["url"], dest):
            downloaded += 1
            log_lines.append(f"{datetime.datetime.now().isoformat()}  OK       {dest}")
        else:
            failed += 1
            log_lines.append(f"{datetime.datetime.now().isoformat()}  FAILED   {c['url']}")
            if os.path.exists(dest):
                os.remove(dest)
        time.sleep(DOWNLOAD_DELAY)

    for c in confirmed_vids:
        dest  = make_video_dest(
            c["board_label"], c["meeting_date"], args.output_dir, c["counter"]
        )
        label = os.path.basename(dest)
        if os.path.exists(dest):
            print(f"  skip (exists)  {label}")
            skipped += 1
            continue
        print(f"  [uploaded {c['upload_date']}] {c['board_label']} — "
              f"meeting {c['meeting_date']}")
        print(f"  downloading    {label}")
        print(f"  source URL:    {c['watch_url']}")
        if download_video(c["watch_url"], dest):
            downloaded += 1
            log_lines.append(f"{datetime.datetime.now().isoformat()}  OK       {dest}")
        else:
            failed += 1
            log_lines.append(f"{datetime.datetime.now().isoformat()}  FAILED   {c['watch_url']}")
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
# 1. Preview without downloading:
#    python3 scripts/download-buckingham-twp-agendas.py --dry-run
#
# 2. PDFs only (skip video):
#    python3 scripts/download-buckingham-twp-agendas.py --no-video
#
# 3. Video only:
#    python3 scripts/download-buckingham-twp-agendas.py --video-only
#
# 4. Widen the lookback window:
#    python3 scripts/download-buckingham-twp-agendas.py --days 7
#
# 5. Widen the meeting candidate window:
#    python3 scripts/download-buckingham-twp-agendas.py --meeting-lookback 180
#
# BOARDS (14 sections as of 2026):
#   Board of Supervisors Regular Business Meeting (+ YouTube video)
#   Board of Supervisors Work Session
#   Board of Supervisors Reorganization and Regular Business (+ YouTube video)
#   Planning Commission Regular Meeting
#   Planning Commission Comprehensive Plan Workshop
#   Zoning Hearing Board
#   Board of Auditors / Board of Auditors Reorganization Meeting
#   Historic Architectural Review Board
#   Historic Commission Meeting
#   Technical Code Review Board of Appeals
#   Agricultural and Open Space Preservation Committee
#   Water and Sewer Commission
#   Parks and Recreation Commission
#   YouTube channel: https://www.youtube.com/@BuckinghamBoardMeetings-wq1dm
