#!/usr/bin/env python3
# download-east-haddam-agendas.py
# Download municipal meeting agendas, minutes, and video recordings from
# the East Haddam, CT Agenda Center for meetings within the past N days
# (and up to M days ahead).
#
# USAGE:
#   python3 scripts/download-east-haddam-agendas.py [options]
#
# REQUIREMENTS:
#   - Python 3.6+  (no third-party packages needed for docs)
#   - yt-dlp       (for video: pip install yt-dlp)
#   - Internet connection
#
# WHAT IT DOES:
#   Documents (default or --docs-only):
#     1. Queries the town's custom "PDF Browser" JSON API for the top-level
#        Agendas & Minutes Archive folder to discover every board
#     2. For each board, queries its folder for year sub-folders, then the
#        current (and, if the window crosses a year boundary, prior) year
#        folder(s) for files directly
#     3. Parses the meeting date out of each filename (e.g.
#        "BOS_01-21-2026_Agenda.pdf" -> Jan 21, 2026) and filters to the
#        lookback/lookahead window
#     4. Downloads matching PDFs to beat-archive/east-haddam-agendas/YYYY-MM/
#
#   Video (--include-video or --video-only; off by default):
#     5. Downloads recent videos from the Town of East Haddam YouTube
#        channel using yt-dlp — this channel posts real, current
#        per-meeting recordings (titled e.g. "Board of Selectmen -
#        8.5.2026"), confirmed directly before writing this script.
#
# SITE STRUCTURE — EvoGov (evogov.com), NOT CivicPlus:
#   easthaddam.org runs EvoGov, a different municipal CMS than every other
#   CT town in this repo so far except Ledyard's Legistar. The town's
#   "Agendas and Minutes" nav item links to a custom-built document
#   browser widget ("PBV" / PDF Browser Viewer) at
#   /pdf-browser/board-agendas-minutes/, which is populated client-side by
#   AJAX calls to its own small JSON API — no CivicPlus/QScend/Legistar
#   involved at all.
#
#   API:     https://www.easthaddam.org/pdf-browser/board-agendas-minutes/data/
#              ?folder_id=NNNNN   (omit for the root folder, which lists
#              every board as a top-level folder)
#            Each response has "folders" (sub-folders: boards contain
#            years, years contain files) and "files" (only populated at
#            the leaf/year level), with each file's "url" already a full,
#            directly downloadable link — no auth needed.
#   Files:   https://www.easthaddam.org/media/BoardAgendasMinutes/<Board>/
#              <Year>/<Prefix>_MM-DD-YYYY_Agenda|Minutes[-Suffix].pdf
#   YouTube: https://www.youtube.com/channel/UCLZq-CmatoKY6FW829c6mBA
#              ("Town of East Haddam")
#
#   The API's own "modified" timestamp is the upload date, not the meeting
#   date — this script parses the meeting date out of the filename's
#   MM-DD-YYYY segment instead, matching the convention used elsewhere in
#   this repo for CivicPlus sites that expose only a posted date.

import argparse
import datetime
import json
import os
import re
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

# --- Configuration ---
BASE_URL = "https://www.easthaddam.org"
API_BASE = f"{BASE_URL}/pdf-browser/board-agendas-minutes/data/"
YOUTUBE_CHANNEL = "https://www.youtube.com/channel/UCLZq-CmatoKY6FW829c6mBA"
OUTPUT_DIR = "beat-archive/east-haddam-agendas"
DAYS_BACK = 4
DAYS_AHEAD = 7
DELAY_SECONDS = 0.5

YT_DLP_NODE = "node:/home/richkirby/.local/bin/yt-dlp-node"  # yt-dlp needs Node 22+; symlink kept current by scripts/update-yt-dlp-node.sh

UA = "East-Haddam-CT-Agendas-Downloader/1.0 (journalism research)"

_FILENAME_DATE_RE = re.compile(r"(\d{2})-(\d{2})-(\d{4})")


# --- HTTP helpers ---

def fetch_json(url):
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return json.loads(r.read().decode("utf-8", errors="replace"))
    except Exception as e:
        print(f"  WARNING: fetch failed for {url}: {e}", file=sys.stderr)
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


# --- (1) Documents ---

def fetch_folder(folder_id=None):
    url = API_BASE
    if folder_id is not None:
        url += "?" + urllib.parse.urlencode({"folder_id": folder_id})
    return fetch_json(url)


def discover_boards():
    """Return list of (board_id, board_name) from the archive root folder."""
    data = fetch_folder()
    if not data:
        return []
    return [(f["id"], f["name"]) for f in data.get("folders", [])]


def parse_filename_date(name):
    m = _FILENAME_DATE_RE.search(name)
    if not m:
        return None
    mm, dd, yyyy = int(m.group(1)), int(m.group(2)), int(m.group(3))
    try:
        return datetime.date(yyyy, mm, dd)
    except ValueError:
        return None


def collect_docs(cutoff, future_limit, board_filter, no_minutes, no_agendas):
    boards = discover_boards()
    if board_filter:
        boards = [(bid, name) for bid, name in boards if board_filter in name.lower()]

    years_needed = {y for y in range(cutoff.year, future_limit.year + 1)}

    docs = []
    for board_id, board_name in boards:
        board_data = fetch_folder(board_id)
        if not board_data:
            continue
        year_folders = {f["name"]: f["id"] for f in board_data.get("folders", [])}

        for year_name, year_id in year_folders.items():
            if not year_name.isdigit() or int(year_name) not in years_needed:
                continue
            year_data = fetch_folder(year_id)
            if not year_data:
                continue
            for f in year_data.get("files", []):
                if not f.get("is_pdf"):
                    continue
                name = f["name"]
                meeting_date = parse_filename_date(name)
                if not meeting_date or not (cutoff <= meeting_date <= future_limit):
                    continue
                lower = name.lower()
                if "minutes" in lower:
                    doc_type = "minutes"
                    if no_minutes:
                        continue
                elif "agenda" in lower:
                    doc_type = "agenda"
                    if no_agendas:
                        continue
                else:
                    continue
                docs.append({
                    "board": board_name, "meeting_date": meeting_date,
                    "doc_type": doc_type, "href": f["url"], "filename": name,
                })
            time.sleep(DELAY_SECONDS)
    return docs


# --- (2) Video (YouTube channel via yt-dlp) ---

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


def make_path(board, doc_type, meeting_date, output_dir, ext=".pdf", counter=0):
    date_str = meeting_date.strftime("%Y-%m-%d")
    month_dir = os.path.join(output_dir, meeting_date.strftime("%Y-%m"))
    os.makedirs(month_dir, exist_ok=True)
    suffix = f"-{counter}" if counter else ""
    return os.path.join(month_dir, f"{date_str}-{slugify(board)}-{slugify(doc_type)}{suffix}{ext}")


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
            "Download East Haddam CT municipal agendas, minutes, and video "
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
    parser.add_argument("--no-minutes", action="store_true", help="Skip minutes, download agendas only")
    parser.add_argument("--no-agendas", action="store_true", help="Skip agendas, download minutes only")
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
    if not args.dry_run:
        print(f"Output dir  : {args.output_dir}")
    print()

    docs = []
    if do_docs:
        print("Discovering boards...")
        docs = collect_docs(cutoff, future_limit, board_filter, args.no_minutes, args.no_agendas)
        print(f"Documents   : {len(docs)} found\n")

    video_listing = []
    if do_video and args.dry_run:
        print(f"Fetching YouTube channel listing (uploaded since {cutoff})...")
        video_listing = download_channel_videos(YOUTUBE_CHANNEL, args.output_dir, cutoff, dry_run=True)
        print(f"Video       : {len(video_listing)} found\n")

    docs.sort(key=lambda x: (x["meeting_date"], x["board"]), reverse=True)
    assign_counters(docs, lambda d: (d["board"], d["meeting_date"], d["doc_type"]))

    if not docs and not video_listing and not do_video:
        print("No items found in the date window.")
        return

    if args.dry_run:
        if docs:
            print(f"{'Board':<40} {'Date':<12} Type")
            print("-" * 65)
            for d in docs:
                print(f"{d['board'][:39]:<40} {d['meeting_date']!s:<12} {d['doc_type']}")
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
        dest = make_path(d["board"], d["doc_type"], d["meeting_date"], args.output_dir, counter=d["counter"])
        label = os.path.basename(dest)
        if os.path.exists(dest):
            print(f"  skip (exists)  {label}")
            skipped += 1
            continue
        print(f"  [{d['meeting_date']}] {d['board']} — {d['doc_type']}")
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
#    python3 scripts/download-east-haddam-agendas.py --dry-run
#
# 2. Just Board of Selectmen:
#    python3 scripts/download-east-haddam-agendas.py --board "Board of Selectmen"
#
# 3. PDFs only (no video — the default; --include-video/--video-only turn it on):
#    python3 scripts/download-east-haddam-agendas.py
#
# 4. Video only:
#    python3 scripts/download-east-haddam-agendas.py --video-only
#
# 5. Change the lookback window:
#    python3 scripts/download-east-haddam-agendas.py --days 14
#
# 6. Run on a schedule (cron — evening):
#    0 21 * * 1-5 cd /path/to/repo && python3 scripts/download-east-haddam-agendas.py --include-video
