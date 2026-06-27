#!/usr/bin/env python3
# download-doylestown-twp-agendas.py
# Download meeting agendas, minutes, and video recordings from Doylestown
# Township, PA (doylestownpa.org) for documents posted in the last N days.
#
# USAGE:
#   python3 scripts/download-doylestown-twp-agendas.py [options]
#
# REQUIREMENTS:
#   - Python 3.6+  (no third-party packages needed for PDFs)
#   - yt-dlp       (for video: pip install yt-dlp or brew install yt-dlp)
#   - Internet connection
#
# WHAT IT DOES:
#   Documents:
#     1. Fetches /government/meetings/ for upcoming agendas (all boards) and
#        Board of Supervisors past meetings (agendas + minutes + video).
#     2. Fetches each board/commission page for its own past meeting documents.
#     3. Collects all PDF links in agenda/minutes columns whose meeting date
#        falls within a broad candidate window (MEETING_LOOKBACK days past +
#        any future date), then HEAD-checks Last-Modified on each PDF.
#     4. Downloads PDFs whose Last-Modified falls within the --days window to
#        beat-archive/doylestown-twp-agendas/YYYY-MM/
#
#   Video:
#     5. Collects YouTube embed URLs from the Board of Supervisors table for
#        meetings within the candidate window.
#     6. Queries yt-dlp for the upload_date of each video to find those
#        posted within the --days window.
#     7. Downloads matching videos via yt-dlp.
#
# SITE STRUCTURE (custom CMS, PDFs self-hosted, video on YouTube):
#   Base:        https://www.doylestownpa.org
#   All boards:  /government/meetings/              (upcoming + BoS past)
#   Board pages: /government/boards-commissions/{slug}/
#   PDFs:        /media/{id}/{filename}.pdf
#   Video:       https://www.youtube.com/embed/{ID}?feature=oembed
#                 → converted to https://www.youtube.com/watch?v={ID} for yt-dlp
#
#   Upcoming table rows: plain <tr> (no data-year attr)
#   Past meeting rows:   <tr data-year="YYYY">
#   Document cells:      <td class="col-i col-agenda"> / col-minutes / col-video
#   Meeting date:        <span class="meeting-date">Month D, YYYY ...
#                     or <div class="meeting-date">Month D, YYYY ...
#
# NOTE: Only the Board of Supervisors meetings have YouTube video recordings.
#   All other boards publish PDFs only.
#
# NOTE: Last-Modified on /media/ PDFs is the server upload timestamp and is
#   used as the canonical "posted" date. Meeting date is used only to bound
#   the candidate window, not for filtering.
#
# NOTE: yt-dlp is invoked with --print upload_date to check the video's
#   YouTube upload date before committing to a full download.

import argparse
import datetime
import email.utils
import html
import os
import re
import subprocess
import sys
import time
import urllib.error
import urllib.request

# --- Configuration ---
BASE_URL   = "https://www.doylestownpa.org"
OUTPUT_DIR = "beat-archive/doylestown-twp-agendas"
DAYS_BACK  = 3
MEETING_LOOKBACK = 90   # candidate past-meeting window in days (pre-HEAD check)

PAGE_DELAY     = 0.5
HEAD_DELAY     = 0.25
DOWNLOAD_DELAY = 0.8

# Board of Supervisors page (also serves upcoming for all boards)
MEETINGS_PAGE = "/government/meetings/"

# Board/commission slugs (all pages with their own past-meeting tables)
BOARD_SLUGS = [
    "agricultural-security-advisory-council",
    "bike-and-hike-committee",
    "board-of-auditors",
    "environmental-advisory-council",
    "finance-committee",
    "friends-of-kids-castle",
    "historical-architectural-review-board",
    "human-relations-commission",
    "local-traffic-advisory-committee",
    "municipal-authority-dtma",
    "park-and-recreation-board",
    "pension-advisory-committee",
    "planning-commission",
    "public-water-sewer-advisory-board",
    "telecommunications-advisory-board",
    "ucc-board-of-appeals",
    "veterans-advisory-committee",
    "zoning-hearing-board",
]

UA = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36"
)

_MONTH_MAP = {
    "January": 1, "February": 2, "March": 3, "April": 4,
    "May": 5, "June": 6, "July": 7, "August": 8,
    "September": 9, "October": 10, "November": 11, "December": 12,
}

# Matches <span class="meeting-date">Month D, YYYY or <div class="meeting-date">...
_DATE_RE = re.compile(
    r'<(?:span|div)\s+class="meeting-date">([A-Za-z]+ \d{1,2}, \d{4})',
    re.IGNORECASE,
)

# Matches PDF links in col-agenda or col-minutes cells
_PDF_CELL_RE = re.compile(
    r'<td[^>]+class="[^"]*col-(?:agenda|minutes)[^"]*"[^>]*>(.*?)</td>',
    re.DOTALL | re.IGNORECASE,
)
_PDF_HREF_RE = re.compile(r'href="(/media/[^"]+\.pdf)"', re.IGNORECASE)

# Matches YouTube embed links in col-video cells
_VIDEO_CELL_RE = re.compile(
    r'<td[^>]+class="[^"]*col-video[^"]*"[^>]*>(.*?)</td>',
    re.DOTALL | re.IGNORECASE,
)
_YOUTUBE_EMBED_RE = re.compile(
    r'https://www\.youtube\.com/embed/([A-Za-z0-9_-]+)',
    re.IGNORECASE,
)

# Board name from upcoming table
_BOARD_NAME_RE = re.compile(
    r'class="meeting-board[^"]*"[^>]*>(?:<a[^>]*>)?(.*?)(?:</a>)?</span>',
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
    """Return the Last-Modified date of a /media/ PDF as datetime.date, or None."""
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
    """
    Return the YouTube upload date as datetime.date by fetching the watch page
    and extracting the uploadDate field from its JSON-LD / inline metadata.
    Falls back to None on any error.
    """
    req = urllib.request.Request(
        watch_url,
        headers={"User-Agent": UA, "Accept": "text/html,*/*"},
    )
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            page = r.read().decode("utf-8", errors="replace")
        # "uploadDate":"2026-05-13T07:35:37-07:00"
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
    """Download a YouTube video via yt-dlp. Returns True on success."""
    cmd = [
        "yt-dlp", "--no-playlist",
        "-f", "bestvideo+bestaudio/best",
        "--merge-output-format", "mp4",
        "-o", dest_path,
        "--no-overwrites", "--quiet", "--no-warnings",
        watch_url,
    ]
    try:
        subprocess.run(cmd, check=True, timeout=600)
        return True
    except FileNotFoundError:
        print("  ERROR: yt-dlp not found. Install with: pip install yt-dlp",
              file=sys.stderr)
        return False
    except subprocess.CalledProcessError as e:
        print(f"  WARNING: yt-dlp failed ({e})", file=sys.stderr)
        return False


# --- Parsing ---

def parse_meeting_date(text):
    """Parse 'Month D, YYYY' into datetime.date, or None."""
    text = html.unescape(re.sub(r"<[^>]+>", "", text)).strip()
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


def split_rows(html_text):
    """
    Split HTML into individual <tr>...</tr> blocks.
    Handles both plain <tr> (upcoming) and <tr data-year="..."> (past).
    """
    return re.findall(r"<tr(?:\s[^>]*)?>.*?</tr>", html_text, re.DOTALL | re.IGNORECASE)


def extract_docs_from_page(html_text, source_label, meeting_cutoff, include_upcoming=True):
    """
    Parse a meetings page and return lists of PDF candidates and video candidates.

    PDF candidate: {url, doc_type, board_label, meeting_date, source}
    Video candidate: {video_id, watch_url, meeting_date, source}

    meeting_cutoff: oldest meeting date to consider for past-meeting rows.
    Upcoming rows (no data-year) are always included if include_upcoming is True.
    """
    today = datetime.date.today()
    pdf_cands  = []
    vid_cands  = []
    seen_pdf   = set()
    seen_vid   = set()

    for row in split_rows(html_text):
        # Determine whether this is an upcoming row or a past-meeting row
        year_m = re.search(r'data-year="(\d{4})"', row, re.IGNORECASE)
        is_upcoming = (year_m is None)

        # Extract meeting date
        date_m = _DATE_RE.search(row)
        meeting_date = parse_meeting_date(date_m.group(1)) if date_m else None

        # Skip past-meeting rows outside the candidate window
        if not is_upcoming:
            if meeting_date is None:
                continue
            if meeting_date < meeting_cutoff or meeting_date > today:
                continue
        else:
            if not include_upcoming:
                continue

        # Board/meeting label
        board_m = _BOARD_NAME_RE.search(row)
        board_label = (
            html.unescape(re.sub(r"<[^>]+>", "", board_m.group(1))).strip()
            if board_m else source_label
        )

        # PDF links in agenda/minutes cells
        for cell in _PDF_CELL_RE.finditer(row):
            cell_html = cell.group(1)
            doc_type = "agenda" if "col-agenda" in cell.group(0) else "minutes"
            for href_m in _PDF_HREF_RE.finditer(cell_html):
                pdf_url = BASE_URL + href_m.group(1)
                if pdf_url not in seen_pdf:
                    seen_pdf.add(pdf_url)
                    pdf_cands.append({
                        "url":          pdf_url,
                        "doc_type":     doc_type,
                        "board_label":  board_label,
                        "meeting_date": meeting_date,
                        "source":       source_label,
                    })

        # YouTube embed links in col-video cell
        for cell in _VIDEO_CELL_RE.finditer(row):
            cell_html = cell.group(1)
            if "no-file" in cell.group(0):
                continue
            for yt_m in _YOUTUBE_EMBED_RE.finditer(cell_html):
                vid_id = yt_m.group(1)
                if vid_id not in seen_vid:
                    seen_vid.add(vid_id)
                    vid_cands.append({
                        "video_id":     vid_id,
                        "watch_url":    f"https://www.youtube.com/watch?v={vid_id}",
                        "meeting_date": meeting_date,
                        "source":       source_label,
                    })

    return pdf_cands, vid_cands


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


def make_video_dest(meeting_date, output_dir, counter=0):
    date_str = meeting_date.strftime("%Y-%m-%d") if meeting_date else "unknown"
    month_dir = os.path.join(output_dir, (meeting_date or datetime.date.today()).strftime("%Y-%m"))
    os.makedirs(month_dir, exist_ok=True)
    suffix = f"-{counter}" if counter > 0 else ""
    fname = f"{date_str}-board-of-supervisors-video{suffix}.mp4"
    return os.path.join(month_dir, fname)


# --- Main ---

def main():
    parser = argparse.ArgumentParser(
        description=(
            "Download Doylestown Township PA meeting agendas, minutes, and "
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
    print(f"Source            : {BASE_URL}")
    if not args.dry_run:
        print(f"Output dir        : {args.output_dir}")
    print()

    # ------------------------------------------------------------------ #
    # Phase 1: collect all document candidates                             #
    # ------------------------------------------------------------------ #

    all_pdf_cands = []
    all_vid_cands = []

    # Main meetings page — upcoming (all boards) + BoS past (with video)
    url = BASE_URL + MEETINGS_PAGE
    print(f"Fetching main meetings page: {url}")
    html_text = fetch_html(url)
    if not html_text:
        print("ERROR: Could not fetch the main meetings page.", file=sys.stderr)
        sys.exit(1)

    pdfs, vids = extract_docs_from_page(
        html_text, "Board of Supervisors", meeting_cutoff, include_upcoming=True
    )
    print(f"  PDFs: {len(pdfs)}  Videos: {len(vids)}")
    all_pdf_cands.extend(pdfs)
    all_vid_cands.extend(vids)
    time.sleep(PAGE_DELAY)

    # Individual board pages — upcoming + past for each board
    print()
    print(f"Fetching {len(BOARD_SLUGS)} board pages...")
    for slug in BOARD_SLUGS:
        board_url = f"{BASE_URL}/government/boards-commissions/{slug}/"
        html_text = fetch_html(board_url)
        if not html_text:
            print(f"  skip (no content) — {slug}")
            time.sleep(PAGE_DELAY)
            continue
        board_name = re.sub(r"-", " ", slug).title()
        pdfs, vids = extract_docs_from_page(
            html_text, board_name, meeting_cutoff, include_upcoming=True
        )
        # Skip vids from board pages (only BoS has them, already collected)
        print(f"  {slug}: {len(pdfs)} PDF(s)")
        all_pdf_cands.extend(pdfs)
        time.sleep(PAGE_DELAY)

    # Deduplicate by URL
    seen_pdfs = set()
    deduped_pdfs = []
    for c in all_pdf_cands:
        if c["url"] not in seen_pdfs:
            seen_pdfs.add(c["url"])
            deduped_pdfs.append(c)
    all_pdf_cands = deduped_pdfs

    seen_vids = set()
    deduped_vids = []
    for c in all_vid_cands:
        if c["video_id"] not in seen_vids:
            seen_vids.add(c["video_id"])
            deduped_vids.append(c)
    all_vid_cands = deduped_vids

    print()
    print(f"Total PDF candidates : {len(all_pdf_cands)}")
    print(f"Total video candidates: {len(all_vid_cands)}")
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
    # Phase 3: Check YouTube upload dates                                  #
    # ------------------------------------------------------------------ #

    confirmed_vids = []
    if do_video and all_vid_cands:
        print("Checking YouTube upload dates...")
        vid_fname_counters: dict = {}
        for cand in all_vid_cands:
            upload_date = get_youtube_upload_date(cand["watch_url"])
            if upload_date is None or upload_date < cutoff:
                continue
            cand["upload_date"] = upload_date
            key = cand["meeting_date"]
            vid_fname_counters[key] = vid_fname_counters.get(key, 0) + 1
            cand["counter"] = vid_fname_counters[key] - 1
            confirmed_vids.append(cand)
            print(f"  [{upload_date}] Board of Supervisors — {cand['meeting_date']} — {cand['watch_url']}")

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
            print(f"{'Board':<42} {'Posted':<12} Type")
            print("-" * 68)
            for c in confirmed_pdfs:
                print(f"{c['board_label'][:41]:<42} {c['last_modified']!s:<12} {c['doc_type']}")
        if confirmed_vids:
            print()
            print(f"{'Board':<42} {'Uploaded':<12} Meeting date")
            print("-" * 68)
            for c in confirmed_vids:
                print(f"{'Board of Supervisors':<42} {c['upload_date']!s:<12} {c['meeting_date']}")
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
        dest  = make_video_dest(c["meeting_date"], args.output_dir, c["counter"])
        label = os.path.basename(dest)
        if os.path.exists(dest):
            print(f"  skip (exists)  {label}")
            skipped += 1
            continue
        print(f"  [uploaded {c['upload_date']}] Board of Supervisors — {c['meeting_date']} — video")
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
#    python3 scripts/download-doylestown-twp-agendas.py --dry-run
#
# 2. PDFs only (skip video check):
#    python3 scripts/download-doylestown-twp-agendas.py --no-video
#
# 3. Video only:
#    python3 scripts/download-doylestown-twp-agendas.py --video-only
#
# 4. Widen the lookback window:
#    python3 scripts/download-doylestown-twp-agendas.py --days 7
#
# 5. Widen the meeting candidate window (to catch older minutes):
#    python3 scripts/download-doylestown-twp-agendas.py --meeting-lookback 180
#
# 6. Save files somewhere else:
#    python3 scripts/download-doylestown-twp-agendas.py --output-dir ~/Downloads/doylestown-twp
#
# BOARDS (18 as of 2026):
#   Agricultural Security Advisory Council, Bike & Hike Committee,
#   Board of Auditors, Environmental Advisory Council, Finance Committee,
#   Friends of Kids Castle, Historical & Architectural Review Board,
#   Human Relations Commission, Local Traffic Advisory Committee,
#   Municipal Authority (DTMA), Park & Recreation Board,
#   Pension Advisory Committee, Planning Commission,
#   Public Water & Sewer Advisory Board, Telecommunications Advisory Board,
#   UCC Board of Appeals, Veterans Advisory Committee, Zoning Hearing Board.
#   Board of Supervisors: past meetings (+ video) on /government/meetings/
