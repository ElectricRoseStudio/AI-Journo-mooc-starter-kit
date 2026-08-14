#!/usr/bin/env python3
# download-killingworth-agendas.py
# Download municipal meeting minutes/agendas and video recordings from
# the Killingworth, CT Agenda Center for meetings within the past N days
# (and up to M days ahead).
#
# USAGE:
#   python3 scripts/download-killingworth-agendas.py [options]
#
# REQUIREMENTS:
#   - Python 3.6+  (no third-party packages needed for docs)
#   - yt-dlp       (for video: pip install yt-dlp)
#   - Internet connection
#
# WHAT IT DOES:
#   Documents (default or --docs-only):
#     1. Fetches https://townofkillingworth.com/meeting-minutes/ to
#        discover every board's own page
#     2. Fetches each board's page and pulls every PDF link out of its
#        "medialist" widget (a WordPress plugin that lists every file in
#        that board's upload folder — full history, no pagination)
#     3. Parses the meeting date out of each filename (e.g.
#        "BOS_07_27_2026.pdf" -> Jul 27, 2026; some older files use a
#        2-digit year, e.g. "BOS_02_26_24.pdf") and filters to the
#        lookback/lookahead window
#     4. Downloads matching PDFs to beat-archive/killingworth-agendas/YYYY-MM/
#
#   Video (--include-video or --video-only; off by default):
#     5. Downloads recent videos from the Town of Killingworth YouTube
#        channel using yt-dlp — this channel posts real, current
#        per-meeting recordings (titled e.g. "Board of Selectmen
#        7-27-2026"), confirmed directly before writing this script.
#
# SITE STRUCTURE — WordPress, NOT CivicPlus/EvoGov/Legistar:
#   townofkillingworth.com is a plain WordPress site (the town's own
#   domain is killingworthct.org, which doesn't resolve at all — the
#   real site lives at townofkillingworth.com). Each board's minutes page
#   embeds a "medialist" plugin widget that lists every PDF uploaded to
#   that board's WordPress media folder, server-rendered — no AJAX or API
#   needed, just plain HTML per board page.
#
#   Hub:         https://townofkillingworth.com/meeting-minutes/
#   Board pages: https://townofkillingworth.com/<board-slug>-minutes/
#   Files:       https://townofkillingworth.com/wp-content/uploads/<Y>/<M>/
#                  <Prefix>_MM_DD_YYYY.pdf (directly downloadable, no auth)
#   YouTube:     https://www.youtube.com/channel/UCWJcx662JLu-zX0o_veIGRA
#                  ("Town of Killingworth, Connecticut")
#
# NOTE ON AGENDA vs MINUTES: unlike every CivicPlus-based town in this
#   repo, Killingworth's board pages don't distinguish agendas from
#   minutes at all -- filenames are just "<Prefix>_MM_DD_YYYY.pdf", one
#   per meeting date, on a page titled "Meeting Minutes & Schedules" with
#   no separate "Agendas" page or section found anywhere on the site.
#   Whatever the town actually uploads for a given meeting (agenda,
#   minutes, or both combined) is downloaded as a single "document" per
#   date -- this script does not claim a distinction the source data
#   doesn't make.

import argparse
import datetime
import html
import os
import re
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

# --- Configuration ---
BASE_URL = "https://townofkillingworth.com"
HUB_URL = f"{BASE_URL}/meeting-minutes/"
YOUTUBE_CHANNEL = "https://www.youtube.com/channel/UCWJcx662JLu-zX0o_veIGRA"
OUTPUT_DIR = "beat-archive/killingworth-agendas"
DAYS_BACK = 4
DAYS_AHEAD = 7
DELAY_SECONDS = 0.5

YT_DLP_NODE = "node:/home/richkirby/.local/bin/yt-dlp-node"  # yt-dlp needs Node 22+; symlink kept current by scripts/update-yt-dlp-node.sh

UA = "Killingworth-CT-Agendas-Downloader/1.0 (journalism research)"

_BOARD_LINK_RE = re.compile(
    r'<a[^>]+href="(https://townofkillingworth\.com/[a-z0-9-]+-minutes/)"[^>]*>((?:(?!</a>).)*)</a>',
    re.S,
)
_PDF_LINK_RE = re.compile(
    r'href="(https://townofkillingworth\.com/wp-content/uploads/[^"]+\.pdf)"',
)
_FILENAME_DATE_RE = re.compile(r"_(\d{2})_(\d{2})_(\d{2,4})\.pdf$", re.IGNORECASE)


# --- HTTP helpers ---

def fetch_html(url):
    req = urllib.request.Request(
        url, headers={"User-Agent": UA, "Accept": "text/html,application/xhtml+xml,*/*"},
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            charset = r.headers.get_content_charset() or "utf-8"
            return r.read().decode(charset, errors="replace")
    except Exception as e:
        print(f"  ERROR fetching {url}: {e}", file=sys.stderr)
        return None


def download_pdf(url, dest_path):
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "application/pdf,*/*"})
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            with open(dest_path, "wb") as f:
                f.write(r.read())
        return True
    except Exception as e:
        print(f"  WARNING: {e}", file=sys.stderr)
        return False


# --- (1) Board discovery ---

def discover_boards():
    """Return list of (board_name, board_url) from the meeting-minutes hub."""
    hub_html = fetch_html(HUB_URL)
    if not hub_html:
        return []
    boards = {}
    for m in _BOARD_LINK_RE.finditer(hub_html):
        url = m.group(1)
        text = html.unescape(re.sub(r"<[^>]+>", "", m.group(2)).strip())
        if text and url not in boards:
            boards[url] = text
    return sorted(boards.items(), key=lambda kv: kv[1])


# --- (2) Per-board document parsing ---

def parse_filename_date(url):
    m = _FILENAME_DATE_RE.search(url)
    if not m:
        return None
    mm, dd, yy = int(m.group(1)), int(m.group(2)), m.group(3)
    yyyy = int(yy) if len(yy) == 4 else 2000 + int(yy)
    try:
        return datetime.date(yyyy, mm, dd)
    except ValueError:
        return None


def collect_board_docs(board_name, board_url, cutoff, future_limit):
    page_html = fetch_html(board_url)
    if not page_html:
        return []
    docs = []
    seen = set()
    for m in _PDF_LINK_RE.finditer(page_html):
        url = m.group(1)
        if url in seen:
            continue
        seen.add(url)
        meeting_date = parse_filename_date(url)
        if not meeting_date or not (cutoff <= meeting_date <= future_limit):
            continue
        docs.append({"board": board_name, "meeting_date": meeting_date, "href": url})
    return docs


# --- (3) Video (YouTube channel via yt-dlp) ---

def download_channel_videos(channel_url, output_dir, date_after, dry_run=False):
    date_str = date_after.strftime("%Y%m%d")
    video_dir = os.path.join(output_dir, "videos")

    if dry_run:
        cmd = [
            "yt-dlp", "--js-runtimes", YT_DLP_NODE, "--flat-playlist", "--dateafter", date_str,
            "--print", "%(upload_date)s %(title)s",
            "--no-warnings", "--quiet",
            channel_url,
        ]
        try:
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
            return [l for l in result.stdout.splitlines() if l.strip()]
        except FileNotFoundError:
            print("  WARNING: yt-dlp not found — skipping video listing", file=sys.stderr)
            return []
        except subprocess.TimeoutExpired:
            print("  WARNING: yt-dlp timed out listing channel videos", file=sys.stderr)
            return []

    os.makedirs(video_dir, exist_ok=True)
    cmd = [
        "yt-dlp", "--js-runtimes", YT_DLP_NODE,
        "--dateafter", date_str,
        # A channel can list a not-yet-started scheduled livestream ("This
        # live event will begin in N days"), which yt-dlp can't extract.
        # Without --ignore-errors, hitting one of these aborts the whole
        # run before it reaches any real, downloadable video after it.
        "--ignore-errors",
        # No --break-match-filters here (unlike some other channel-based
        # downloaders in this repo): a scheduled-livestream placeholder has
        # no upload_date, so a date-based break filter would treat the
        # first one hit as "too old" and stop the walk right there —
        # before ever reaching real videos beneath it. --playlist-end
        # below already bounds the walk cheaply enough on its own.
        "--playlist-end", "20",
        "--sleep-requests", "0.75",
        "--sleep-interval", "10",
        "--max-sleep-interval", "20",
        "-f", "bestvideo+bestaudio/best",
        "--merge-output-format", "mp4",
        "-o", os.path.join(video_dir, "%(upload_date)s-%(title)s.%(ext)s"),
        "--no-overwrites",
        "--quiet",
        "--no-warnings",
        "--write-info-json",
        channel_url,
    ]
    downloaded = failed = 0
    try:
        result = subprocess.run(cmd, timeout=1800)
        if result.returncode == 0:
            downloaded = 1  # yt-dlp handles per-file counting/skipping internally
        else:
            failed = 1
    except FileNotFoundError:
        print("  ERROR: yt-dlp not found. Install with: pip install yt-dlp", file=sys.stderr)
        failed = 1
    except subprocess.TimeoutExpired:
        print("  WARNING: yt-dlp timed out downloading channel videos", file=sys.stderr)
        failed = 1
    return downloaded, 0, failed


# --- File naming ---

def slugify(text, max_len=55):
    text = str(text).lower().strip()
    text = re.sub(r"[/\\&]", "-", text)
    text = re.sub(r"[^\w\s-]", "", text)
    text = re.sub(r"[\s_]+", "-", text)
    text = re.sub(r"-{2,}", "-", text)
    return text.strip("-")[:max_len]


def make_path(board, meeting_date, output_dir, ext=".pdf", counter=0):
    date_str = meeting_date.strftime("%Y-%m-%d")
    month_dir = os.path.join(output_dir, meeting_date.strftime("%Y-%m"))
    os.makedirs(month_dir, exist_ok=True)
    suffix = f"-{counter}" if counter else ""
    return os.path.join(month_dir, f"{date_str}-{slugify(board)}{suffix}{ext}")


def assign_counters(items, key_fn):
    seen = {}
    for item in items:
        key = key_fn(item)
        item["counter"] = seen.get(key, 0)
        seen[key] = item["counter"] + 1


# --- Main ---

def main():
    parser = argparse.ArgumentParser(
        description=(
            "Download Killingworth CT municipal meeting documents and video "
            "recordings for meetings within the past N days (and up to M days ahead)."
        )
    )
    parser.add_argument("--days", type=int, default=DAYS_BACK, metavar="N",
                        help=f"Look back N days (default: {DAYS_BACK})")
    parser.add_argument("--ahead", type=int, default=DAYS_AHEAD, metavar="N",
                        help=f"Also include meetings up to N days ahead (default: {DAYS_AHEAD})")
    parser.add_argument("--output-dir", default=OUTPUT_DIR, metavar="DIR",
                        help=f"Destination directory (default: {OUTPUT_DIR})")
    parser.add_argument("--dry-run", action="store_true", help="List matching items without downloading")
    parser.add_argument("--board", metavar="NAME",
                        help="Only process boards whose name contains NAME (case-insensitive)")
    parser.add_argument("--include-video", action="store_true", help="Also download the town's YouTube channel video")
    parser.add_argument("--video-only", action="store_true", help="Download only video recordings")
    args = parser.parse_args()

    now = datetime.datetime.now()
    if (now.weekday() == 5 and now.hour >= 18) or (now.weekday() == 6 and now.hour < 12):
        print("Skipping — no downloads on Saturday nights or Sunday mornings.")
        sys.exit(0)

    do_docs = not args.video_only
    do_video = args.include_video or args.video_only

    today = datetime.date.today()
    cutoff = today - datetime.timedelta(days=args.days)
    future_limit = today + datetime.timedelta(days=args.ahead)
    board_filter = args.board.lower() if args.board else None

    print(f"Date window : {cutoff} to {future_limit}")
    print(f"Hub URL     : {HUB_URL}")
    if not args.dry_run:
        print(f"Output dir  : {args.output_dir}")
    print()

    docs = []
    if do_docs:
        print("Discovering boards...")
        boards = discover_boards()
        if not boards:
            print("ERROR: Could not discover any boards.", file=sys.stderr)
            sys.exit(1)
        if board_filter:
            boards = [(u, n) for u, n in boards if board_filter in n.lower()]
        print(f"  {len(boards)} board(s).\n")

        for board_url, board_name in boards:
            board_docs = collect_board_docs(board_name, board_url, cutoff, future_limit)
            if board_docs:
                print(f"  {board_name}: {len(board_docs)} doc(s)")
                docs.extend(board_docs)
            time.sleep(DELAY_SECONDS)

        print(f"\nDocuments   : {len(docs)} found\n")

    video_listing = []
    if do_video and args.dry_run:
        print(f"Fetching YouTube channel listing (uploaded since {cutoff})...")
        video_listing = download_channel_videos(YOUTUBE_CHANNEL, args.output_dir, cutoff, dry_run=True)
        print(f"Video       : {len(video_listing)} found\n")

    docs.sort(key=lambda x: (x["meeting_date"], x["board"]), reverse=True)
    assign_counters(docs, lambda d: (d["board"], d["meeting_date"]))

    if not docs and not video_listing and not do_video:
        print("No items found in the date window.")
        return

    if args.dry_run:
        if docs:
            print(f"{'Board':<40} {'Date':<12}")
            print("-" * 55)
            for d in docs:
                print(f"{d['board'][:39]:<40} {d['meeting_date']!s:<12}")
            print(f"\n{len(docs)} document(s).")
        if video_listing:
            print(f"\nYouTube channel videos (uploaded since {cutoff}):")
            for line in video_listing:
                print(f"  {line}")
        print("\nRe-run without --dry-run to download.")
        return

    os.makedirs(args.output_dir, exist_ok=True)
    log_path = os.path.join(args.output_dir, "download-log.txt")
    log_lines = []
    downloaded = skipped = failed = 0

    for d in docs:
        dest = make_path(d["board"], d["meeting_date"], args.output_dir, counter=d["counter"])
        label = os.path.basename(dest)
        if os.path.exists(dest):
            print(f"  skip (exists)  {label}")
            skipped += 1
            continue
        print(f"  [{d['meeting_date']}] {d['board']}")
        print(f"  downloading    {label}")
        if download_pdf(d["href"], dest):
            downloaded += 1
            log_lines.append(f"{datetime.datetime.now().isoformat()}  OK       {dest}")
        else:
            failed += 1
            log_lines.append(f"{datetime.datetime.now().isoformat()}  FAILED   {d['href']}")
            if os.path.exists(dest):
                os.remove(dest)
        time.sleep(DELAY_SECONDS)

    if log_lines:
        with open(log_path, "a") as f:
            f.write("\n".join(log_lines) + "\n")

    print()
    print(f"Documents — downloaded: {downloaded}  skipped: {skipped}  failed: {failed}")

    if do_video:
        print()
        print(f"Downloading YouTube channel videos (since {cutoff})...")
        print(f"  Channel: {YOUTUBE_CHANNEL}")
        print(f"  Output:  {os.path.join(args.output_dir, 'videos')}/")
        v_dl, v_skip, v_fail = download_channel_videos(YOUTUBE_CHANNEL, args.output_dir, cutoff)
        if v_fail:
            print("  WARNING: one or more video downloads failed", file=sys.stderr)

    print()
    if downloaded + skipped:
        print(f"Files in: {args.output_dir}")
    if log_lines:
        print(f"Log:      {log_path}")


if __name__ == "__main__":
    main()


# --- Tips ---
#
# 1. Preview without downloading:
#    python3 scripts/download-killingworth-agendas.py --dry-run
#
# 2. Just Board of Selectmen:
#    python3 scripts/download-killingworth-agendas.py --board "Board of Selectmen"
#
# 3. PDFs only (no video — the default; --include-video/--video-only turn it on):
#    python3 scripts/download-killingworth-agendas.py
#
# 4. Video only:
#    python3 scripts/download-killingworth-agendas.py --video-only
#
# 5. Change the lookback window:
#    python3 scripts/download-killingworth-agendas.py --days 14
#
# 6. Run on a schedule (cron — evening):
#    0 21 * * 1-5 cd /path/to/repo && python3 scripts/download-killingworth-agendas.py --include-video
