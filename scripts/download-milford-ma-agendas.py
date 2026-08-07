#!/usr/bin/env python3
# download-milford-ma-agendas.py
# Download municipal meeting agendas, minutes, and video recordings from
# the Milford MA Agenda Center for meetings within the past N days (and up
# to 7 days ahead).
#
# NOTE ON NAMING: This repo already has scripts for Milford, CT
# (download-milford-agendas.py / send-milford-docs.py). This town uses the
# "-ma" suffix throughout (script names, output dir, CSV slug) to avoid
# colliding with those.
#
# USAGE:
#   python3 scripts/download-milford-ma-agendas.py [options]
#
# REQUIREMENTS:
#   - Python 3.6+  (no third-party packages needed for docs)
#   - yt-dlp       (for video: pip install yt-dlp)
#   - Internet connection
#
# WHAT IT DOES:
#   Documents (default or --docs-only):
#     1. Fetches the Milford MA Agenda Center search endpoint with a date
#        range spanning DAYS_BACK days ago through DAYS_AHEAD days ahead
#     2. Parses each board section and meeting row for board name, meeting
#        date, agenda URL, and minutes URL
#     3. Downloads PDFs to beat-archive/milford-ma-agendas/YYYY-MM/
#
#   Video (--include-video or --video-only):
#     4. Crawls each of MyMilfordTV's per-board YouTube playlists (see
#        VIDEO NOTE) for uploads within the date window, parsed by title
#     5. Appends a download log to beat-archive/milford-ma-agendas/download-log.txt
#
# SITE STRUCTURE (CivicPlus CivicEngage "Agenda Center", same platform as
# Waterford CT and Malden MA):
#   Hub:     https://www.milfordma.gov/AgendaCenter
#   Search:  https://www.milfordma.gov/AgendaCenter/Search/?term=&CIDs=all
#              &startDate=MM/DD/YYYY&endDate=MM/DD/YYYY&dateRange=Custom&dateSelector=0
#   Agenda:  https://www.milfordma.gov/AgendaCenter/ViewFile/Agenda/_MMDDYYYY-ID
#   Minutes: https://www.milfordma.gov/AgendaCenter/ViewFile/Minutes/_MMDDYYYY-ID
#
#   Checked directly: the AgendaCenter itself carries NO video links at all
#   (zero <td class="media"> cells found across a 6-month window) — video
#   is entirely a separate source, see VIDEO NOTE. School Committees (Board
#   of Education) and Parks Commission (Parks & Recreation) are both native
#   AgendaCenter categories — no separate document scrape needed for
#   either.
#
# VIDEO NOTE — MyMilfordTV (Milford's PEG access nonprofit) posts meeting
# recordings to a single YouTube channel (youtube.com/user/MyMilfordTV)
# organized into per-board playlists — the same shape as Arlington MA's
# ACMi, and a real upgrade over Melrose's single mixed-content channel.
# Confirmed playlists exist for Select Board, School Committee, Finance
# Committee, Planning Board, Zoning Board, the High School Building
# Committee, and Annual/Special Town Meetings — no Parks Commission
# playlist was found (checked the channel's full playlist list directly),
# so Parks Commission gets documents only, the same limitation Arlington's
# Parks & Recreation Commission has.
#
# Titles follow "{Board} Meeting: {Month} {Day}[st/nd/rd/th], {Year}"
# consistently (a small number of titles use a different board name than
# their containing playlist, e.g. "Capital Subcommittee Meeting" appears in
# the Finance Committee playlist — the title's own board name is used, not
# the playlist's, so these stay correctly labeled).

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
BASE_URL = "https://www.milfordma.gov"
SEARCH_URL = f"{BASE_URL}/AgendaCenter/Search/"
OUTPUT_DIR = "beat-archive/milford-ma-agendas"
DAYS_BACK = 4
DAYS_AHEAD = 7
DELAY_SECONDS = 0.8

UA = "Milford-MA-Agendas-Downloader/1.0 (journalism research)"

_H3_DATE_RE = re.compile(
    r"\b(Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)\s+(\d{1,2}),\s+(\d{4})\b"
)
_MONTH_ABBR = {
    "Jan": 1, "Feb": 2, "Mar": 3, "Apr": 4, "May": 5, "Jun": 6,
    "Jul": 7, "Aug": 8, "Sep": 9, "Oct": 10, "Nov": 11, "Dec": 12,
}

# Board name (as it appears in playlist titles) -> MyMilfordTV YouTube
# playlist. Confirmed by listing youtube.com/user/MyMilfordTV/playlists
# directly. No Parks Commission playlist exists — that board gets
# documents only, same as every board not listed here.
COMMITTEE_PLAYLISTS = {
    "Select Board": "PLoL-QQLIEANNUxCbd0bKDliYO6v8TeWqI",
    "School Committee": "PLoL-QQLIEANOZ_UKVetHVGvIhnvtr1rZc",
    "Finance Committee": "PLoL-QQLIEANM_RIeRrlvPTBCN8RAfqn4X",
    "Planning Board": "PLoL-QQLIEANNjRHo3dJ2GzF_pI82AYs6G",
    "Zoning Board": "PLoL-QQLIEANOUU2fLDVHinXqfB55nFmWW",
    "Milford High School Building Committee": "PLoL-QQLIEANO7hkNfh1nADeEaHrzu0zNv",
    "Annual/Special Town Meetings": "PLoL-QQLIEANP7DVXyHI47B9WQXzgOrBt6",
}

_YT_TITLE_RE = re.compile(
    r"^(.+?)\s+Meeting:\s*"
    r"(January|February|March|April|May|June|July|August|September|"
    r"October|November|December)\s+(\d{1,2})(?:st|nd|rd|th)?,?\s+(\d{4})\s*$",
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
    Single-pass parser for the Milford CivicPlus Agenda Center search
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


# --- Video: MyMilfordTV YouTube playlists ---

def parse_yt_title(title):
    """Split a MyMilfordTV playlist title into (board, meeting_date), or None."""
    m = _YT_TITLE_RE.match(title.strip())
    if not m:
        return None
    board = m.group(1).strip()
    month, day, year = m.group(2), int(m.group(3)), int(m.group(4))
    try:
        meeting_date = datetime.datetime.strptime(f"{month} {day}, {year}", "%B %d, %Y").date()
    except ValueError:
        return None
    return board, meeting_date


def collect_playlist_videos(playlist_id, cutoff, future_limit, board_filter, playlist_cap=40):
    ytdlp = shutil.which("yt-dlp")
    if not ytdlp:
        print("  WARNING: yt-dlp not found, skipping video", file=sys.stderr)
        return []
    cmd = [ytdlp, "--flat-playlist", "--dump-json", "--playlist-end", str(playlist_cap),
           f"https://www.youtube.com/playlist?list={playlist_id}"]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
    except subprocess.TimeoutExpired:
        print(f"  WARNING: playlist listing timed out for {playlist_id}", file=sys.stderr)
        return []
    if result.returncode != 0:
        print(f"  WARNING: playlist listing failed: {result.stderr.strip()[:200]}", file=sys.stderr)
        return []

    videos = []
    for line in result.stdout.splitlines():
        try:
            d = json.loads(line)
        except json.JSONDecodeError:
            continue
        parsed = parse_yt_title(d.get("title") or "")
        if not parsed:
            continue
        board, meeting_date = parsed
        if board_filter and board_filter not in board.lower():
            continue
        if cutoff <= meeting_date <= future_limit:
            videos.append({"board": board, "meeting_date": meeting_date, "video_id": d.get("id"),
                            "title": d.get("title")})
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
            "Download Milford MA municipal agendas, minutes, and video recordings "
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
                        help="Also download video recordings from MyMilfordTV's YouTube playlists")
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
        print("Fetching MyMilfordTV playlists...")
        for board, playlist_id in COMMITTEE_PLAYLISTS.items():
            if board_filter and board_filter not in board.lower():
                continue
            videos += collect_playlist_videos(playlist_id, start_date, end_date, board_filter)
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
#    python3 scripts/download-milford-ma-agendas.py --dry-run
#
# 2. Narrow to one board:
#    python3 scripts/download-milford-ma-agendas.py --board "School Committee"
#
# 3. Download documents AND video recordings:
#    python3 scripts/download-milford-ma-agendas.py --include-video
#
# 4. Download only video recordings:
#    python3 scripts/download-milford-ma-agendas.py --video-only
#
# 5. Change the lookback window:
#    python3 scripts/download-milford-ma-agendas.py --days 14
#
# 6. Run on a schedule (cron — evening):
#    0 19 * * 1-5 cd /path/to/repo && python3 scripts/download-milford-ma-agendas.py
#
# COVERAGE: School Committees (Board of Education) and Parks Commission
# (Parks & Recreation) are both native Agenda Center categories — no
# separate document source needed. Parks Commission has no video source
# (no MyMilfordTV playlist exists for it, checked directly) — documents
# only for that board, the same limitation Arlington MA's Parks &
# Recreation Commission has.
