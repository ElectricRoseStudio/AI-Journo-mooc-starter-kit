#!/usr/bin/env python3
# download-swampscott-agendas.py
# Download municipal meeting agendas, minutes, and video recordings from
# the Swampscott MA Agenda Center for meetings within the past N days
# (and up to 7 days ahead).
#
# USAGE:
#   python3 scripts/download-swampscott-agendas.py [options]
#
# REQUIREMENTS:
#   - Python 3.6+  (no third-party packages needed for docs)
#   - Internet connection
#
# WHAT IT DOES:
#   Documents (default or --docs-only):
#     1. Fetches the Swampscott MA Agenda Center search endpoint with a
#        date range spanning DAYS_BACK days ago through DAYS_AHEAD days
#        ahead
#     2. Parses each board section and meeting row for board name,
#        meeting date, agenda URL, and minutes URL
#     3. Downloads PDFs to beat-archive/swampscott-agendas/YYYY-MM/
#
#   Video (--include-video or --video-only):
#     4. Queries Swampscott TV's Cablecast API directly (see VIDEO NOTE)
#        and downloads matching recordings
#
# SITE STRUCTURE (CivicPlus CivicEngage "Agenda Center", same platform as
# most other MA towns in this repo):
#   Hub:     https://www.swampscottma.gov/AgendaCenter
#   Search:  https://www.swampscottma.gov/AgendaCenter/Search/?term=&CIDs=all
#              &startDate=MM/DD/YYYY&endDate=MM/DD/YYYY&dateRange=Custom&dateSelector=0
#   Agenda:  https://www.swampscottma.gov/AgendaCenter/ViewFile/Agenda/_MMDDYYYY-ID
#   Minutes: https://www.swampscottma.gov/AgendaCenter/ViewFile/Minutes/_MMDDYYYY-ID
#
# VIDEO NOTE — Swampscott TV (the town's PEG access org) runs on Cablecast
# (Tightrope Media Systems), the same platform already solved for North
# Andover MA and Peabody MA elsewhere in this repo, but yet another
# tenant, this one proxied directly on the town's own domain rather than
# through a *.cablecast.tv subdomain:
#   GET https://tv.swampscottma.gov/cablecastapi/v1/shows?search={board}&sort=-eventDate
# The Agenda Center's own <td class="media"> cell links to this same
# platform (tv.swampscottma.gov/internetchannel/show/{id}) but only for
# Select Board — checked directly across an 8-month window and no other
# board's agenda rows carry a media link at all. Cablecast's own search
# has much broader coverage (School Committee alone has 50+ recordings
# going back to 2024), so video is collected independently from Cablecast
# by board name — the same approach used for North Andover/Peabody —
# rather than by parsing the Agenda Center HTML for media links. Unlike
# those two towns, meeting dates come directly from each show's own
# eventDate field rather than being parsed back out of the title, since
# it's already there and authoritative. Each matched show's direct,
# unauthenticated MP4 URL comes from:
#   GET https://tv.swampscottma.gov/cablecastapi/v1/vods/{vod_id}
#
# COVERAGE: Both School Committee (Board of Education) and Recreation
# Commission (Parks & Recreation) are native Agenda Center categories —
# confirmed directly with real, current document postings for both — no
# separate document source needed for either. Recreation Commission has
# no real video coverage on Cablecast — its only search hit is an
# unrelated 2020 talent-show broadcast, not a board meeting.

import argparse
import datetime
import html.parser
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

# --- Configuration ---
BASE_URL = "https://www.swampscottma.gov"
SEARCH_URL = f"{BASE_URL}/AgendaCenter/Search/"
CABLECAST_BASE = "https://tv.swampscottma.gov/cablecastapi/v1"
OUTPUT_DIR = "beat-archive/swampscott-agendas"
DAYS_BACK = 4
DAYS_AHEAD = 7
DELAY_SECONDS = 0.8

UA = "Swampscott-MA-Agendas-Downloader/1.0 (journalism research)"

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
            import json
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
    req = urllib.request.Request(video_url, headers={"User-Agent": UA})
    try:
        with urllib.request.urlopen(req, timeout=120) as r, open(dest_path, "wb") as f:
            while True:
                chunk = r.read(1024 * 256)
                if not chunk:
                    break
                f.write(chunk)
        return True
    except Exception as e:
        print(f"  WARNING: {e}", file=sys.stderr)
        if os.path.exists(dest_path):
            os.remove(dest_path)
        return False


# --- (1) Documents ---

def fetch_known_boards():
    html_text = fetch_html(f"{BASE_URL}/AgendaCenter")
    if not html_text:
        return set()
    return {m.strip() for m in _H2_RE.findall(html_text)}


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


# --- (2) Video ---

def fetch_cablecast_shows(search=None, page_size=25):
    params = {"pageSize": page_size, "sort": "-eventDate"}
    if search:
        params["search"] = search
    url = f"{CABLECAST_BASE}/shows?{urllib.parse.urlencode(params)}"
    data = fetch_json(url)
    return (data or {}).get("shows", [])


def fetch_cablecast_vod_url(vod_id):
    data = fetch_json(f"{CABLECAST_BASE}/vods/{vod_id}")
    return (data or {}).get("vod", {}).get("url")


def collect_videos(cutoff, future_limit, board_filter, known_boards):
    all_boards = set(known_boards)
    if board_filter:
        all_boards = {b for b in all_boards if board_filter in b.lower()}
    videos = []
    seen_show_ids = set()
    for board in sorted(all_boards):
        for show in fetch_cablecast_shows(search=board):
            if show["id"] in seen_show_ids:
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
            title = show.get("title") or ""
            if board.lower() not in title.lower():
                continue
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
            "Download Swampscott MA municipal agendas, minutes, and video "
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
    parser.add_argument("--include-video", action="store_true", help="Also download Swampscott TV video recordings")
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

    videos = []
    if do_video:
        print("Fetching known board list (for video search)...")
        known_boards = fetch_known_boards()
        print(f"  {len(known_boards)} board(s) known.")
        print("Fetching Swampscott TV (Cablecast) recordings...")
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
#    python3 scripts/download-swampscott-agendas.py --dry-run
#
# 2. Just School Committee:
#    python3 scripts/download-swampscott-agendas.py --board "School Committee"
#
# 3. PDFs only (no video — the default; --include-video/--video-only turn it on):
#    python3 scripts/download-swampscott-agendas.py
#
# 4. Video only:
#    python3 scripts/download-swampscott-agendas.py --video-only
#
# 5. Change the lookback window:
#    python3 scripts/download-swampscott-agendas.py --days 14
#
# 6. Run on a schedule (cron — evening):
#    0 19 * * 1-5 cd /path/to/repo && python3 scripts/download-swampscott-agendas.py
