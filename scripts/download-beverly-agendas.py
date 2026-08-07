#!/usr/bin/env python3
# download-beverly-agendas.py
# Download municipal meeting agendas, minutes, and video recordings from
# Beverly MA for meetings whose date falls within the past N days (and up
# to 7 days ahead).
#
# USAGE:
#   python3 scripts/download-beverly-agendas.py [options]
#
# REQUIREMENTS:
#   - Python 3.6+  (no third-party packages needed for docs)
#   - yt-dlp       (for video: pip install yt-dlp)
#   - Internet connection
#
# WHAT IT DOES:
#   1. City boards: fetches the Beverly Agenda Center search endpoint
#      (CivicPlus) for the date window and downloads Agenda/Minutes PDFs.
#   2. School Committee (+ its subcommittees): scrapes the Beverly Public
#      Schools site (a different, separate platform — see SITE STRUCTURE).
#   3. Video: crawls BevCam's YouTube playlists for School Committee, City
#      Council, Planning Board, and Legal Affairs Committee.
#   4. Appends a download log to beat-archive/beverly-agendas/download-log.txt
#
# SITE STRUCTURE:
#   (1) City boards — classic CivicPlus Agenda Center, same platform as
#       Waterford CT, Malden/Milford/Shrewsbury/Westborough/Wayland MA:
#         https://www.beverlyma.gov/AgendaCenter/Search/?term=&CIDs=all
#           &startDate=MM/DD/YYYY&endDate=MM/DD/YYYY&dateRange=Custom&dateSelector=0
#       No embedded video links exist here (checked directly — zero
#       <td class="media"> cells across a 6-month window).
#       Parks & Recreation Commission is a native category here — Beverly
#       is a city, not a town, and (unlike every other MA town in this
#       repo) School Committee is NOT in this system at all.
#
#   (2) School Committee — beverlyschools.org, an Edlio-hosted school CMS,
#       completely separate from the city's Agenda Center (confirmed
#       directly — no School Committee category exists on
#       beverlyma.gov/AgendaCenter). Document links are plain <a
#       class="attachment-type-pdf" href="https://N.files.edl.io/...">
#       {title}</a> where {title} embeds both date and doc type, e.g.
#       "2026 06 10 School Committee Agenda" — parsed by finding a
#       YYYY MM DD prefix and using the remainder as doc_type.
#
#       The site organizes agendas by SCHOOL YEAR, and each year's page
#       tree is hand-built by district staff — there's no stable API and
#       no guarantee next year's page IDs will resemble this year's.
#       Fetches from two known trees as of 2026-08-07:
#         - The 2025-2026 tree (uREC_ID=4443590), which splits into 7
#           sibling sub-committee pages (pREC_ID values below) —
#           Committee of the Whole, Regular School Committee, Finance &
#           Facilities, Curriculum/Instruction & Student Life, Policy
#           Review Sub Committee, Negotiations Committee, Special
#           Meetings.
#         - The 2026-27 page (uREC_ID=2083160, pREC_ID=2756436), which
#           as of this writing mixes all committees on one page rather
#           than splitting by sub-committee the way 2025-2026 did — the
#           script just scrapes whatever's there and doesn't assume a
#           particular sub-page structure will exist for this year.
#       THIS WILL NEED REVISITING each new school year — check
#       beverlyschools.org's School Committee > Agendas section for
#       the current year's actual page IDs and update BPS_YEAR_PAGES below.
#
#   (3) Video — BevCam (Beverly's PEG access nonprofit) has a YouTube
#       channel with genuine per-board playlists (a clean setup, like
#       Milford MA's MyMilfordTV and unlike Westborough's 58-playlist
#       needle-in-haystack search): School Committee Meetings, City
#       Council Meetings, Planning Board Meetings, Legal Affairs
#       Committee. No Parks & Recreation Commission playlist exists —
#       that board is documents only, the same limitation several other
#       towns' minor boards have in this repo. Titles mix MM/DD/YYYY and
#       MM-DD-YYYY formats; date is found by search, not anchored to a
#       fixed position, matching the approach already proven for
#       Westborough and Sudbury.

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
CITY_BASE = "https://www.beverlyma.gov"
CITY_SEARCH_URL = f"{CITY_BASE}/AgendaCenter/Search/"
BPS_BASE = "https://www.beverlyschools.org"
OUTPUT_DIR = "beat-archive/beverly-agendas"
DAYS_BACK = 4
DAYS_AHEAD = 7
DELAY_SECONDS = 0.6
PLAYLIST_SCAN_CAP = 60

UA = "Beverly-MA-Agendas-Downloader/1.0 (journalism research)"

# --- (1) City Agenda Center ---

_H3_DATE_RE = re.compile(
    r"\b(Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)\s+(\d{1,2}),\s+(\d{4})\b"
)
_MONTH_ABBR = {
    "Jan": 1, "Feb": 2, "Mar": 3, "Apr": 4, "May": 5, "Jun": 6,
    "Jul": 7, "Aug": 8, "Sep": 9, "Oct": 10, "Nov": 11, "Dec": 12,
}


class AgendaParser(html.parser.HTMLParser):
    """Single-pass parser for the Beverly CivicPlus Agenda Center search results."""

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
            # "?html=true" serves a tiny HTML wrapper instead of the real
            # PDF (confirmed on this same CivicPlus platform for Wayland
            # MA) — strip any query string.
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


def build_city_search_url(start_date, end_date):
    params = urllib.parse.urlencode({
        "term": "", "CIDs": "all",
        "startDate": start_date.strftime("%m/%d/%Y"),
        "endDate": end_date.strftime("%m/%d/%Y"),
        "dateRange": "Custom", "dateSelector": "0",
    })
    return f"{CITY_SEARCH_URL}?{params}"


def collect_city_docs(cutoff, future_limit, board_filter, no_minutes, no_agendas):
    docs = []
    html_text = fetch_html(build_city_search_url(cutoff, future_limit))
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


# --- (2) Beverly Public Schools: School Committee ---

# Known page trees as of 2026-08-07 — see SITE STRUCTURE note above about
# why these will need updating in future school years.
BPS_YEAR_PAGES = [
    # 2025-2026 tree: uREC_ID=4443590, one page per sub-committee.
    ("https://www.beverlyschools.org/apps/pages/index.jsp?uREC_ID=4443590&type=d&pREC_ID=2681411",),
    ("https://www.beverlyschools.org/apps/pages/index.jsp?uREC_ID=4443590&type=d&pREC_ID=2681410",),
    ("https://www.beverlyschools.org/apps/pages/index.jsp?uREC_ID=4443590&type=d&pREC_ID=2681412",),
    ("https://www.beverlyschools.org/apps/pages/index.jsp?uREC_ID=4443590&type=d&pREC_ID=2681413",),
    ("https://www.beverlyschools.org/apps/pages/index.jsp?uREC_ID=4443590&type=d&pREC_ID=2681414",),
    ("https://www.beverlyschools.org/apps/pages/index.jsp?uREC_ID=4443590&type=d&pREC_ID=2714000",),
    ("https://www.beverlyschools.org/apps/pages/index.jsp?uREC_ID=4443590&type=d&pREC_ID=2744236",),
    # 2026-27 tree: currently a single mixed page, not yet split by
    # sub-committee.
    ("https://www.beverlyschools.org/apps/pages/index.jsp?uREC_ID=2083160&type=d&pREC_ID=2756436",),
]

_BPS_ATTACHMENT_RE = re.compile(
    r'class="attachment-type-[a-z]+"[^>]*href="([^"]+)"[^>]*>([^<]+)</a>'
)
_BPS_TITLE_DATE_RE = re.compile(r"^(\d{4})\s+(\d{1,2})\s+(\d{1,2})\s*(.*)$")


def collect_school_committee_docs(cutoff, future_limit, board_filter, no_agendas):
    docs = []
    if board_filter and "school" not in board_filter and "committee" not in board_filter:
        return docs
    if no_agendas:
        return docs  # this source's single PDF per row isn't split by type

    for (url,) in BPS_YEAR_PAGES:
        html_text = fetch_html(url)
        if not html_text:
            continue
        for href, title in _BPS_ATTACHMENT_RE.findall(html_text):
            title = title.strip()
            m = _BPS_TITLE_DATE_RE.match(title)
            if not m:
                continue
            year, month, day, doc_type = int(m.group(1)), int(m.group(2)), int(m.group(3)), m.group(4).strip()
            try:
                meeting_date = datetime.date(year, month, day)
            except ValueError:
                continue
            if not (cutoff <= meeting_date <= future_limit):
                continue
            docs.append({
                "board": "School Committee", "meeting_date": meeting_date,
                "doc_type": doc_type or "document", "href": href,
            })
        time.sleep(DELAY_SECONDS)
    return docs


# --- (3) Video: BevCam YouTube playlists ---

COMMITTEE_PLAYLISTS = {
    "School Committee": "PL1ZUt4ybG_IDM8b-acl-5EmXpe-NWPBBW",
    "City Council": "PL1ZUt4ybG_IAyHEQSAEvtGQ5g_2hVHv8S",
    "Planning Board": "PL1ZUt4ybG_IAXrt7Sbd2hHqSRC8LbVbgl",
    "Legal Affairs Committee": "PL1ZUt4ybG_IBFjraxBZBXss63aYrLpT5N",
}

_VIDEO_TITLE_DATE_RE = re.compile(r"(\d{1,2})[/-](\d{1,2})[/-](\d{2,4})")


def parse_video_date(title):
    m = _VIDEO_TITLE_DATE_RE.search(title)
    if not m:
        return None
    month, day, year = int(m.group(1)), int(m.group(2)), int(m.group(3))
    if year < 100:
        year += 2000
    try:
        return datetime.date(year, month, day)
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
            videos.append({"board": board, "meeting_date": meeting_date,
                           "video_id": d.get("id"), "title": title})
    return videos


def download_video(video_id, dest_path):
    cmd = [
        "yt-dlp", "--js-runtimes", YT_DLP_NODE,
        "--no-playlist", "-f", "bestvideo+bestaudio/best",
        "--merge-output-format", "mp4", "-o", dest_path,
        "--no-overwrites", "--quiet", "--no-warnings",
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


# --- Shared HTTP helper ---

def fetch_html(url):
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "text/html,*/*"})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            charset = r.headers.get_content_charset() or "utf-8"
            return r.read().decode(charset, errors="replace")
    except Exception as e:
        print(f"  WARNING: fetch failed for {url}: {e}", file=sys.stderr)
        return None


def download_file(url, dest_path):
    # City Agenda Center links are relative ("/AgendaCenter/..."); school
    # site links (edl.io) are always absolute already.
    if url.startswith("/"):
        url = CITY_BASE + url
    safe_url = urllib.parse.quote(url, safe=":/?&=%")
    req = urllib.request.Request(safe_url, headers={"User-Agent": UA, "Accept": "*/*"})
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            with open(dest_path, "wb") as f:
                f.write(r.read())
        return True
    except Exception as e:
        print(f"  WARNING: {e}", file=sys.stderr)
        return False


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
    fname = f"{date_str}-{slugify(board)}-{slugify(doc_type)}{suffix}{ext}"
    return os.path.join(month_dir, fname)


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
            "Download Beverly MA municipal agendas, minutes, and video "
            "recordings for meetings within the past N days (and up to 7 ahead)."
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
                        help="Only include boards containing NAME (case-insensitive)")
    parser.add_argument("--no-minutes", action="store_true", help="Skip minutes (city boards only)")
    parser.add_argument("--no-agendas", action="store_true", help="Skip agendas/documents")
    parser.add_argument("--no-video", action="store_true", help="Skip video (docs only)")
    parser.add_argument("--docs-only", action="store_true", help="Alias for --no-video")
    parser.add_argument("--video-only", action="store_true", help="Skip documents (video only)")
    args = parser.parse_args()

    now = datetime.datetime.now()
    if (now.weekday() == 5 and now.hour >= 18) or (now.weekday() == 6 and now.hour < 12):
        print("Skipping — no downloads on Saturday nights or Sunday mornings.")
        sys.exit(0)

    include_video = not (args.no_video or args.docs_only)
    include_docs = not args.video_only

    today = datetime.date.today()
    cutoff = today - datetime.timedelta(days=args.days)
    future_limit = today + datetime.timedelta(days=args.ahead)
    board_filter = args.board.lower() if args.board else None

    print(f"Date window : {cutoff} to {future_limit}")
    if not args.dry_run:
        print(f"Output dir  : {args.output_dir}")
    print()

    docs = []
    if include_docs:
        print("Fetching city Agenda Center...")
        c_docs = collect_city_docs(cutoff, future_limit, board_filter, args.no_minutes, args.no_agendas)
        docs += c_docs
        print(f"  {len(c_docs)} document(s).")

        print("Fetching School Committee (beverlyschools.org)...")
        s_docs = collect_school_committee_docs(cutoff, future_limit, board_filter, args.no_agendas)
        docs += s_docs
        print(f"  {len(s_docs)} document(s).\n")

    videos = []
    if include_video:
        print("Fetching BevCam playlists...")
        for board, playlist_id in COMMITTEE_PLAYLISTS.items():
            if board_filter and board_filter not in board.lower():
                continue
            videos += collect_playlist_videos(board, playlist_id, cutoff, future_limit)
            time.sleep(DELAY_SECONDS)
        print(f"  {len(videos)} video(s).\n")

    docs.sort(key=lambda x: (x["meeting_date"], x["board"]), reverse=True)
    videos.sort(key=lambda x: (x["meeting_date"], x["board"]), reverse=True)
    assign_counters(docs, lambda d: (d["board"], d["meeting_date"], d["doc_type"]))
    assign_counters(videos, lambda v: (v["board"], v["meeting_date"]))

    total = len(docs) + len(videos)
    print(f"{total} item(s) total in window.\n")

    if args.dry_run:
        if docs:
            print(f"{'Board':<30} {'Date':<12} Type")
            print("-" * 60)
            for d in docs:
                print(f"{d['board'][:29]:<30} {d['meeting_date']!s:<12} {d['doc_type']}")
            print()
        if videos:
            print(f"{'Board':<30} {'Date':<12} Video")
            print("-" * 60)
            for v in videos:
                print(f"{v['board'][:29]:<30} {v['meeting_date']!s:<12} {v['title']}")
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
        if download_file(d["href"], dest):
            downloaded += 1
            log_lines.append(f"{datetime.datetime.now().isoformat()}  OK       {dest}")
        else:
            failed += 1
            log_lines.append(f"{datetime.datetime.now().isoformat()}  FAILED   {d['href']}")
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
    print(f"Files in: {args.output_dir}")
    if log_lines:
        print(f"Log:      {log_path}")


if __name__ == "__main__":
    main()


# --- Tips ---
#
# 1. Preview without downloading:
#    python3 scripts/download-beverly-agendas.py --dry-run
#
# 2. Just School Committee:
#    python3 scripts/download-beverly-agendas.py --board "School Committee"
#
# 3. PDFs only (no video):
#    python3 scripts/download-beverly-agendas.py --no-video
#
# 4. Video only:
#    python3 scripts/download-beverly-agendas.py --video-only
#
# 5. Change the lookback window:
#    python3 scripts/download-beverly-agendas.py --days 14
#
# 6. Run on a schedule (cron — evening):
#    0 19 * * 1-5 cd /path/to/repo && python3 scripts/download-beverly-agendas.py
#
# COVERAGE: Parks & Recreation Commission is a native city Agenda Center
# category (documents only — no BevCam playlist exists for it). School
# Committee (Board of Education) is NOT on the city's Agenda Center at
# all — it's sourced separately from beverlyschools.org (documents) and
# BevCam's "School Committee Meetings" YouTube playlist (video). See the
# SITE STRUCTURE note above about BPS_YEAR_PAGES needing a manual update
# each new school year.
