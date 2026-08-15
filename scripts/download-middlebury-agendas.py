#!/usr/bin/env python3
# download-middlebury-agendas.py
# Download municipal meeting agendas, minutes, and (best-effort) video
# recordings from the Middlebury, CT Agenda Center for meetings within
# the past N days (and up to M days ahead).
#
# USAGE:
#   python3 scripts/download-middlebury-agendas.py [options]
#
# REQUIREMENTS:
#   - Python 3.6+  (no third-party packages needed for docs)
#   - Internet connection
#
# WHAT IT DOES:
#   Documents (default or --docs-only):
#     1. Fetches the Middlebury CT Agenda Center search endpoint with a
#        date range spanning DAYS_BACK days ago through DAYS_AHEAD days
#        ahead
#     2. Parses each board section and meeting row for board name,
#        meeting date, agenda URL, and minutes URL
#     3. Downloads PDFs to beat-archive/middlebury-agendas/YYYY-MM/
#
#   Video (--include-video or --video-only; off by default — see VIDEO NOTE):
#     4. Searches the regional Cablecast (Tightrope Media Systems) archive
#        for shows whose title matches a known board name and whose event
#        date falls in the window, and downloads any that have a published
#        VOD
#
# SITE STRUCTURE (CivicPlus CivicEngage "Agenda Center", same platform as
# Clinton/Haddam/Salem/Swampscott/Woodbridge/Middlefield/East Hampton/
# Chester/Essex/Deep River/Newington/Norwich/Wethersfield elsewhere in
# this repo):
#   Hub:     https://www.middleburyct.gov/AgendaCenter
#   Search:  https://www.middleburyct.gov/AgendaCenter/Search/?term=&CIDs=all
#              &startDate=MM/DD/YYYY&endDate=MM/DD/YYYY&dateRange=Custom&dateSelector=0
#   Agenda:  https://www.middleburyct.gov/AgendaCenter/ViewFile/Agenda/_MMDDYYYY-ID
#   Minutes: https://www.middleburyct.gov/AgendaCenter/ViewFile/Minutes/_MMDDYYYY-ID
#
#   NOTE: the town's own /agendacenter link on middlebury-ct.org
#   301-redirects every request to middleburyct.gov (no hyphen), so this
#   script uses the .gov domain directly to skip that extra hop.
#
# VIDEO NOTE — No reliable per-meeting video source was found for
#   Middlebury. Checked directly before writing this script:
#     - No YouTube/Vimeo/Facebook-Live/PEG link anywhere on
#       middleburyct.gov.
#     - The regional Cablecast (Tightrope Media Systems) instance at
#       reflect-vsctv.cablecast.tv — the same platform already used for
#       Durham/Middlefield/Chester/Essex/Haddam elsewhere in this repo —
#       has no Middlebury project at all.
#     - A YouTube search for '"Middlebury, CT" Board of Selectmen' and
#       "Middlebury CT Board of Selectmen meeting" returns nothing
#       relevant (unrelated towns and unrelated video noise only).
#   Video search is included anyway for completeness and left OFF by
#   default (--include-video/--video-only to try it) — expect it to find
#   nothing until/unless Middlebury starts recording meetings somewhere
#   discoverable.

import argparse
import datetime
import html.parser
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

# --- Configuration ---
BASE_URL = "https://www.middleburyct.gov"
SEARCH_URL = f"{BASE_URL}/AgendaCenter/Search/"
CABLECAST_BASE = "https://reflect-vsctv.cablecast.tv/cablecastapi/v1"
OUTPUT_DIR = "beat-archive/middlebury-agendas"
DAYS_BACK = 4
DAYS_AHEAD = 7
DELAY_SECONDS = 0.8

UA = "Middlebury-CT-Agendas-Downloader/1.0 (journalism research)"

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


def fetch_json(url):
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return json.loads(r.read().decode("utf-8", errors="replace"))
    except Exception as e:
        print(f"  WARNING: fetch failed for {url}: {e}", file=sys.stderr)
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


def download_video_file(video_url, dest_path):
    # Written to a .part temp file and only renamed to dest_path once the
    # whole transfer succeeds, so a run killed mid-download (e.g. by the
    # cron timeout wrapper) can't leave a truncated file that future runs'
    # "if os.path.exists(dest): skip" check would treat as already done.
    tmp_path = dest_path + ".part"
    req = urllib.request.Request(video_url, headers={"User-Agent": UA})
    try:
        with urllib.request.urlopen(req, timeout=120) as r, open(tmp_path, "wb") as f:
            while True:
                chunk = r.read(1024 * 256)
                if not chunk:
                    break
                f.write(chunk)
        os.replace(tmp_path, dest_path)
        return True
    except Exception as e:
        print(f"  WARNING: {e}", file=sys.stderr)
        if os.path.exists(tmp_path):
            os.remove(tmp_path)
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


# --- (2) Video (best-effort — see VIDEO NOTE above) ---

def fetch_cablecast_shows(search, page_size=25):
    params = {"pageSize": page_size, "sort": "-eventDate", "search": search}
    data = fetch_json(f"{CABLECAST_BASE}/shows?{urllib.parse.urlencode(params)}")
    return (data or {}).get("shows", [])


def fetch_cablecast_vod_url(vod_id):
    data = fetch_json(f"{CABLECAST_BASE}/vods/{vod_id}")
    return (data or {}).get("vod", {}).get("url")


def collect_videos(cutoff, future_limit, board_filter, known_boards):
    boards = set(known_boards)
    if board_filter:
        boards = {b for b in boards if board_filter in b.lower()}
    videos = []
    seen_show_ids = set()
    for board in sorted(boards):
        for show in fetch_cablecast_shows(search=f"Middlebury {board}"):
            if show["id"] in seen_show_ids:
                continue
            title = show.get("title") or ""
            if "middlebury" not in title.lower() or board.lower() not in title.lower():
                continue
            raw_date = show.get("eventDate") or ""
            try:
                meeting_date = datetime.date.fromisoformat(raw_date[:10])
            except ValueError:
                continue
            if not (cutoff <= meeting_date <= future_limit):
                continue
            if not show.get("vods"):
                continue  # scheduled but not yet recorded/published
            seen_show_ids.add(show["id"])
            videos.append({
                "board": board, "meeting_date": meeting_date,
                "vod_id": show["vods"][0], "title": title,
            })
    return videos


def download_video(vod_id, dest_path):
    url = fetch_cablecast_vod_url(vod_id)
    if not url:
        return False
    return download_video_file(url, dest_path)


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
            "Download Middlebury CT municipal agendas, minutes, and (best-effort) "
            "video for meetings within the past N days (and up to M days ahead)."
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
    parser.add_argument("--include-video", action="store_true",
                        help="Also search for Cablecast video recordings (see VIDEO NOTE — low/no yield expected)")
    parser.add_argument("--video-only", action="store_true", help="Search only for video recordings")
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

    videos = []
    if do_video:
        known_boards = {d["board"] for d in docs}
        if not known_boards:
            # Fall back to a wide window so there's still a board-name list
            # to search Cablecast against even when nothing falls inside
            # the (narrow, default) document date window.
            wide_docs = collect_docs(today - datetime.timedelta(days=365), future_limit, board_filter, False, False)
            known_boards = {d["board"] for d in wide_docs}
        print(f"Searching Cablecast for {len(known_boards)} known board name(s)...")
        videos = collect_videos(cutoff, future_limit, board_filter, known_boards)
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
            print("-" * 65)
            for d in docs:
                print(f"{d['board'][:39]:<40} {d['meeting_date']!s:<12} {d['doc_type']}")
            print()
        if videos:
            print(f"{'Board':<40} {'Date':<12} Video")
            print("-" * 65)
            for v in videos:
                print(f"{v['board'][:39]:<40} {v['meeting_date']!s:<12} {v['title']}")
            print()
        print(f"{total} item(s). Re-run without --dry-run to download.")
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

    for v in videos:
        dest = make_path(v["board"], "video", v["meeting_date"], args.output_dir, ext=".mp4", counter=v["counter"])
        label = os.path.basename(dest)
        if os.path.exists(dest):
            print(f"  skip (exists)  {label}")
            skipped += 1
            continue
        print(f"  [{v['meeting_date']}] {v['board']} — video ({v['title']})")
        print(f"  downloading    {label}")
        if download_video(v["vod_id"], dest):
            downloaded += 1
            log_lines.append(f"{datetime.datetime.now().isoformat()}  OK       {dest}")
        else:
            failed += 1
            log_lines.append(f"{datetime.datetime.now().isoformat()}  FAILED   video {v['vod_id']}")

    if log_lines:
        with open(log_path, "a") as f:
            f.write("\n".join(log_lines) + "\n")

    print()
    print(f"Done — downloaded: {downloaded}  skipped: {skipped}  failed: {failed}")
    print(f"Files in: {args.output_dir}")
    if log_lines:
        print(f"Log:      {log_path}")


if __name__ == "__main__":
    main()


# --- Tips ---
#
# 1. Preview without downloading:
#    python3 scripts/download-middlebury-agendas.py --dry-run
#
# 2. Just Board of Selectmen:
#    python3 scripts/download-middlebury-agendas.py --board "Board of Selectmen"
#
# 3. Change the lookback window:
#    python3 scripts/download-middlebury-agendas.py --days 14
#
# 4. Also try Cablecast video search (see VIDEO NOTE — expect little/nothing):
#    python3 scripts/download-middlebury-agendas.py --include-video
#
# 5. Run on a schedule (cron — evening):
#    0 21 * * 1-5 cd /path/to/repo && python3 scripts/download-middlebury-agendas.py
