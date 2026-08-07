#!/usr/bin/env python3
# download-westborough-agendas.py
# Download municipal meeting agendas, minutes, and video recordings from
# the Westborough MA Agenda Center for meetings within the past N days
# (and up to 7 days ahead).
#
# USAGE:
#   python3 scripts/download-westborough-agendas.py [options]
#
# REQUIREMENTS:
#   - Python 3.6+  (no third-party packages needed for docs)
#   - yt-dlp       (for video: pip install yt-dlp)
#   - Internet connection
#
# WHAT IT DOES:
#   Documents (default or --docs-only):
#     1. Fetches the Westborough MA Agenda Center search endpoint with a
#        date range spanning DAYS_BACK days ago through DAYS_AHEAD days
#        ahead
#     2. Parses each board section and meeting row for board name, meeting
#        date, agenda URL, and minutes URL
#     3. Downloads PDFs to beat-archive/westborough-agendas/YYYY-MM/
#
#   Video (--include-video or --video-only):
#     4. Crawls each of WestboroughTV's per-board YouTube playlists (see
#        VIDEO NOTE) for uploads within the date window, parsed by title
#     5. Appends a download log to beat-archive/westborough-agendas/download-log.txt
#
# SITE STRUCTURE (CivicPlus CivicEngage "Agenda Center", same platform as
# Waterford CT, Malden MA, Milford MA, and Shrewsbury MA):
#   Hub:     https://www.westboroughma.gov/AgendaCenter
#   Search:  https://www.westboroughma.gov/AgendaCenter/Search/?term=&CIDs=all
#              &startDate=MM/DD/YYYY&endDate=MM/DD/YYYY&dateRange=Custom&dateSelector=0
#   Agenda:  https://www.westboroughma.gov/AgendaCenter/ViewFile/Agenda/_MMDDYYYY-ID
#   Minutes: https://www.westboroughma.gov/AgendaCenter/ViewFile/Minutes/_MMDDYYYY-ID
#
#   Checked directly: the AgendaCenter itself carries NO video links at
#   all (zero <td class="media"> cells found across a 6-month window) —
#   video is entirely a separate source, see VIDEO NOTE. School Committee
#   (Board of Education) and Recreation Commission (Parks & Recreation)
#   are both native AgendaCenter categories — no separate document scrape
#   needed for either.
#
# VIDEO NOTE — WestboroughTV (the town's PEG access nonprofit) has TWO
# possible video sources; a Cablecast VOD platform
# (live.westboroughtv.org/cablecastapi/v1/shows, a real public JSON API)
# was investigated first but rejected: its date/text/category filter
# query params are all silently ignored (confirmed directly — identical
# results regardless of eventDateFrom/To, searchText, or category params),
# it mixes in large volumes of unrelated content (church services, sports,
# community shows) under the same "Gov Channel" category tag, and — most
# importantly — a full-catalog scan found essentially no School Committee
# or Recreation Commission content on it at all (one 2020 entry each,
# nothing current).
#
# The actual, actively-maintained source is WestboroughTV's YouTube
# channel (youtube.com/@WestboroughTV), organized into genuine per-board
# playlists — confirmed by listing the channel's FULL playlist set
# (initial spot-checks with a low --playlist-end cap missed the
# committee/board playlists entirely; they're not at the front of the
# list). Playlist ordering is NOT reliably reverse-chronological within a
# playlist (School Committee's playlist has 2021 entries appearing before
# 2025/2026 ones) — so unlike Arlington's ACMi or Milford's MyMilfordTV,
# this script does a full flat-list scan of each playlist (capped, not
# early-stopped by date) and filters every title client-side, rather than
# relying on yt-dlp's --dateafter/--break-match-filters early-stop
# optimization, which assumes newest-first ordering that doesn't hold
# here. The playlist's own dict key is used as the "board" label directly
# (not parsed from the title) since title formats vary considerably
# ("Meeting - Date", "Meeting: Date" with no hyphen and double spaces,
# "LIVE STREAM Date", "Remote Meeting - Date") — only the date is parsed
# from each title, via a search (not anchored) for a month-name date
# pattern, which is robust across all the formats observed.
#
# Recreation Commission's playlist exists but its most recent entry (as
# of 2026-08) is from October 2025 — likely a dormant board rather than a
# missing source (same situation as several other towns' minor boards in
# this repo); it'll pick up automatically if postings resume.

import argparse
import datetime
import html.parser
import json
import os
import re
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

YT_DLP_NODE = "node:/home/richkirby/.local/bin/yt-dlp-node"  # yt-dlp needs Node 22+; symlink kept current by scripts/update-yt-dlp-node.sh

# --- Configuration ---
BASE_URL = "https://www.westboroughma.gov"
SEARCH_URL = f"{BASE_URL}/AgendaCenter/Search/"
OUTPUT_DIR = "beat-archive/westborough-agendas"
DAYS_BACK = 4
DAYS_AHEAD = 7
DELAY_SECONDS = 0.8
PLAYLIST_SCAN_CAP = 200  # generous — playlist order isn't reliably chronological

UA = "Westborough-MA-Agendas-Downloader/1.0 (journalism research)"

_H3_DATE_RE = re.compile(
    r"\b(Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)\s+(\d{1,2}),\s+(\d{4})\b"
)
_MONTH_ABBR = {
    "Jan": 1, "Feb": 2, "Mar": 3, "Apr": 4, "May": 5, "Jun": 6,
    "Jul": 7, "Aug": 8, "Sep": 9, "Oct": 10, "Nov": 11, "Dec": 12,
}

# Board name -> WestboroughTV YouTube playlist. Confirmed by listing the
# channel's full playlist set directly (youtube.com/@WestboroughTV).
COMMITTEE_PLAYLISTS = {
    "Select Board": "PLmSFJQ2Y_ncwEoejj__ZKJDRP7d1_4Zmg",
    "School Committee": "PLmSFJQ2Y_ncxLfKbsNHhRl6OnLUewaRNs",
    "Recreation Commission": "PLmSFJQ2Y_ncyaKy-CjQRMkeuDV-NbpWUx",
    "Advisory Finance Committee": "PLmSFJQ2Y_nczFuvFqGnlWJUmyeXY2uDVO",
    "Planning Board": "PLmSFJQ2Y_ncw9BhAm7JddHvWCQ2N7YM6-",
    "Zoning Board of Appeals": "PLmSFJQ2Y_ncwqnpWSNeZFE53ZZl5UjV30",
    "Conservation Commission": "PLmSFJQ2Y_ncw0I59-TlE5vHBYgjGE3dXg",
    "Board of Health": "PLmSFJQ2Y_ncx9LbSPKheXV5nlE-B7OU22",
    "Design Review Board": "PLmSFJQ2Y_ncwqzvxyLxoFRLWAoA5vLhaq",
}

_TITLE_DATE_RE = re.compile(
    r"(January|February|March|April|May|June|July|August|September|"
    r"October|November|December)\s+(\d{1,2})(?:st|nd|rd|th)?,?\s+(\d{4})",
    re.IGNORECASE,
)


# --- HTTP helpers ---

def fetch_html(url):
    req = urllib.request.Request(
        url,
        headers={"User-Agent": UA, "Accept": "text/html,application/xhtml+xml,*/*"},
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            charset = r.headers.get_content_charset() or "utf-8"
            return r.read().decode(charset, errors="replace")
    except urllib.error.HTTPError as e:
        print(f"  HTTP {e.code} — {url}", file=sys.stderr)
        return None
    except urllib.error.URLError as e:
        print(f"  ERROR fetching {url}: {e}", file=sys.stderr)
        return None


def download_pdf(path, dest_path):
    url = BASE_URL + path if path.startswith("/") else path
    req = urllib.request.Request(
        url,
        headers={"User-Agent": UA, "Accept": "application/pdf,application/msword,*/*"},
    )
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            with open(dest_path, "wb") as f:
                f.write(r.read())
        return True
    except Exception as e:
        print(f"  WARNING: {e}", file=sys.stderr)
        return False


# --- HTML parser for the Agenda Center search results ---

class AgendaParser(html.parser.HTMLParser):
    """
    Single-pass parser for the Westborough CivicPlus Agenda Center search
    results. Tracks h2 (board name) and h3 (meeting date), collecting
    ViewFile/Agenda and ViewFile/Minutes links between h3 boundaries.
    """

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
            # "?html=true" serves a tiny HTML wrapper (content-type:
            # text/html) instead of the real PDF (confirmed directly via
            # curl -I against a live Wayland MA link, same CivicPlus
            # platform as this town) — strip any query string so
            # downloads always hit the actual application/pdf response.
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
                "board": self._board,
                "meeting_date": self._current_date,
                "agenda_url": self._agenda_url,
                "minutes_url": self._minutes_url,
            })
        self._agenda_url = None
        self._minutes_url = None

    def get_items(self):
        self._flush()
        return self.items


# --- Video: WestboroughTV YouTube playlists ---

def parse_video_date(title):
    """Find a month-name date anywhere in a video title. Returns date or None."""
    m = _TITLE_DATE_RE.search(title)
    if not m:
        return None
    month, day, year = m.group(1), int(m.group(2)), int(m.group(3))
    try:
        return datetime.datetime.strptime(f"{month} {day}, {year}", "%B %d, %Y").date()
    except ValueError:
        return None


def collect_playlist_videos(board, playlist_id, cutoff, future_limit):
    ytdlp = shutil.which("yt-dlp")
    if not ytdlp:
        print("  WARNING: yt-dlp not found, skipping video", file=sys.stderr)
        return []
    cmd = [ytdlp, "--flat-playlist", "--dump-json", "--playlist-end", str(PLAYLIST_SCAN_CAP),
           f"https://www.youtube.com/playlist?list={playlist_id}"]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
    except subprocess.TimeoutExpired:
        print(f"  WARNING: playlist listing timed out for {board}", file=sys.stderr)
        return []
    if result.returncode != 0:
        print(f"  WARNING: playlist listing failed for {board}: {result.stderr.strip()[:200]}", file=sys.stderr)
        return []

    videos = []
    for line in result.stdout.splitlines():
        try:
            d = json.loads(line)
        except json.JSONDecodeError:
            continue
        title = d.get("title") or ""
        meeting_date = parse_video_date(title)
        if not meeting_date:
            continue
        if cutoff <= meeting_date <= future_limit:
            videos.append({"board": board, "meeting_date": meeting_date, "video_id": d.get("id"),
                            "title": title})
    return videos


def download_video(video_id, dest_path):
    cmd = [
        "yt-dlp", "--js-runtimes", YT_DLP_NODE,
        "--no-playlist",
        "-f", "bestvideo+bestaudio/best",
        "--merge-output-format", "mp4",
        "-o", dest_path,
        "--no-overwrites",
        "--quiet",
        "--no-warnings",
        f"https://www.youtube.com/watch?v={video_id}",
    ]
    try:
        subprocess.run(cmd, check=True, timeout=3600)
        return True
    except FileNotFoundError:
        print("  ERROR: yt-dlp not found. Install with: pip install yt-dlp", file=sys.stderr)
        return False
    except subprocess.CalledProcessError as e:
        print(f"  WARNING: yt-dlp failed ({e})", file=sys.stderr)
        return False
    except subprocess.TimeoutExpired:
        print(f"  WARNING: yt-dlp timed out downloading {video_id}", file=sys.stderr)
        return False


# --- File naming ---

def slugify(text, max_len=50):
    text = text.lower().strip()
    text = re.sub(r"[/\\&]", "-", text)
    text = re.sub(r"\s+-\s+", "-", text)
    text = re.sub(r"[^\w\s-]", "", text)
    text = re.sub(r"[\s_]+", "-", text)
    text = re.sub(r"-{2,}", "-", text)
    return text.strip("-")[:max_len]


def make_dest_path(board, doc_type, meeting_date, output_dir, ext=".pdf", counter=0):
    date_prefix = meeting_date.strftime("%Y-%m-%d")
    month_dir = os.path.join(output_dir, meeting_date.strftime("%Y-%m"))
    os.makedirs(month_dir, exist_ok=True)
    board_slug = slugify(board)
    suffix = f"-{counter}" if counter else ""
    return os.path.join(month_dir, f"{date_prefix}-{board_slug}-{doc_type}{suffix}{ext}")


def assign_counters(items, key_fn):
    seen = {}
    for item in items:
        key = key_fn(item)
        item["counter"] = seen.get(key, 0)
        seen[key] = item["counter"] + 1


# --- Main ---

def build_search_url(start_date, end_date):
    params = urllib.parse.urlencode({
        "term": "",
        "CIDs": "all",
        "startDate": start_date.strftime("%m/%d/%Y"),
        "endDate": end_date.strftime("%m/%d/%Y"),
        "dateRange": "Custom",
        "dateSelector": "0",
    })
    return f"{SEARCH_URL}?{params}"


def main():
    parser = argparse.ArgumentParser(
        description=(
            "Download Westborough MA municipal agendas, minutes, and video recordings "
            "for meetings within the past N days (and up to M days ahead)."
        )
    )
    parser.add_argument("--days", type=int, default=DAYS_BACK, metavar="N",
                        help=f"Look back N days (default: {DAYS_BACK})")
    parser.add_argument("--ahead", type=int, default=DAYS_AHEAD, metavar="N",
                        help=f"Also include meetings up to N days ahead (default: {DAYS_AHEAD})")
    parser.add_argument("--output-dir", default=OUTPUT_DIR, metavar="DIR",
                        help=f"Destination directory (default: {OUTPUT_DIR})")
    parser.add_argument("--dry-run", action="store_true",
                        help="List matching documents without downloading")
    parser.add_argument("--board", metavar="NAME",
                        help="Only process boards whose name contains NAME (case-insensitive)")
    parser.add_argument("--no-minutes", action="store_true", help="Skip minutes, download agendas only")
    parser.add_argument("--no-agendas", action="store_true", help="Skip agendas, download minutes only")
    parser.add_argument("--include-video", action="store_true",
                        help="Also download video recordings from WestboroughTV's YouTube playlists")
    parser.add_argument("--video-only", action="store_true", help="Download only video recordings")
    args = parser.parse_args()

    now = datetime.datetime.now()
    if (now.weekday() == 5 and now.hour >= 18) or (now.weekday() == 6 and now.hour < 12):
        print("Skipping — no downloads on Saturday nights or Sunday mornings.")
        sys.exit(0)

    do_docs = not args.video_only
    do_video = args.include_video or args.video_only

    today = datetime.date.today()
    start_date = today - datetime.timedelta(days=args.days)
    end_date = today + datetime.timedelta(days=args.ahead)
    board_filter = args.board.lower() if args.board else None

    print(f"Date window : {start_date} to {end_date}")
    if not args.dry_run:
        print(f"Output dir  : {args.output_dir}")
    print()

    docs = []
    if do_docs:
        html_text = fetch_html(build_search_url(start_date, end_date))
        if html_text:
            agenda_parser = AgendaParser()
            agenda_parser.feed(html_text)
            items = agenda_parser.get_items()
            if board_filter:
                items = [i for i in items if board_filter in i["board"].lower()]
            for item in items:
                if item["agenda_url"] and not args.no_agendas:
                    docs.append({"board": item["board"], "meeting_date": item["meeting_date"],
                                 "doc_type": "agenda", "href": item["agenda_url"]})
                if item["minutes_url"] and not args.no_minutes:
                    docs.append({"board": item["board"], "meeting_date": item["meeting_date"],
                                 "doc_type": "minutes", "href": item["minutes_url"]})
        print(f"Documents   : {len(docs)} found\n")

    videos = []
    if do_video:
        print("Fetching WestboroughTV playlists...")
        for board, playlist_id in COMMITTEE_PLAYLISTS.items():
            if board_filter and board_filter not in board.lower():
                continue
            videos += collect_playlist_videos(board, playlist_id, start_date, end_date)
            time.sleep(DELAY_SECONDS)
        print(f"Video       : {len(videos)} found\n")

    docs.sort(key=lambda x: (x["meeting_date"], x["board"]), reverse=True)
    videos.sort(key=lambda x: (x["meeting_date"], x["board"]), reverse=True)
    assign_counters(docs, lambda d: (d["board"], d["meeting_date"], d["doc_type"]))
    assign_counters(videos, lambda v: (v["board"], v["meeting_date"]))

    total = len(docs) + len(videos)
    if total == 0:
        print("No items found in the date window.")
        return

    if args.dry_run:
        if docs:
            print(f"{'Board':<40} {'Date':<12} Type")
            print("-" * 60)
            for d in docs:
                print(f"{d['board'][:39]:<40} {d['meeting_date']!s:<12} {d['doc_type']}")
            print()
        if videos:
            print(f"{'Board':<40} {'Date':<12} Video ID")
            print("-" * 60)
            for v in videos:
                print(f"{v['board'][:39]:<40} {v['meeting_date']!s:<12} {v['video_id']}")
            print()
        print(f"{total} item(s). Re-run without --dry-run to download.")
        return

    os.makedirs(args.output_dir, exist_ok=True)
    log_path = os.path.join(args.output_dir, "download-log.txt")
    log_lines = []
    downloaded = skipped = failed = 0

    for d in docs:
        dest = make_dest_path(d["board"], d["doc_type"], d["meeting_date"], args.output_dir,
                               counter=d["counter"])
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

    for v in videos:
        dest = make_dest_path(v["board"], "video", v["meeting_date"], args.output_dir,
                               ext=".mp4", counter=v["counter"])
        label = os.path.basename(dest)
        if os.path.exists(dest):
            print(f"  skip (exists)  {label}")
            skipped += 1
            continue
        print(f"  [{v['meeting_date']}] {v['board']} — video ({v['video_id']})")
        print(f"  downloading    {label}")
        if download_video(v["video_id"], dest):
            downloaded += 1
            log_lines.append(f"{datetime.datetime.now().isoformat()}  OK       {dest}")
        else:
            failed += 1
            log_lines.append(f"{datetime.datetime.now().isoformat()}  FAILED   video {v['video_id']}")

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
#    python3 scripts/download-westborough-agendas.py --dry-run
#
# 2. Narrow to one board:
#    python3 scripts/download-westborough-agendas.py --board "School Committee"
#
# 3. Download documents AND video recordings:
#    python3 scripts/download-westborough-agendas.py --include-video
#
# 4. Download only video recordings:
#    python3 scripts/download-westborough-agendas.py --video-only
#
# 5. Change the lookback window:
#    python3 scripts/download-westborough-agendas.py --days 14
#
# 6. Run on a schedule (cron — evening):
#    0 19 * * 1-5 cd /path/to/repo && python3 scripts/download-westborough-agendas.py
#
# COVERAGE: School Committee (Board of Education) and Recreation
# Commission (Parks & Recreation) are both native Agenda Center categories
# for documents, and both have dedicated WestboroughTV YouTube playlists
# for video — Recreation Commission's playlist has been dormant since
# October 2025, likely because the board itself hasn't met since, not a
# sourcing gap.
