#!/usr/bin/env python3
# download-east-hampton-agendas.py
# Download municipal meeting agendas, minutes, and video recordings from
# the East Hampton, CT Agenda Center for meetings within the past N days
# (and up to M days ahead).
#
# USAGE:
#   python3 scripts/download-east-hampton-agendas.py [options]
#
# REQUIREMENTS:
#   - Python 3.6+  (no third-party packages needed for docs)
#   - yt-dlp       (for video: pip install yt-dlp)
#   - Internet connection
#
# WHAT IT DOES:
#   Documents (default or --docs-only):
#     1. Fetches the East Hampton CT Agenda Center search endpoint with a
#        date range spanning DAYS_BACK days ago through DAYS_AHEAD days
#        ahead
#     2. Parses each board section and meeting row for board name,
#        meeting date, agenda URL, and minutes URL
#     3. Downloads PDFs to beat-archive/east-hampton-agendas/YYYY-MM/
#
#   Video (--include-video or --video-only; off by default):
#     4. Downloads recent videos from the Town of East Hampton YouTube
#        channel using yt-dlp — this channel posts real, current
#        per-meeting recordings (titled e.g. "ZBA Meeting 13Apr2026",
#        "WPCA Meeting 03Mar2026"), confirmed directly before writing
#        this script.
#
# SITE STRUCTURE (CivicPlus CivicEngage "Agenda Center", same platform as
# Clinton/Haddam/Salem/Swampscott/Woodbridge/Middlefield elsewhere in this
# repo):
#   Hub:     https://www.easthamptonct.gov/AgendaCenter
#   Search:  https://www.easthamptonct.gov/AgendaCenter/Search/?term=&CIDs=all
#              &startDate=MM/DD/YYYY&endDate=MM/DD/YYYY&dateRange=Custom&dateSelector=0
#   Agenda:  https://www.easthamptonct.gov/AgendaCenter/ViewFile/Agenda/_MMDDYYYY-ID
#   Minutes: https://www.easthamptonct.gov/AgendaCenter/ViewFile/Minutes/_MMDDYYYY-ID
#   YouTube: https://www.youtube.com/channel/UC-Zo6Mlg_TaW6IutCDpoLgg
#
# NOTE: The town also links a "vocalvideo.com/c/town-of-east-hampton" page
#   from its homepage, but that's a public-comment video-submission tool,
#   not meeting recordings — not used here.

import argparse
import datetime
import html.parser
import os
import re
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

# --- Configuration ---
BASE_URL = "https://www.easthamptonct.gov"
SEARCH_URL = f"{BASE_URL}/AgendaCenter/Search/"
YOUTUBE_CHANNEL = "https://www.youtube.com/channel/UC-Zo6Mlg_TaW6IutCDpoLgg"
OUTPUT_DIR = "beat-archive/east-hampton-agendas"
DAYS_BACK = 4
DAYS_AHEAD = 7
DELAY_SECONDS = 0.8

YT_DLP_NODE = "node:/home/richkirby/.local/bin/yt-dlp-node"  # yt-dlp needs Node 22+; symlink kept current by scripts/update-yt-dlp-node.sh

UA = "East-Hampton-CT-Agendas-Downloader/1.0 (journalism research)"

_H2_RE = re.compile(r'aria-controls="category-panel-\d+">([^<]+)</h2>')
_H3_DATE_RE = re.compile(
    r"\b(Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)\s+(\d{1,2}),\s+(\d{4})\b"
)
_MONTH_ABBR = {
    "Jan": 1, "Feb": 2, "Mar": 3, "Apr": 4, "May": 5, "Jun": 6,
    "Jul": 7, "Aug": 8, "Sep": 9, "Oct": 10, "Nov": 11, "Dec": 12,
}


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


def download_pdf(path, dest_path):
    url = BASE_URL + path if path.startswith("/") else path
    req = urllib.request.Request(
        url, headers={"User-Agent": UA, "Accept": "application/pdf,application/msword,*/*"},
    )
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            with open(dest_path, "wb") as f:
                f.write(r.read())
        return True
    except Exception as e:
        print(f"  WARNING: {e}", file=sys.stderr)
        return False


# --- (1) Documents ---

class AgendaParser(html.parser.HTMLParser):
    def __init__(self):
        super().__init__()
        self.items = []
        self._board = "Unknown Board"
        self._current_date = None
        self._agenda_url = None
        self._minutes_url = None
        self._in_h2 = False
        self._in_h3 = False
        self._buf = ""

    def handle_starttag(self, tag, attrs):
        attrs_d = dict(attrs)
        if tag == "h2":
            self._flush()
            self._in_h2 = True
            self._buf = ""
            self._current_date = None
        elif tag == "h3":
            self._flush()
            self._in_h3 = True
            self._buf = ""
        elif tag == "a":
            href = attrs_d.get("href", "")
            if not href:
                return
            href = href.split("?", 1)[0]
            lower = href.lower()
            if "/agendacenter/viewfile/agenda/" in lower:
                if self._agenda_url is None:
                    self._agenda_url = href
            elif "/agendacenter/viewfile/minutes/" in lower:
                if self._minutes_url is None:
                    self._minutes_url = href

    def handle_data(self, data):
        if self._in_h2 or self._in_h3:
            self._buf += data

    def handle_endtag(self, tag):
        if tag == "h2" and self._in_h2:
            self._in_h2 = False
            name = self._buf.strip()
            if name:
                self._board = name
            self._buf = ""
        elif tag == "h3" and self._in_h3:
            self._in_h3 = False
            m = _H3_DATE_RE.search(self._buf)
            if m:
                mon, day, yr = m.group(1), int(m.group(2)), int(m.group(3))
                try:
                    self._current_date = datetime.date(yr, _MONTH_ABBR[mon], day)
                except ValueError:
                    self._current_date = None
            self._buf = ""

    def _flush(self):
        if self._current_date and (self._agenda_url or self._minutes_url):
            self.items.append({
                "board": self._board, "meeting_date": self._current_date,
                "agenda_url": self._agenda_url, "minutes_url": self._minutes_url,
            })
        self._agenda_url = None
        self._minutes_url = None

    def get_items(self):
        self._flush()
        return self.items


def build_search_url(start_date, end_date):
    params = urllib.parse.urlencode({
        "term": "", "CIDs": "all",
        "startDate": start_date.strftime("%m/%d/%Y"),
        "endDate": end_date.strftime("%m/%d/%Y"),
        "dateRange": "Custom", "dateSelector": "0",
    })
    return f"{SEARCH_URL}?{params}"


def collect_docs(cutoff, future_limit, board_filter, no_minutes, no_agendas):
    docs = []
    html_text = fetch_html(build_search_url(cutoff, future_limit))
    if not html_text:
        return docs
    parser = AgendaParser()
    parser.feed(html_text)
    items = parser.get_items()
    if board_filter:
        items = [i for i in items if board_filter in i["board"].lower()]
    for item in items:
        if item["agenda_url"] and not no_agendas:
            docs.append({"board": item["board"], "meeting_date": item["meeting_date"],
                        "doc_type": "agenda", "href": item["agenda_url"]})
        if item["minutes_url"] and not no_minutes:
            docs.append({"board": item["board"], "meeting_date": item["meeting_date"],
                        "doc_type": "minutes", "href": item["minutes_url"]})
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
            "Download East Hampton CT municipal agendas, minutes, and video "
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
            src = BASE_URL + d["href"] if d["href"].startswith("/") else d["href"]
            log_lines.append(f"{datetime.datetime.now().isoformat()}  FAILED   {src}")
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
#    python3 scripts/download-east-hampton-agendas.py --dry-run
#
# 2. Just Town Council:
#    python3 scripts/download-east-hampton-agendas.py --board "Town Council"
#
# 3. PDFs only (no video — the default; --include-video/--video-only turn it on):
#    python3 scripts/download-east-hampton-agendas.py
#
# 4. Video only:
#    python3 scripts/download-east-hampton-agendas.py --video-only
#
# 5. Change the lookback window:
#    python3 scripts/download-east-hampton-agendas.py --days 14
#
# 6. Run on a schedule (cron — evening):
#    0 19 * * 1-5 cd /path/to/repo && python3 scripts/download-east-hampton-agendas.py --include-video
