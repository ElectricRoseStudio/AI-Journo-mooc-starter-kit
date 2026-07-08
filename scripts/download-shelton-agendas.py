#!/usr/bin/env python3
# download-shelton-agendas.py
# Download municipal meeting agendas, minutes, and YouTube recording links
# from Shelton CT for documents/videos dated within the past N days.
#
# USAGE:
#   python3 scripts/download-shelton-agendas.py [options]
#
# REQUIREMENTS:
#   - Python 3.6+  (no third-party packages needed)
#   - yt-dlp  (for YouTube recording links — install: pip install yt-dlp)
#   - Internet connection
#
# WHAT IT DOES:
#   1. Fetches the Shelton minutes-and-agendas hub page to discover all boards
#      and their document library folder IDs
#   2. For each board, fetches the year subfolder(s) covering the lookback window
#   3. Parses each file listing for documents whose title begins with a MMDDYYYY
#      date that falls within the date window
#   4. Downloads matching PDFs to beat-archive/shelton-agendas/YYYY-MM/
#   5. Lists videos from the two Shelton YouTube channels, parses meeting dates
#      from video titles, and saves matching videos as .url shortcut files
#   6. Appends a download log to beat-archive/shelton-agendas/download-log.txt
#
# SITE STRUCTURE (QScend / Catalis CMS):
#   Hub:      https://www.cityofshelton.org/p/minutes-agendas
#   Doc API:  GET /Home/Documents?dirId={uuid}&mainDirId={root_uuid}
#             Returns an HTML fragment with:
#               <ul class="directory-list"> — subfolder list (year folders, then meeting folders)
#               <ul class="file-list">     — file list with DownloadDocument links
#   Download: GET /Home/DownloadDocument?docId={uuid}  → application/pdf
#
#   Structure: Main root → Board folder → Year folder → [Meeting subfolder →] files
#   File titles use a MMDDYYYY prefix to encode the meeting date, e.g.:
#     "04102025 BOA Regular Meeting Agenda" → April 10, 2025
#
# YOUTUBE CHANNELS:
#   City Hall:              https://www.youtube.com/channel/UCOm-u1DcLoOFmVnnCgxIm9w
#   Conservation Commission:https://www.youtube.com/channel/UCdNSokFtzuiCjBd7QeASEXw
#   Videos are listed via yt-dlp --flat-playlist. Meeting dates are parsed from
#   video titles (the channels use several date formats — see parse_date_from_title).
#   Videos are saved as Windows Internet Shortcut (.url) files; they are NOT
#   downloaded as video files. Note: City Hall videos were uploaded as live streams
#   and may require a YouTube account to view.
#
# NOTE: The site serves an SSL certificate whose CN does not always match
# "www.cityofshelton.org" depending on which load-balanced server responds.
# The script disables Python's hostname verification to work around this.
# The server IS using TLS; traffic is still encrypted.

import argparse
import datetime
import html
import json
import os
import re
import ssl
import subprocess
import sys
import time

YT_DLP_NODE = "node:/home/richkirby/.local/bin/yt-dlp-node"  # yt-dlp needs Node 22+; symlink kept current by scripts/update-yt-dlp-node.sh
import urllib.error
import urllib.parse
import urllib.request

# --- Configuration ---
BASE_URL = "https://www.cityofshelton.org"
HUB_URL = f"{BASE_URL}/p/minutes-agendas"
DOC_API = f"{BASE_URL}/Home/Documents"
DOWNLOAD_URL = f"{BASE_URL}/Home/DownloadDocument"
OUTPUT_DIR = "beat-archive/shelton-agendas"
DAYS_BACK = 4
DELAY_SECONDS = 0.8

# YouTube channels for meeting recordings
YT_CHANNELS = [
    ("UCOm-u1DcLoOFmVnnCgxIm9w", "City Hall"),
    ("UCdNSokFtzuiCjBd7QeASEXw", "Conservation Commission"),
]

UA = "Mozilla/5.0"

# SSL context — site has a cert hostname mismatch on some servers
_SSL_CTX = ssl.create_default_context()
_SSL_CTX.check_hostname = False
_SSL_CTX.verify_mode = ssl.CERT_NONE


# --- HTTP helpers ---

def fetch_html(url, params=None, retries=3, accept=None):
    """GET url and return decoded HTML, or None on error.

    NOTE: The cityofshelton.org server has Accept-header routing quirks:
      - The hub page (/p/minutes-agendas) requires Accept: text/html
      - The Documents API (/Home/Documents) requires Accept: */* (or no Accept)
    Pass accept='text/html' for hub-page fetches; omit for API fetches.

    The server is load-balanced and intermittently returns 404 on certain
    backends even for valid URLs. Retries with a short delay handle this.
    """
    full_url = url
    if params:
        full_url += "?" + urllib.parse.urlencode(params)
    headers = {"User-Agent": UA}
    if accept:
        headers["Accept"] = accept
    req = urllib.request.Request(full_url, headers=headers)
    for attempt in range(retries + 1):
        try:
            with urllib.request.urlopen(req, timeout=30, context=_SSL_CTX) as r:
                raw = r.read()
                charset = r.headers.get_content_charset() or "utf-8"
                return raw.decode(charset, errors="replace")
        except urllib.error.HTTPError as e:
            if e.code == 404 and attempt < retries:
                time.sleep(1.5)
                continue
            if e.code != 404:
                print(f"  HTTP {e.code} — {full_url}", file=sys.stderr)
            return None
        except Exception as e:
            if attempt < retries:
                time.sleep(1)
                continue
            print(f"  ERROR fetching {full_url}: {e}", file=sys.stderr)
            return None
    return None


def download_pdf(doc_id, dest_path, retries=3):
    """Download a document by docId to dest_path. Returns True on success."""
    url = f"{DOWNLOAD_URL}?docId={doc_id}"
    req = urllib.request.Request(
        url,
        headers={"User-Agent": UA, "Accept": "application/pdf, */*"},
    )
    for attempt in range(retries + 1):
        try:
            with urllib.request.urlopen(req, timeout=60, context=_SSL_CTX) as r:
                with open(dest_path, "wb") as f:
                    f.write(r.read())
            return True
        except urllib.error.HTTPError as e:
            if e.code == 404 and attempt < retries:
                time.sleep(1.5)
                continue
            print(f"  WARNING: HTTP {e.code} — {url}", file=sys.stderr)
            return False
        except Exception as e:
            if attempt < retries:
                time.sleep(1)
                continue
            print(f"  WARNING: {e}", file=sys.stderr)
            return False
    return False


# --- YouTube helpers ---

# Date patterns found in Shelton video titles (tried in order):
#   YYYY-MM-DD                  "2023-07-12 Meeting Conservation Commission"
#   MM/DD/YYYY or M/DD/YYYY     "SPZC 08/13/2025", "Inland Wetlands Meeting 6/19/2025"
#   MM-DD-YYYY or M-DD-YYYY     "Pension Board 8-20-2025", "P&Z 8-30-2023"
#   MMDDYY (6 digits)           "WPCA 011425"  → Jan 14 2025
#   M DD YYYY / MM DD YYYY      "SPZC 2 11 2026", "Board of A&T 4 10 2025"
_YT_DATE_PATTERNS = [
    # YYYY-MM-DD
    re.compile(r'\b(20\d{2})[-/](0?[1-9]|1[0-2])[-/](0?[1-9]|[12]\d|3[01])\b'),
    # MM/DD/YYYY or M/DD/YYYY
    re.compile(r'\b(0?[1-9]|1[0-2])/(0?[1-9]|[12]\d|3[01])/(20\d{2})\b'),
    # MM-DD-YYYY or M-DD-YYYY
    re.compile(r'\b(0?[1-9]|1[0-2])-(0?[1-9]|[12]\d|3[01])-(20\d{2})\b'),
    # MMDDYY compact (6 digits)
    re.compile(r'\b(\d{2})(\d{2})(\d{2})\b'),
    # M DD YYYY or MM DD YYYY (space-separated)
    re.compile(r'\b(0?[1-9]|1[0-2])\s+(0?[1-9]|[12]\d|3[01])\s+(20\d{2})\b'),
]


def parse_date_from_video_title(title):
    """
    Extract a meeting date from a YouTube video title using several common
    Shelton formats. Returns a datetime.date or None.
    """
    for i, pat in enumerate(_YT_DATE_PATTERNS):
        m = pat.search(title)
        if not m:
            continue
        try:
            if i == 0:      # YYYY-MM-DD
                return datetime.date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
            elif i in (1, 2):  # MM/DD/YYYY or MM-DD-YYYY
                return datetime.date(int(m.group(3)), int(m.group(1)), int(m.group(2)))
            elif i == 3:    # MMDDYY → guess century
                yy = int(m.group(3))
                yyyy = 2000 + yy if yy <= 50 else 1900 + yy
                return datetime.date(yyyy, int(m.group(1)), int(m.group(2)))
            else:           # M DD YYYY space-separated
                return datetime.date(int(m.group(3)), int(m.group(1)), int(m.group(2)))
        except ValueError:
            continue
    return None


def fetch_channel_videos(channel_id, label):
    """
    Use yt-dlp --flat-playlist to list videos in a YouTube channel.
    Returns a list of {"id": ..., "title": ..., "url": ..., "label": ...}.
    Returns [] if yt-dlp is not available or the channel is unreachable.
    """
    url = f"https://www.youtube.com/channel/{channel_id}/videos"
    try:
        result = subprocess.run(
            ["yt-dlp", "--js-runtimes", YT_DLP_NODE, "--flat-playlist", "-J", url],
            capture_output=True, text=True, timeout=60,
        )
    except FileNotFoundError:
        print(f"  WARNING: yt-dlp not found — skipping YouTube ({label})", file=sys.stderr)
        return []
    except subprocess.TimeoutExpired:
        print(f"  WARNING: yt-dlp timed out for {label}", file=sys.stderr)
        return []

    if result.returncode != 0:
        print(f"  WARNING: yt-dlp error for {label}: {result.stderr[:120]}", file=sys.stderr)
        return []

    try:
        data = json.loads(result.stdout)
    except json.JSONDecodeError:
        return []

    videos = []
    for entry in data.get("entries", []):
        vid_id = entry.get("id", "")
        title = entry.get("title", "")
        if not vid_id or not title:
            continue
        videos.append({
            "id": vid_id,
            "title": title,
            "url": f"https://www.youtube.com/watch?v={vid_id}",
            "label": label,
        })
    return videos


def save_url_shortcut(url, dest_path):
    """Save a URL as a Windows Internet Shortcut (.url) file. Returns True."""
    with open(dest_path, "w") as f:
        f.write(f"[InternetShortcut]\nURL={url}\n")
    return True


# --- Parsing helpers ---

def parse_dirs(html_text):
    """Return list of (uuid, name) for subdirectories in a Documents response."""
    return [
        (uuid, html.unescape(name.strip()))
        for uuid, name in re.findall(
            r'data-directory="([a-f0-9-]{36})"[^>]*>.*?<i[^>]*></i>([^<]+)',
            html_text,
        )
    ]


def parse_files(html_text):
    """Return list of (doc_id, title) for downloadable files in a Documents response."""
    return [
        (m[0], html.unescape(m[1].strip()))
        for m in re.findall(
            r'/Home/DownloadDocument\?docId=([a-f0-9-]{36})[^"]*"[^>]*>([^<]+)',
            html_text,
        )
    ]


def parse_main_dir(hub_html):
    """Extract the root document library directory UUID from the hub page."""
    m = re.search(r'id="main-dir-id" data-directory="([a-f0-9-]{36})"', hub_html)
    return m.group(1) if m else None


def parse_board_dirs(hub_html):
    """
    Extract (board_name, dir_uuid) pairs from the hub page directory listing.
    Returns a dict keyed by board name.
    """
    entries = re.findall(
        r'data-directory="([a-f0-9-]{36})"[^>]*>.*?<i[^>]*></i>([^<]+)',
        hub_html,
    )
    boards = {}
    for uuid, raw_name in entries:
        name = html.unescape(raw_name.strip())
        boards[name] = uuid
    return boards


_DATE_RE = re.compile(
    r"^(\d{1,2})(\d{2})(\d{4})\b"   # MMDDYYYY or MDDYYYY at start of title
)


def parse_title_date(title):
    """
    Try to extract a date from a MMDDYYYY or MDDYYYY prefix in the document title.
    Returns a datetime.date or None.
    """
    # Clean up leading spaces/numbers that got concatenated (e.g. "111320205" typos)
    stripped = title.strip()
    # Match 7 or 8 leading digits
    m = re.match(r"^(\d{7,8})\b", stripped)
    if not m:
        return None
    digits = m.group(1)
    # Try MMDDYYYY (8 digits)
    if len(digits) == 8:
        mm, dd, yyyy = int(digits[:2]), int(digits[2:4]), int(digits[4:])
        try:
            return datetime.date(yyyy, mm, dd)
        except ValueError:
            pass
    # Try MDDYYYY (7 digits)
    if len(digits) == 7:
        mm, dd, yyyy = int(digits[:1]), int(digits[1:3]), int(digits[3:])
        try:
            return datetime.date(yyyy, mm, dd)
        except ValueError:
            pass
    return None


def slugify(text, max_len=55):
    text = text.lower().strip()
    text = html.unescape(text)
    text = re.sub(r"[/\\&]", "-", text)
    text = re.sub(r"[^\w\s-]", "", text)
    text = re.sub(r"[\s_]+", "-", text)
    text = re.sub(r"-{2,}", "-", text)
    return text.strip("-")[:max_len]


def make_dest_path(board_name, doc_title, doc_id, meeting_date, output_dir):
    date_str = meeting_date.strftime("%Y-%m-%d")
    month_dir = os.path.join(output_dir, meeting_date.strftime("%Y-%m"))
    os.makedirs(month_dir, exist_ok=True)
    board_slug = slugify(board_name, max_len=30)
    title_slug = slugify(doc_title, max_len=45)
    fname = f"{date_str}-{board_slug}-{title_slug}-{doc_id[:8]}.pdf"
    return os.path.join(month_dir, fname)


def make_video_dest_path(video_id, video_title, meeting_date, output_dir):
    date_str = meeting_date.strftime("%Y-%m-%d")
    month_dir = os.path.join(output_dir, meeting_date.strftime("%Y-%m"))
    os.makedirs(month_dir, exist_ok=True)
    title_slug = slugify(video_title, max_len=55)
    fname = f"{date_str}-{title_slug}-{video_id[:8]}.url"
    return os.path.join(month_dir, fname)


# --- Core logic ---

def collect_docs_from_dir(dir_id, main_dir_id, board_name, cutoff, future_limit,
                          depth=0):
    """
    Recursively collect documents from a directory.
    Returns a list of {board, doc_id, title, meeting_date}.
    depth=0 → board dir (contains year subdirs)
    depth=1 → year dir (contains meeting subdirs + files)
    depth=2 → meeting subdir (contains files)
    """
    if depth > 2:
        return []

    html_text = fetch_html(DOC_API, {"dirId": dir_id, "mainDirId": main_dir_id}, accept=None)
    if not html_text:
        return []

    results = []

    if depth == 0:
        # Board level — only recurse into year subdirs that overlap the date window
        subdirs = parse_dirs(html_text)
        for subdir_id, subdir_name in subdirs:
            # Subdir names at this level are years, e.g. "2025"
            if re.match(r"^\d{4}$", subdir_name.strip()):
                year = int(subdir_name.strip())
                if cutoff.year <= year <= future_limit.year:
                    time.sleep(DELAY_SECONDS * 0.5)
                    results.extend(
                        collect_docs_from_dir(
                            subdir_id, main_dir_id, board_name,
                            cutoff, future_limit, depth=1,
                        )
                    )
        return results

    elif depth == 1:
        # Year level — collect direct files AND recurse into meeting subdirs
        subdirs = parse_dirs(html_text)
        files = parse_files(html_text)

        # Files directly at year level
        for doc_id, title in files:
            d = parse_title_date(title)
            if d and cutoff <= d <= future_limit:
                results.append({
                    "board": board_name,
                    "doc_id": doc_id,
                    "title": title,
                    "meeting_date": d,
                })

        # Recurse into meeting subdirs (named with MMDDYYYY prefix)
        for subdir_id, subdir_name in subdirs:
            d = parse_title_date(subdir_name)
            if d and cutoff <= d <= future_limit:
                time.sleep(DELAY_SECONDS * 0.5)
                results.extend(
                    collect_docs_from_dir(
                        subdir_id, main_dir_id, board_name,
                        cutoff, future_limit, depth=2,
                    )
                )
        return results

    else:
        # Meeting subdir — collect files only
        files = parse_files(html_text)
        for doc_id, title in files:
            # Use the date from the parent folder name if the file itself has no date
            d = parse_title_date(title)
            if d and cutoff <= d <= future_limit:
                results.append({
                    "board": board_name,
                    "doc_id": doc_id,
                    "title": title,
                    "meeting_date": d,
                })
        return results


# --- Main ---

def main():
    parser = argparse.ArgumentParser(
        description=(
            "Download Shelton CT municipal agendas and minutes "
            "for documents dated within the past N days."
        )
    )
    parser.add_argument(
        "--days", type=int, default=DAYS_BACK, metavar="N",
        help=f"Look back N days by document title date (default: {DAYS_BACK})",
    )
    parser.add_argument(
        "--ahead", type=int, default=7, metavar="N",
        help="Also include documents up to N days ahead (default: 7)",
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
        help="Only process boards whose name contains NAME (case-insensitive)",
    )
    parser.add_argument(
        "--no-video", action="store_true",
        help="Skip YouTube recording link collection",
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
    print(f"Hub page    : {HUB_URL}")
    if not args.dry_run:
        print(f"Output dir  : {args.output_dir}")
    print()

    # --- Step 1: fetch hub page and discover board directories ---
    print("Fetching hub page to discover boards...")
    hub_html = fetch_html(HUB_URL, accept="text/html")
    if not hub_html:
        print("ERROR: Could not fetch the hub page.", file=sys.stderr)
        sys.exit(1)

    main_dir_id = parse_main_dir(hub_html)
    if not main_dir_id:
        print("ERROR: Could not find the root document library ID.", file=sys.stderr)
        sys.exit(1)

    board_dirs = parse_board_dirs(hub_html)
    if not board_dirs:
        print("ERROR: No board directories found — page structure may have changed.",
              file=sys.stderr)
        sys.exit(1)

    print(f"Found {len(board_dirs)} board(s). Root dir: {main_dir_id}")

    if args.board:
        filter_str = args.board.lower()
        board_dirs = {k: v for k, v in board_dirs.items() if filter_str in k.lower()}
        print(f"Filtered to {len(board_dirs)} board(s) matching '{args.board}'.")

    print()

    # --- Step 2: collect matching documents across all boards ---
    all_docs = []
    seen_doc_ids = set()

    for board_name, dir_id in sorted(board_dirs.items()):
        print(f"  Scanning: {board_name}")
        docs = collect_docs_from_dir(
            dir_id, main_dir_id, board_name, cutoff, future_limit, depth=0
        )
        # Deduplicate by doc_id
        for doc in docs:
            if doc["doc_id"] not in seen_doc_ids:
                seen_doc_ids.add(doc["doc_id"])
                all_docs.append(doc)
        time.sleep(DELAY_SECONDS)

    all_docs.sort(key=lambda x: (x["meeting_date"], x["board"]), reverse=True)

    print()
    print(
        f"Found {len(all_docs)} document(s) across "
        f"{len({d['board'] for d in all_docs})} board(s)."
    )

    # --- Step 2b: collect YouTube recording links ---
    all_videos = []
    if not args.no_video and not args.board:
        print()
        for channel_id, label in YT_CHANNELS:
            print(f"  YouTube ({label}): fetching channel video list...")
            videos = fetch_channel_videos(channel_id, label)
            matched = 0
            for v in videos:
                d = parse_date_from_video_title(v["title"])
                if d and cutoff <= d <= future_limit:
                    v["meeting_date"] = d
                    all_videos.append(v)
                    matched += 1
            print(f"    {len(videos)} video(s) listed, {matched} in date window.")
        all_videos.sort(key=lambda x: x["meeting_date"], reverse=True)
        print(f"\nFound {len(all_videos)} recording link(s) from YouTube.")

    print()

    if not all_docs and not all_videos:
        print("Nothing found in date window.")
        return

    if args.dry_run:
        if all_docs:
            print(f"{'Board':<38} {'Date':<12} {'Title'}")
            print("-" * 90)
            for d in all_docs:
                print(
                    f"{d['board'][:37]:<38} "
                    f"{d['meeting_date']!s:<12} "
                    f"{d['title'][:40]}"
                )
            print(f"\n{len(all_docs)} document(s).")
        if all_videos:
            print()
            print(f"{'Channel':<28} {'Date':<12} {'Title'}")
            print("-" * 85)
            for v in all_videos:
                print(
                    f"{v['label']:<28} "
                    f"{v['meeting_date']!s:<12} "
                    f"{v['title'][:45]}"
                )
            print(f"\n{len(all_videos)} recording link(s).")
        print("\nRe-run without --dry-run to download.")
        return

    # --- Step 3: download PDFs ---
    os.makedirs(args.output_dir, exist_ok=True)
    log_path = os.path.join(args.output_dir, "download-log.txt")
    log_lines = []
    downloaded = skipped = failed = 0

    for d in all_docs:
        dest = make_dest_path(
            d["board"], d["title"], d["doc_id"], d["meeting_date"], args.output_dir
        )
        label = os.path.basename(dest)

        if os.path.exists(dest):
            print(f"  skip (exists)  {label}")
            skipped += 1
            continue

        print(f"  [{d['meeting_date']}] {d['board']} — {d['title'][:50]}")
        print(f"  downloading    {label}")

        if download_pdf(d["doc_id"], dest):
            downloaded += 1
            log_lines.append(
                f"{datetime.datetime.now().isoformat()}  OK       {dest}"
            )
        else:
            failed += 1
            log_lines.append(
                f"{datetime.datetime.now().isoformat()}  FAILED   docId={d['doc_id']}"
            )
            if os.path.exists(dest):
                os.remove(dest)

        time.sleep(DELAY_SECONDS)

    # --- Step 4: save YouTube recording .url shortcuts ---
    vid_saved = vid_skipped = 0
    for v in all_videos:
        dest = make_video_dest_path(
            v["id"], v["title"], v["meeting_date"], args.output_dir
        )
        label = os.path.basename(dest)

        if os.path.exists(dest):
            print(f"  skip (exists)  {label}")
            vid_skipped += 1
            continue

        print(f"  [{v['meeting_date']}] {v['label']} — {v['title'][:50]}")
        print(f"  saving         {label}")
        save_url_shortcut(v["url"], dest)
        vid_saved += 1
        log_lines.append(
            f"{datetime.datetime.now().isoformat()}  OK       {dest}"
        )

    if log_lines:
        with open(log_path, "a") as f:
            f.write("\n".join(log_lines) + "\n")

    print()
    print(f"Done — PDFs: downloaded={downloaded}  skipped={skipped}  failed={failed}")
    if all_videos:
        print(f"      URLs: saved={vid_saved}  skipped={vid_skipped}")
    if downloaded + skipped + vid_saved + vid_skipped:
        print(f"Files in: {args.output_dir}")
    if log_lines:
        print(f"Log:      {log_path}")


if __name__ == "__main__":
    main()


# --- Tips ---
#
# 1. Preview without downloading:
#    python3 scripts/download-shelton-agendas.py --dry-run
#
# 2. Narrow to one board:
#    python3 scripts/download-shelton-agendas.py --board "Aldermen"
#
# 3. Change the lookback window:
#    python3 scripts/download-shelton-agendas.py --days 7
#
# 4. Save files somewhere else:
#    python3 scripts/download-shelton-agendas.py --output-dir ~/Downloads/shelton
#
# 5. Skip YouTube recording links (PDFs only):
#    python3 scripts/download-shelton-agendas.py --no-video
#
# 6. Run on a schedule (cron — 7 AM daily):
#    0 7 * * * cd /path/to/repo && python3 scripts/download-shelton-agendas.py
#
# 7. Process downloaded files with Claude afterward:
#    python3 scripts/download-shelton-agendas.py && bash scripts/batch-process.sh beat-archive/shelton-agendas/
#
# NOTES:
#   - Shelton uses a QScend/Catalis CMS with a document library organized as:
#       Main root → Board folder → Year folder → [Meeting subfolder →] files
#   - Dates are parsed from the MMDDYYYY prefix in document titles.
#     Files without a date prefix (e.g. annual schedules) are skipped.
#   - The script disables SSL hostname verification due to a cert/hostname
#     mismatch on cityofshelton.org's load-balanced servers. TLS is still used;
#     traffic is encrypted.
#   - Board directories are discovered dynamically from the hub page on each run,
#     so new boards added to the site will be picked up automatically.
#   - RECORDINGS: Shelton broadcasts meetings on two YouTube channels:
#       City Hall:     https://www.youtube.com/channel/UCOm-u1DcLoOFmVnnCgxIm9w
#       Conservation:  https://www.youtube.com/channel/UCdNSokFtzuiCjBd7QeASEXw
#     Videos are saved as .url shortcut files (not downloaded as video files).
#     City Hall videos were uploaded as live streams and may require a YouTube
#     login to view. Requires yt-dlp: pip install yt-dlp
