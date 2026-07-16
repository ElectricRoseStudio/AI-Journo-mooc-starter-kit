#!/usr/bin/env python3
# download-wallingford-agendas.py
# Download municipal meeting agendas, minutes, and YouTube video recordings
# from Wallingford CT for meetings within the past N days (and up to 7 days
# ahead, to catch agendas posted early for upcoming meetings).
#
# USAGE:
#   python3 scripts/download-wallingford-agendas.py [options]
#
# REQUIREMENTS:
#   - Python 3.6+  (no third-party packages needed for docs)
#   - yt-dlp  (for video:  pip install yt-dlp  or  sudo apt install yt-dlp)
#
# WHAT IT DOES:
#   1. Fetches the Wallingford CT minutes-and-agendas page (a single static
#      HTML page containing all documents for all boards)
#   2. Parses the ul.fileList hierarchy to identify boards and their
#      year-archive subfolders
#   3. For each document link, parses the meeting date from the title text
#   4. Downloads Agenda and Minutes documents whose meeting date falls within
#      the date window to beat-archive/wallingford-agendas/YYYY-MM/
#   5. Optionally queries the Wallingford Government Television YouTube channel
#      (WGTV) for meeting recordings within the window and downloads them via
#      yt-dlp.  Dates are parsed from video titles since upload dates only
#      roughly correlate with meeting dates.
#   6. Appends a download log to beat-archive/wallingford-agendas/download-log.txt
#
# SITE STRUCTURE:
#   Wallingford CT uses a custom CMS. All 3,500+ documents are embedded in a
#   single static HTML page at /minutes-and-agendas/. No JavaScript or
#   authentication is required.
#
#   Page structure:
#     ul.fileList
#       li.dir  → top-level board folder (e.g., "Town Council")
#         ul
#           li.dir  → year archive subfolder (e.g., "2025 Town Council Archive")
#             ul
#               li.pdf  → document link
#
#   Document link example:
#     <a href="/minutes-and-agendas/DownloadFile.aspx?FileID=10739" class="pdf"
#        title="TCRMMinutes.12.12.23.pdf">
#       <strong>Minutes of Regular Meeting - December 12, 2023</strong>
#     </a>
#
#   Document type is identified from the title prefix:
#     "Agenda ..."        → agenda
#     "Minutes ..."       → minutes
#     "Amended Agenda ..."  → agenda (variant)
#     "Amended Minutes ..." → minutes (variant)
#
#   Meeting date is parsed from the title after " - ": "December 12, 2023"
#
#   Download URL:
#     https://www.wallingfordct.gov/minutes-and-agendas/DownloadFile.aspx?FileID=NNNN
#
# VIDEO SOURCE:
#   Wallingford Government Television (WGTV) posts meeting recordings to:
#   https://www.youtube.com/channel/UCdWP8OnNWc1nyrtewoqYRpQ
#   Meetings are also broadcast on Comcast Xfinity Ch. 20/1084 and
#   Frontier Vantage position 99 (cable TV only; not downloadable).

import argparse
import datetime
import glob
import html as html_module
import os
import re
import subprocess
import sys
import time
import urllib.error
import urllib.request

YT_DLP_NODE = "node:/home/richkirby/.local/bin/yt-dlp-node"  # yt-dlp needs Node 22+; symlink kept current by scripts/update-yt-dlp-node.sh

# --- Configuration ---
BASE_URL    = "https://www.wallingfordct.gov"
LISTING_URL = f"{BASE_URL}/minutes-and-agendas/"
OUTPUT_DIR  = "beat-archive/wallingford-agendas"
DAYS_BACK   = 4
DAYS_AHEAD  = 7    # capture agendas posted early for upcoming meetings
DELAY       = 1.0

# Wallingford Government Television YouTube channel
YOUTUBE_CHANNEL = "https://www.youtube.com/channel/UCdWP8OnNWc1nyrtewoqYRpQ"

UA = "Wallingford-Agendas-Downloader/1.0 (journalism research)"

MONTHS = {
    "January": 1, "February": 2, "March": 3, "April": 4,
    "May": 5, "June": 6, "July": 7, "August": 8,
    "September": 9, "October": 10, "November": 11, "December": 12,
}

# Date pattern for PDF titles: "- Month DD, YYYY" at end of string
_PDF_DATE_RE = re.compile(
    r"-\s+(January|February|March|April|May|June|July|August"
    r"|September|October|November|December)"
    r"\s+(\d{1,2}),?\s+(\d{4})\s*$"
)

# Date patterns for video titles (various positions/formats)
_VIDEO_DATE_PATS = [
    # Month DD, YYYY  (or  Month D, YYYY)
    re.compile(
        r"\b(January|February|March|April|May|June|July|August"
        r"|September|October|November|December)\s+(\d{1,2}),?\s+(20\d{2})\b",
        re.I,
    ),
    # MM/DD/YYYY or MM-DD-YYYY or MM.DD.YYYY
    re.compile(r"\b(\d{1,2})[./\-](\d{1,2})[./\-](20\d{2})\b"),
    # YYYY-MM-DD
    re.compile(r"\b(20\d{2})[.\-](\d{1,2})[.\-](\d{1,2})\b"),
]


def _safe_date(y, m, d):
    try:
        return datetime.date(y, m, d)
    except ValueError:
        return None


def parse_pdf_date(title):
    """Parse meeting date from a Wallingford PDF title ('- Month DD, YYYY' suffix)."""
    m = _PDF_DATE_RE.search(title)
    if not m:
        return None
    return _safe_date(int(m.group(3)), MONTHS[m.group(1)], int(m.group(2)))


def parse_video_date(title):
    """Parse meeting date from a WGTV video title. Returns date or None."""
    for pat in _VIDEO_DATE_PATS:
        match = pat.search(title)
        if match:
            g = match.groups()
            try:
                if not g[0].isdigit():  # Month DD YYYY
                    d = _safe_date(int(g[2]), MONTHS[g[0].capitalize()], int(g[1]))
                elif len(g[0]) == 4:    # YYYY MM DD
                    d = _safe_date(int(g[0]), int(g[1]), int(g[2]))
                else:                   # MM DD YYYY
                    d = _safe_date(int(g[2]), int(g[0]), int(g[1]))
                if d:
                    return d
            except (KeyError, ValueError):
                continue
    return None


# --- HTTP helpers ---

def fetch_html(url):
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "text/html"})
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            return r.read().decode("utf-8", errors="replace")
    except urllib.error.URLError as e:
        print(f"  ERROR fetching {url}: {e}", file=sys.stderr)
        return None


def download_file(file_id, dest_path):
    """Download a Wallingford document by FileID."""
    url = f"{LISTING_URL}DownloadFile.aspx?FileID={file_id}"
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            with open(dest_path, "wb") as f:
                f.write(r.read())
        return True
    except Exception as e:
        print(f"  WARNING: {e}", file=sys.stderr)
        return False


# --- HTML parsing ---

def parse_listing(html):
    """
    Parse the ul.fileList hierarchy and return a list of document dicts:
      {board_name, year_folder, year, title, file_id}
    """
    docs = []
    fl_match = re.search(r'<ul[^>]+class="fileList"', html, re.IGNORECASE)
    if not fl_match:
        return docs

    chunk = html[fl_match.start():]
    token_re = re.compile(
        r'(<ul[^>]*>)'
        r'|(</ul>)'
        r'|(<a\b[^>]+class="dir"[^>]*>)(.*?)(</a>)'
        r'|(<a\b[^>]*href="[^"]*FileID=(\d+)[^"]*"[^>]*>)(.*?)(</a>)',
        re.IGNORECASE | re.DOTALL,
    )

    ul_depth = 0
    current_board = None
    current_year = None

    for m in token_re.finditer(chunk):
        if m.group(1):      # <ul>
            ul_depth += 1
        elif m.group(2):    # </ul>
            ul_depth -= 1
            if ul_depth == 1:
                current_board = None
                current_year = None
        elif m.group(3):    # <a class="dir"> — folder label
            label_m = re.search(r"<strong>([^<]+)</strong>", m.group(4) or "", re.I)
            if label_m:
                label = html_module.unescape(label_m.group(1).strip())
                if ul_depth == 1:
                    current_board = label
                elif ul_depth == 2:
                    current_year = label
        elif m.group(6):    # <a href="...FileID=N..."> — document
            if not current_board:
                continue
            file_id = m.group(7)
            title_m = re.search(r"<strong>([^<]+)</strong>", m.group(8) or "", re.I)
            if not title_m:
                continue
            title = html_module.unescape(title_m.group(1).strip())
            year_val = None
            if current_year:
                ym = re.search(r"\b(\d{4})\b", current_year)
                if ym:
                    year_val = int(ym.group(1))
            docs.append({
                "board_name":  current_board,
                "year_folder": current_year,
                "year":        year_val,
                "title":       title,
                "file_id":     file_id,
            })

    return docs


# --- Utilities ---

def classify_doc_type(title):
    lower = title.lower()
    if re.match(r"(amended\s+|special\s+|revised\s+)?agenda\b", lower):
        return "agenda"
    if re.match(r"(amended\s+|special\s+|revised\s+)?minutes\b", lower):
        return "minutes"
    return None


def slugify(text, max_len=60):
    text = text.lower().strip()
    text = re.sub(r"[/\\]", "-", text)
    text = re.sub(r"\s+-\s+", "-", text)
    text = re.sub(r"[^\w\s-]", "", text)
    text = re.sub(r"[\s_]+", "-", text)
    text = re.sub(r"-{2,}", "-", text)
    return text.strip("-")[:max_len]


def month_dir(date, output_dir):
    path = os.path.join(output_dir, date.strftime("%Y-%m"))
    os.makedirs(path, exist_ok=True)
    return path


def make_doc_dest(board_name, doc_type, meeting_date, output_dir, suffix=""):
    d        = month_dir(meeting_date, output_dir)
    date_str = meeting_date.strftime("%Y-%m-%d")
    board    = slugify(board_name, max_len=40)
    dtype    = slugify(doc_type, max_len=10)
    return os.path.join(d, f"{date_str}-{board}-{dtype}{suffix}.pdf")


# --- YouTube / yt-dlp helpers ---

def _ytdlp_available():
    try:
        r = subprocess.run(["yt-dlp", "--js-runtimes", YT_DLP_NODE, "--version"], capture_output=True, timeout=5)
        return r.returncode == 0
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return False


def list_channel_videos(channel_url):
    """
    Return list of (video_id, title) for every video on the channel.
    Uses --flat-playlist for speed (single API call, ~1-2 seconds).
    """
    cmd = [
        "yt-dlp", "--js-runtimes", YT_DLP_NODE,
        "--flat-playlist",
        "--print", "%(id)s\t%(title)s",
        "--no-warnings",
        channel_url,
    ]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
    except subprocess.TimeoutExpired:
        print("  WARNING: yt-dlp channel listing timed out.", file=sys.stderr)
        return []
    videos = []
    for line in result.stdout.splitlines():
        parts = line.split("\t", 1)
        if len(parts) == 2:
            videos.append((parts[0].strip(), parts[1].strip()))
    return videos


def video_dest_template(date, title, output_dir):
    d        = month_dir(date, output_dir)
    date_str = date.strftime("%Y-%m-%d")
    slug     = slugify(title, max_len=60)
    return os.path.join(d, f"{date_str}-{slug}.%(ext)s")


def video_already_exists(dest_template):
    base = dest_template.replace(".%(ext)s", "")
    return bool(glob.glob(base + ".*"))


def download_youtube_video(video_id, dest_template, dry_run=False):
    url = f"https://www.youtube.com/watch?v={video_id}"
    if dry_run:
        print(f"    [dry-run] would download: {url}")
        return True
    cmd = [
        "yt-dlp", "--js-runtimes", YT_DLP_NODE,
        "--no-playlist",
        "-f", "bestvideo+bestaudio/best",
        "--merge-output-format", "mp4",
        "-o", dest_template,
        "--no-overwrites",
        "--quiet", "--no-warnings",
        url,
    ]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=7200)
    except subprocess.TimeoutExpired:
        print(f"  WARNING: yt-dlp timed out downloading {url}", file=sys.stderr)
        return False
    if result.returncode != 0 and result.stderr:
        print(f"  WARNING: yt-dlp: {result.stderr[:300]}", file=sys.stderr)
    return result.returncode == 0


# --- Main ---

def main():
    parser = argparse.ArgumentParser(
        description=(
            "Download Wallingford CT municipal agendas, minutes, and WGTV "
            "YouTube meeting recordings for meetings within the past N days."
        )
    )
    parser.add_argument("--days", type=int, default=DAYS_BACK, metavar="N",
                        help=f"Look back N days by meeting date (default: {DAYS_BACK})")
    parser.add_argument("--ahead", type=int, default=DAYS_AHEAD, metavar="N",
                        help=f"Also include meetings up to N days ahead (default: {DAYS_AHEAD})")
    parser.add_argument("--output-dir", default=OUTPUT_DIR, metavar="DIR",
                        help=f"Destination directory (default: {OUTPUT_DIR})")
    parser.add_argument("--dry-run", action="store_true",
                        help="List matching documents/videos without downloading")
    parser.add_argument("--board", metavar="NAME",
                        help="Only process boards whose name contains NAME (case-insensitive)")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--include-video", action="store_true",
                      help="Download both documents and WGTV YouTube recordings")
    mode.add_argument("--video-only", action="store_true",
                      help="Download WGTV YouTube recordings only (skip PDFs)")
    mode.add_argument("--docs-only", action="store_true",
                      help="Download PDFs only (skip video)")
    args = parser.parse_args()

    now = datetime.datetime.now()
    if (now.weekday() == 5 and now.hour >= 18) or (now.weekday() == 6 and now.hour < 12):  # Saturday night, Sunday morning
        print("Skipping — no downloads on Saturday nights or Sunday mornings.")
        sys.exit(0)

    do_docs  = not args.video_only
    do_video = args.include_video or args.video_only

    today        = datetime.date.today()
    cutoff       = today - datetime.timedelta(days=args.days)
    future_limit = today + datetime.timedelta(days=args.ahead)

    has_ytdlp = _ytdlp_available()

    print(f"Date window : {cutoff} to {future_limit}")
    print(f"Site        : {LISTING_URL}")
    if do_video:
        print(f"Video       : enabled (WGTV YouTube / yt-dlp"
              f"{'  *** NOT FOUND ***' if not has_ytdlp else ''})")
    if not args.dry_run:
        print(f"Output dir  : {args.output_dir}")
    print()

    log_lines = []
    dl_ok = dl_skip = dl_fail = 0
    vd_ok = vd_skip = vd_fail = 0

    # --- Documents (PDFs) ---
    if do_docs:
        print("Fetching minutes-and-agendas listing page (may be large)...")
        html = fetch_html(LISTING_URL)
        if not html:
            print("ERROR: Could not fetch the listing page.", file=sys.stderr)
            sys.exit(1)

        all_docs = parse_listing(html)
        print(f"Parsed {len(all_docs)} document entries from listing page.")
        print()

        matches = []
        for doc in all_docs:
            if args.board and args.board.lower() not in doc["board_name"].lower():
                continue
            doc_type = classify_doc_type(doc["title"])
            if not doc_type:
                continue
            meeting_date = parse_pdf_date(doc["title"])
            if not meeting_date:
                continue
            if meeting_date < cutoff or meeting_date > future_limit:
                continue
            matches.append({
                "board":        doc["board_name"],
                "meeting_date": meeting_date,
                "doc_type":     doc_type,
                "title":        doc["title"],
                "file_id":      doc["file_id"],
            })

        # Assign disambiguation suffixes for same (board, type, date) duplicates
        seen_keys: dict = {}
        for m in matches:
            key = (m["board"], m["doc_type"], m["meeting_date"])
            seen_keys[key] = seen_keys.get(key, 0) + 1
        key_counter: dict = {}
        for m in matches:
            key = (m["board"], m["doc_type"], m["meeting_date"])
            if seen_keys[key] > 1:
                key_counter[key] = key_counter.get(key, 0) + 1
                m["suffix"] = f"-{key_counter[key]}"
            else:
                m["suffix"] = ""

        matches.sort(key=lambda x: (x["meeting_date"], x["board"]), reverse=True)

        print(
            f"Found {len(matches)} document(s) across "
            f"{len({m['board'] for m in matches})} board(s)."
        )
        print()

        if args.dry_run:
            print(f"{'Board':<42} {'Date':<12} Type")
            print("-" * 68)
            for m in matches:
                print(f"{m['board'][:41]:<42} {m['meeting_date']!s:<12} {m['doc_type']}")
        else:
            os.makedirs(args.output_dir, exist_ok=True)
            for m in matches:
                dest = make_doc_dest(
                    m["board"], m["doc_type"], m["meeting_date"],
                    args.output_dir, suffix=m.get("suffix", ""),
                )
                label = os.path.basename(dest)
                if os.path.exists(dest):
                    print(f"  skip (exists)  {label}")
                    dl_skip += 1
                    continue
                print(f"  [{m['meeting_date']}] {m['board']} — {m['doc_type']}")
                print(f"  downloading    {label}")
                if download_file(m["file_id"], dest):
                    dl_ok += 1
                    log_lines.append(
                        f"{datetime.datetime.now().isoformat()}  OK       {dest}"
                    )
                else:
                    dl_fail += 1
                    log_lines.append(
                        f"{datetime.datetime.now().isoformat()}  FAILED   "
                        f"FileID={m['file_id']}  {m['title']}"
                    )
                    if os.path.exists(dest):
                        os.remove(dest)
                time.sleep(DELAY)

        print()

    # --- YouTube video recordings ---
    if do_video:
        if not has_ytdlp and not args.dry_run:
            print("WARNING: yt-dlp not found — skipping video.", file=sys.stderr)
            print("  Install with:  pip install yt-dlp  or  sudo apt install yt-dlp",
                  file=sys.stderr)
        else:
            print("Fetching WGTV YouTube channel listing...")
            all_videos = list_channel_videos(YOUTUBE_CHANNEL)
            print(f"Found {len(all_videos)} video(s) on channel. Filtering by date window...")

            video_matches = []
            for vid_id, title in all_videos:
                vdate = parse_video_date(title)
                if not vdate:
                    continue
                if cutoff <= vdate <= future_limit:
                    video_matches.append((vdate, vid_id, title))

            video_matches.sort(key=lambda x: x[0], reverse=True)
            print(f"Found {len(video_matches)} meeting recording(s) in window.")
            print()

            for vdate, vid_id, title in video_matches:
                tmpl   = video_dest_template(vdate, title, args.output_dir)
                header = f"  [{vdate}] {title}"

                if args.dry_run:
                    print(header)
                    print(f"    {os.path.basename(tmpl)}")
                    print(f"    https://www.youtube.com/watch?v={vid_id}")
                    continue

                print(header)
                if video_already_exists(tmpl):
                    existing = glob.glob(tmpl.replace(".%(ext)s", ".*"))[0]
                    print(f"    skip (exists)  {os.path.basename(existing)}")
                    vd_skip += 1
                    continue

                print(f"    downloading    {os.path.basename(tmpl)}")
                print(f"    source URL:    https://www.youtube.com/watch?v={vid_id}")
                os.makedirs(os.path.dirname(tmpl), exist_ok=True)
                if download_youtube_video(vid_id, tmpl):
                    vd_ok += 1
                    log_lines.append(
                        f"{datetime.datetime.now().isoformat()}  OK       "
                        f"https://www.youtube.com/watch?v={vid_id}  {title}"
                    )
                else:
                    vd_fail += 1
                    log_lines.append(
                        f"{datetime.datetime.now().isoformat()}  FAILED   "
                        f"https://www.youtube.com/watch?v={vid_id}  {title}"
                    )

    if not args.dry_run:
        if log_lines:
            log_path = os.path.join(args.output_dir, "download-log.txt")
            os.makedirs(args.output_dir, exist_ok=True)
            with open(log_path, "a") as f:
                f.write("\n".join(log_lines) + "\n")

        if do_docs:
            print(f"Documents  — downloaded: {dl_ok}  skipped: {dl_skip}  failed: {dl_fail}")
        if do_video:
            print(f"Video      — downloaded: {vd_ok}  skipped: {vd_skip}  failed: {vd_fail}")
        if dl_ok + dl_skip + vd_ok + vd_skip:
            print(f"Files in: {args.output_dir}")
        if log_lines:
            print(f"Log:      {os.path.join(args.output_dir, 'download-log.txt')}")

    elif args.dry_run and (do_docs or do_video):
        print("Re-run without --dry-run to download.")


if __name__ == "__main__":
    main()


# --- Tips ---
#
# Preview without downloading (documents only, 30-day window):
#   python3 scripts/download-wallingford-agendas.py --dry-run
#
# Download documents + WGTV meeting recordings:
#   python3 scripts/download-wallingford-agendas.py --include-video
#
# Video recordings only:
#   python3 scripts/download-wallingford-agendas.py --video-only
#
# Preview video recordings in window:
#   python3 scripts/download-wallingford-agendas.py --video-only --dry-run
#
# Narrow to one board (documents + video):
#   python3 scripts/download-wallingford-agendas.py --include-video --board "Town Council"
#
# Change the lookback window:
#   python3 scripts/download-wallingford-agendas.py --include-video --days 15
#
# Run daily via cron (7 AM):
#   0 7 * * * cd /path/to/repo && python3 scripts/download-wallingford-agendas.py --include-video
#
# NOTE: The --ahead flag (default: 7 days) captures agendas for upcoming meetings
# already published. Run daily to stay current.
#
# NOTE: Video titles on WGTV include the meeting date in most cases
# (e.g. "Planning & Zoning Commission - Regular Meeting - Monday, April 13, 2026").
# Videos without a recognizable date (community events, highlights, etc.) are
# skipped automatically.
#
# NOTE: The listing page is large (~700 KB). Each run fetches it once, then
# filters entirely in memory — no per-board requests needed.
#
# VIDEO SOURCE:
#   Wallingford Government Television (WGTV)
#   https://www.youtube.com/channel/UCdWP8OnNWc1nyrtewoqYRpQ
#   Also broadcasts live and on-demand on:
#     Comcast Xfinity — Channel 20 or 1084
#     Frontier Vantage — Position 99
