#!/usr/bin/env python3
# download-waterford-agendas.py
# Download municipal meeting agendas, minutes, and video recordings from the
# Waterford CT Agenda Center for meetings within the past N days (and up to 7
# days ahead, to catch agendas posted early for upcoming meetings).
#
# USAGE:
#   python3 scripts/download-waterford-agendas.py [options]
#
# REQUIREMENTS:
#   - Python 3.6+  (no third-party packages needed for docs)
#   - yt-dlp       (for specific video links: pip install yt-dlp)
#   - Internet connection
#
# WHAT IT DOES:
#   Documents (default or --docs-only):
#     1. Fetches the Waterford CT Agenda Center search endpoint with a date
#        range spanning DAYS_BACK days ago through DAYS_AHEAD days ahead
#        (filtered by meeting date — the site does not expose posted dates)
#     2. Parses each board section and meeting row for board name, meeting date,
#        agenda URL, and minutes URL
#     3. Downloads PDFs to beat-archive/waterford-agendas/YYYY-MM/
#     4. Appends a download log to beat-archive/waterford-agendas/download-log.txt
#
#   Video (--include-video or --video-only):
#     5. Extracts YouTube links from each meeting row (linked via an
#        img alt="Videos" icon)
#     6. Specific videos (watch?v= or youtu.be/ URLs) are downloaded with yt-dlp
#     7. Channel/stream-page URLs (e.g. @TownofWaterfordCT-Stream/streams) are
#        saved as .url Internet Shortcut files instead — they point to the live
#        stream archive rather than a single downloadable recording
#
# SITE STRUCTURE (CivicPlus CivicEngage):
#   Hub:     https://www.waterfordct.org/agendacenter
#   Search:  https://www.waterfordct.org/agendacenter/Search/?term=&CIDs=all
#              &startDate=MM/DD/YYYY&endDate=MM/DD/YYYY&dateRange=Custom&dateSelector=0
#   Agenda:  https://www.waterfordct.org/AgendaCenter/ViewFile/Agenda/_MMDDYYYY-ID
#   Minutes: https://www.waterfordct.org/AgendaCenter/ViewFile/Minutes/_MMDDYYYY-ID
#   Video:   YouTube URLs embedded as icon links (alt="Videos"); some boards
#            link specific watch?v= recordings, others link the channel stream
#            page (youtube.com/@TownofWaterfordCT-Stream/streams)
#
#   Search result page layout per board section:
#     <h2>Board Name</h2>
#     <h3><strong>Mon DD, YYYY</strong></h3>
#     <a href="/AgendaCenter/ViewFile/Agenda/_MMDDYYYY-ID">...</a>
#     <a href="/AgendaCenter/ViewFile/Minutes/_MMDDYYYY-ID">...</a>
#     <a href="https://youtu.be/..."><img alt="Videos" ...></a>
#
# NOTE: The search endpoint returns meetings whose meeting date falls within the
# requested range. Minutes for older meetings appear alongside their meeting row
# once uploaded. The default 3-day lookback covers recent meetings; use --days 7
# for a wider safety margin.
#
# NOTE: The server requires no authentication. A plain urllib request suffices.

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
BASE_URL = "https://www.waterfordct.org"
SEARCH_URL = f"{BASE_URL}/agendacenter/Search/"
OUTPUT_DIR = "beat-archive/waterford-agendas"
DAYS_BACK = 3
DAYS_AHEAD = 7
DELAY_SECONDS = 0.8

UA = "Waterford-CT-Agendas-Downloader/1.0 (journalism research)"

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
    """Download a ViewFile path to dest_path. Returns True on success."""
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


def is_specific_video(url):
    """True for watch?v= or youtu.be/ links (single downloadable video)."""
    lower = url.lower()
    return "youtu.be/" in lower or "youtube.com/watch" in lower


def download_video(video_url, dest_path):
    """Download a specific YouTube video via yt-dlp. Returns True on success."""
    cmd = [
        "yt-dlp", "--js-runtimes", "node",
        "--no-playlist",
        "-f", "bestvideo+bestaudio/best",
        "--merge-output-format", "mp4",
        "-o", dest_path,
        "--no-overwrites",
        "--quiet",
        "--no-warnings",
        video_url,
    ]
    try:
        subprocess.run(cmd, check=True)
        return True
    except FileNotFoundError:
        print("  ERROR: yt-dlp not found. Install with: pip install yt-dlp", file=sys.stderr)
        return False
    except subprocess.CalledProcessError as e:
        print(f"  WARNING: yt-dlp failed ({e})", file=sys.stderr)
        return False


def save_url_shortcut(video_url, dest_path):
    """Save a channel/stream URL as a .url Internet Shortcut file."""
    with open(dest_path, "w") as f:
        f.write(f"[InternetShortcut]\nURL={video_url}\n")
    return True


# --- HTML parser ---

class AgendaParser(html.parser.HTMLParser):
    """
    Single-pass parser for the Waterford CivicPlus Agenda Center search results.

    Tracks h2 (board name) and h3 (meeting date), collecting ViewFile/Agenda,
    ViewFile/Minutes, and YouTube video links between h3 boundaries.

    Video link detection accepts any youtube.com or youtu.be href so that
    both specific-video URLs (watch?v=) and channel/stream-page URLs
    (@TownofWaterfordCT-Stream/streams) are captured.
    """

    def __init__(self):
        super().__init__()
        self.items = []
        self._board = "Unknown Board"
        self._current_date = None
        self._agenda_url = None
        self._minutes_url = None
        self._video_url = None
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
            elif self._video_url is None and (
                "youtube.com" in lower or "youtu.be" in lower
            ):
                self._video_url = href

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
        if self._current_date and (
            self._agenda_url or self._minutes_url or self._video_url
        ):
            self.items.append({
                "board": self._board,
                "meeting_date": self._current_date,
                "agenda_url": self._agenda_url,
                "minutes_url": self._minutes_url,
                "video_url": self._video_url,
            })
        self._agenda_url = None
        self._minutes_url = None
        self._video_url = None

    def get_items(self):
        self._flush()
        return self.items


# --- File naming ---

def slugify(text, max_len=50):
    text = text.lower().strip()
    text = re.sub(r"[/\\&]", "-", text)
    text = re.sub(r"\s+-\s+", "-", text)
    text = re.sub(r"[^\w\s-]", "", text)
    text = re.sub(r"[\s_]+", "-", text)
    text = re.sub(r"-{2,}", "-", text)
    return text.strip("-")[:max_len]


def make_dest_path(board, doc_type, meeting_date, output_dir, ext=".pdf"):
    date_prefix = meeting_date.strftime("%Y-%m-%d")
    month_dir = os.path.join(output_dir, meeting_date.strftime("%Y-%m"))
    os.makedirs(month_dir, exist_ok=True)
    board_slug = slugify(board)
    return os.path.join(month_dir, f"{date_prefix}-{board_slug}-{doc_type}{ext}")


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
            "Download Waterford CT municipal agendas, minutes, and video recordings "
            "for meetings within the past N days (and up to M days ahead)."
        )
    )
    parser.add_argument(
        "--days", type=int, default=DAYS_BACK, metavar="N",
        help=f"Look back N days (default: {DAYS_BACK})",
    )
    parser.add_argument(
        "--ahead", type=int, default=DAYS_AHEAD, metavar="N",
        help=f"Also include meetings up to N days ahead (default: {DAYS_AHEAD})",
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
        "--no-minutes", action="store_true",
        help="Skip minutes, download agendas only",
    )
    parser.add_argument(
        "--no-agendas", action="store_true",
        help="Skip agendas, download minutes only",
    )
    parser.add_argument(
        "--include-video", action="store_true",
        help="Also download video recordings via yt-dlp (specific links) or .url shortcuts (channel links)",
    )
    parser.add_argument(
        "--video-only", action="store_true",
        help="Download only video recordings (skip documents)",
    )
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
    search_url = build_search_url(start_date, end_date)

    print(f"Date window : {start_date} to {end_date}")
    print(f"Search URL  : {search_url}")
    if not args.dry_run:
        print(f"Output dir  : {args.output_dir}")
    print()

    html_text = fetch_html(search_url)
    if not html_text:
        print("ERROR: Could not fetch the search results page.", file=sys.stderr)
        sys.exit(1)

    agenda_parser = AgendaParser()
    agenda_parser.feed(html_text)
    all_items = agenda_parser.get_items()

    if not all_items:
        print("WARNING: No agenda items found — the page structure may have changed.",
              file=sys.stderr)
        sys.exit(1)

    if args.board:
        filter_str = args.board.lower()
        all_items = [i for i in all_items if filter_str in i["board"].lower()]
        print(f"Filtered to {len(all_items)} item(s) matching '{args.board}'.")

    print(f"Found {len(all_items)} meeting(s) across "
          f"{len({i['board'] for i in all_items})} board(s).")
    print()

    # Expand each meeting into individual download tasks
    tasks = []
    for item in sorted(all_items, key=lambda x: x["meeting_date"], reverse=True):
        if do_docs:
            if item["agenda_url"] and not args.no_agendas:
                tasks.append({**item, "doc_type": "agenda", "href": item["agenda_url"],
                               "ext": ".pdf"})
            if item["minutes_url"] and not args.no_minutes:
                tasks.append({**item, "doc_type": "minutes", "href": item["minutes_url"],
                               "ext": ".pdf"})
        if do_video and item["video_url"]:
            if is_specific_video(item["video_url"]):
                tasks.append({**item, "doc_type": "video", "href": item["video_url"],
                               "ext": ".mp4"})
            else:
                # Channel/stream-page URL — save as Internet Shortcut
                tasks.append({**item, "doc_type": "video-link", "href": item["video_url"],
                               "ext": ".url"})
        elif item["video_url"] and not do_video:
            print(f"  VIDEO (not downloaded): {item['video_url']}")

    if not tasks:
        print("No downloadable items found within the date window.")
        return

    if args.dry_run:
        noun = "item(s)" if (do_docs and do_video) else (
            "recording(s)" if do_video else "document(s)"
        )
        print(f"{'Board':<45} {'Date':<12} Type")
        print("-" * 70)
        for t in tasks:
            extra = f"  {t['href'][:40]}..." if t["doc_type"] in ("video", "video-link") else ""
            print(f"{t['board'][:44]:<45} {t['meeting_date']!s:<12} {t['doc_type']}{extra}")
        print(f"\n{len(tasks)} {noun}. Re-run without --dry-run to download.")
        return

    os.makedirs(args.output_dir, exist_ok=True)
    log_path = os.path.join(args.output_dir, "download-log.txt")
    log_lines = []
    downloaded = skipped = failed = 0

    filename_counters: dict = {}

    for t in tasks:
        key = (slugify(t["board"]), t["meeting_date"], t["doc_type"])
        filename_counters[key] = filename_counters.get(key, 0) + 1
        count = filename_counters[key] - 1
        suffix = f"-{count}" if count > 0 else ""

        base = make_dest_path(t["board"], t["doc_type"], t["meeting_date"],
                              args.output_dir, t["ext"])
        if suffix:
            root, ext = os.path.splitext(base)
            dest = root + suffix + ext
        else:
            dest = base

        label = os.path.basename(dest)

        if os.path.exists(dest):
            print(f"  skip (exists)  {label}")
            skipped += 1
            continue

        print(f"  [{t['meeting_date']}] {t['board']} — {t['doc_type']}")
        print(f"  downloading    {label}")

        if t["doc_type"] == "video":
            ok = download_video(t["href"], dest)
        elif t["doc_type"] == "video-link":
            ok = save_url_shortcut(t["href"], dest)
        else:
            ok = download_pdf(t["href"], dest)

        if ok:
            downloaded += 1
            log_lines.append(f"{datetime.datetime.now().isoformat()}  OK       {dest}")
        else:
            failed += 1
            src = t["href"] if t["doc_type"] in ("video", "video-link") else (
                BASE_URL + t["href"] if t["href"].startswith("/") else t["href"]
            )
            log_lines.append(f"{datetime.datetime.now().isoformat()}  FAILED   {src}")
            if os.path.exists(dest) and t["doc_type"] != "video-link":
                os.remove(dest)

        time.sleep(DELAY_SECONDS)

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
#    python3 scripts/download-waterford-agendas.py --dry-run
#
# 2. Narrow to one board:
#    python3 scripts/download-waterford-agendas.py --board "Board of Selectmen"
#
# 3. Agendas only (skip minutes):
#    python3 scripts/download-waterford-agendas.py --no-minutes
#
# 4. Change the lookback window:
#    python3 scripts/download-waterford-agendas.py --days 7
#
# 5. Save files somewhere else:
#    python3 scripts/download-waterford-agendas.py --output-dir ~/Downloads/waterford
#
# 6. Download documents AND video recordings:
#    python3 scripts/download-waterford-agendas.py --include-video
#
# 7. Download only video recordings (skip PDFs):
#    python3 scripts/download-waterford-agendas.py --video-only
#
# 8. Preview video recordings without downloading:
#    python3 scripts/download-waterford-agendas.py --video-only --dry-run
#
# 9. Run on a schedule (cron — nightly at 8 PM):
#    0 20 * * * cd /path/to/repo && python3 scripts/download-waterford-agendas.py
#
# 10. Process downloaded files with Claude afterward:
#    python3 scripts/download-waterford-agendas.py && \
#    bash scripts/batch-process.sh beat-archive/waterford-agendas/
#
# VIDEO NOTE: Waterford posts two types of video links:
#   - Specific recordings: youtube.com/watch?v=... or youtu.be/... — downloaded
#     via yt-dlp as .mp4 files
#   - Channel stream page: youtube.com/@TownofWaterfordCT-Stream/streams — saved
#     as .url Internet Shortcut files (open in browser to view the stream archive)
#   The Board of Finance and RTM typically link the channel page; the Board of
#   Selectmen and others often link specific recordings.
#
# SITE NOTE: The Waterford Agenda Center does not expose document "posted" dates
# in its HTML. The script filters by meeting date. Use --days 7 to ensure recently
# posted minutes for meetings held earlier in the week are captured.
#
# BOARDS (40+ as of 2026):
#   Board of Education, Board of Finance, Board of Police Commissioners,
#   Board of Selectmen, Conservation Commission, Design Review Board,
#   Economic Development Commission, Flood & Erosion Control Board,
#   Harbor Management Commission, Oswegatchie Fire Station Building Committee,
#   Personnel Review Board, Planning & Zoning Commission,
#   Representative Town Meeting (RTM), Shellfish Commission, Utility Commission,
#   Waterford/East Lyme Shellfish Commission, Zoning Board of Appeals,
#   and others — see /agendacenter for the complete current list.
