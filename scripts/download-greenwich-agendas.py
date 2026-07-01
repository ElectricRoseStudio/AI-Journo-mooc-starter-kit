#!/usr/bin/env python3
# download-greenwich-agendas.py
# Download municipal meeting agendas, minutes, and video recordings from Greenwich CT
# for meetings whose date falls within the past N days (and up to 7 days ahead,
# to catch agendas posted early for upcoming meetings).
#
# USAGE:
#   python3 scripts/download-greenwich-agendas.py [options]
#
# REQUIREMENTS:
#   - Python 3.6+  (no third-party packages needed)
#   - Internet connection
#   - yt-dlp (for video: pip install yt-dlp or brew install yt-dlp)
#
# WHAT IT DOES:
#   1. Fetches the Greenwich CT Agenda Center page (all current-year data is inline)
#   2. Parses each board section and meeting row for board name, meeting date,
#      agenda URL, and minutes URL
#   3. Downloads PDFs whose meeting date falls within the date window to
#      beat-archive/greenwich-agendas/YYYY-MM/
#   4. Optionally scans Greenwich Community Television (GCTV) YouTube channel
#      and downloads meeting recordings in the date window
#   5. Appends a download log to beat-archive/greenwich-agendas/download-log.txt
#
# SITE STRUCTURE (CivicPlus CivicEngage):
#   Hub:     https://www.greenwichct.gov/AgendaCenter
#   Agenda:  https://www.greenwichct.gov/AgendaCenter/ViewFile/Agenda/_MMDDYYYY-ID
#   Minutes: https://www.greenwichct.gov/AgendaCenter/ViewFile/Minutes/_MMDDYYYY-ID
#
#   Page layout:
#     <h2>Board Name</h2>
#       year nav: javascript:changeYear(...)
#     <h3><strong>Month DD, YYYY</strong> — Posted Month DD, YYYY HH:MM AM/PM</h3>
#     <p><a href="/AgendaCenter/ViewFile/Agenda/_MMDDYYYY-ID">Title</a></p>
#     <p><a href="/AgendaCenter/ViewFile/Minutes/_MMDDYYYY-ID"><img...></a></p>  (if minutes)
#     <ol>
#       <li><a href="/AgendaCenter/ViewFile/Agenda/_MMDDYYYY-ID">Agenda</a></li>
#       ...
#     </ol>
#
# NOTE: The Agenda Center page embeds only the current year's meetings in its
# initial HTML. Older years are loaded dynamically via javascript:changeYear().
# This script covers only the initial page load — sufficient for a 30-day
# lookback but will not reach into prior calendar years.
#
# NOTE: The meeting date is the bolded date at the start of each h3 heading.
# The "Posted" date that follows the em-dash is the publication timestamp and
# is available but not used for filtering (use --days to control the meeting-date
# window instead).
#
# NOTE: The meeting date is also encoded in every ViewFile URL as _MMDDYYYY,
# used here as a fallback when the h3 text cannot be parsed.
#
# NOTE: The server requires no authentication. A plain urllib request with a
# browser-like User-Agent is sufficient.

import argparse
import datetime
import html.parser
import os

YT_DLP_NODE = "node:/home/richkirby/.nvm/versions/node/v20.20.2/bin/node"  # yt-dlp needs Node 20+; system node is 18
import re
import subprocess
import sys
import time
import urllib.error
import urllib.request

# --- Configuration ---
BASE_URL = "https://www.greenwichct.gov"
HUB_URL = f"{BASE_URL}/AgendaCenter"
OUTPUT_DIR = "beat-archive/greenwich-agendas"
DAYS_BACK = 4
DAYS_AHEAD = 7
DELAY_SECONDS = 0.8

YOUTUBE_CHANNEL = "https://www.youtube.com/channel/UCoiCrTH1tV16z2Ww-Tw3ohQ"

UA = "Greenwich-CT-Agendas-Downloader/1.0 (journalism research)"

_URL_DATE_RE = re.compile(r"_(\d{2})(\d{2})(\d{4})-\d+")

MONTHS = {
    "January": 1, "February": 2, "March": 3, "April": 4,
    "May": 5, "June": 6, "July": 7, "August": 8,
    "September": 9, "October": 10, "November": 11, "December": 12,
}

_VIDEO_DATE_RE = re.compile(
    r"\b(January|February|March|April|May|June|July|August|September|"
    r"October|November|December)\s+(\d{1,2}),?\s+(20\d{2})\b",
    re.I,
)


# --- HTML parser ---

class AgendaParser(html.parser.HTMLParser):
    """
    Single-pass parser for the Greenwich Agenda Center page.

    Tracks h2 tags (board name) and h3 tags (meeting date + posted date),
    collecting ViewFile/Agenda and ViewFile/Minutes links between h3 boundaries.

    Greenwich h3 format: "Month DD, YYYY — Posted Month DD, YYYY HH:MM AM/PM"
    The meeting date is the first date found; everything after "Posted" is ignored
    for filtering purposes.
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
        attrs = dict(attrs)

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
            href = attrs.get("href", "")
            if not href:
                return
            href_clean = href.split("?")[0]

            if "/AgendaCenter/ViewFile/Agenda/" in href_clean:
                if self._agenda_url is None:
                    self._agenda_url = href_clean
                    if self._current_date is None:
                        self._current_date = _date_from_url(href_clean)

            elif "/AgendaCenter/ViewFile/Minutes/" in href_clean:
                if self._minutes_url is None:
                    self._minutes_url = href_clean

    def handle_data(self, data):
        if self._in_h2 or self._in_h3:
            self._buf += data

    def handle_endtag(self, tag):
        if tag == "h2" and self._in_h2:
            self._in_h2 = False
            text = " ".join(self._buf.split()).strip()
            if text:
                self._board = text
            self._buf = ""

        elif tag == "h3" and self._in_h3:
            self._in_h3 = False
            text = " ".join(self._buf.split()).strip()
            parsed = _parse_meeting_date(text)
            if parsed:
                self._current_date = parsed
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


# --- Helpers ---

def _parse_meeting_date(text):
    """
    Extract the meeting date from an h3 heading.

    Greenwich format: "Month DD, YYYY — Posted Month DD, YYYY HH:MM AM/PM"
    The meeting date is the first date in the string; anything after "Posted"
    or "—" is the publication timestamp and is discarded.
    """
    # Strip the posted-date portion so we only parse the meeting date
    date_part = re.split(r"\s*[—–-]\s*[Pp]osted|\s+[Pp]osted\s+", text, maxsplit=1)[0]
    date_part = " ".join(date_part.split()).strip()

    m = re.search(r"([A-Za-z]+)\s+(\d{1,2}),?\s+(\d{4})", date_part)
    if not m:
        return None

    month_str = m.group(1)
    day_str = f"{int(m.group(2)):02d}"
    year_str = m.group(3)

    for fmt in ("%B %d %Y", "%b %d %Y"):
        try:
            return datetime.datetime.strptime(f"{month_str} {day_str} {year_str}", fmt).date()
        except ValueError:
            continue
    return None


def _date_from_url(path):
    """Extract meeting date from /AgendaCenter/ViewFile/.../_MMDDYYYY-ID URLs."""
    m = _URL_DATE_RE.search(path)
    if not m:
        return None
    mm, dd, yyyy = int(m.group(1)), int(m.group(2)), int(m.group(3))
    try:
        return datetime.date(yyyy, mm, dd)
    except ValueError:
        return None


def fetch_html(url):
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "text/html"})
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
    """Download a ViewFile PDF to dest_path. Returns True on success."""
    url = BASE_URL + path if path.startswith("/") else path
    req = urllib.request.Request(
        url,
        headers={"User-Agent": UA, "Accept": "application/pdf, */*"},
    )
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            with open(dest_path, "wb") as f:
                f.write(r.read())
        return True
    except Exception as e:
        print(f"  WARNING: {e}", file=sys.stderr)
        return False


def slugify(text, max_len=50):
    text = text.lower().strip()
    text = re.sub(r"[/\\&]", "-", text)
    text = re.sub(r"\s+-\s+", "-", text)
    text = re.sub(r"[^\w\s-]", "", text)
    text = re.sub(r"[\s_]+", "-", text)
    text = re.sub(r"-{2,}", "-", text)
    return text.strip("-")[:max_len]


def _safe_date(year, month, day):
    try:
        return datetime.date(year, month, day)
    except ValueError:
        return None


def parse_video_date(title):
    """Extract meeting date from a GCTV title like 'Board of Selectmen, May 14, 2026'."""
    m = _VIDEO_DATE_RE.search(title)
    if not m:
        return None
    month_name, day_str, year_str = m.group(1), m.group(2), m.group(3)
    try:
        return _safe_date(int(year_str), MONTHS[month_name.capitalize()], int(day_str))
    except (KeyError, ValueError):
        return None


def list_channel_videos(channel_url):
    """Return [(vid_id, vdate, title), ...] for all datable GCTV videos."""
    cmd = [
        "yt-dlp", "--js-runtimes", YT_DLP_NODE, "--flat-playlist", "--print", "%(id)s\t%(title)s",
        "--no-warnings", channel_url,
    ]
    try:
        out = subprocess.check_output(cmd, text=True, stderr=subprocess.DEVNULL)
    except FileNotFoundError:
        print("  ERROR: yt-dlp not found. Install with: pip install yt-dlp", file=sys.stderr)
        return []
    except subprocess.CalledProcessError:
        return []
    videos = []
    for line in out.splitlines():
        if "\t" not in line:
            continue
        vid_id, title = line.split("\t", 1)
        vdate = parse_video_date(title)
        if vdate:
            videos.append((vid_id, vdate, title))
    return videos


def download_video(vid_id, dest_template, dry_run=False):
    url = f"https://www.youtube.com/watch?v={vid_id}"
    if dry_run:
        return True
    cmd = [
        "yt-dlp", "--js-runtimes", YT_DLP_NODE, "--no-playlist",
        "-f", "bestvideo+bestaudio/best",
        "--merge-output-format", "mp4",
        "-o", dest_template,
        "--no-overwrites", "--quiet", "--no-warnings", url,
    ]
    try:
        subprocess.run(cmd, check=True)
        return True
    except (subprocess.CalledProcessError, FileNotFoundError) as e:
        print(f"  WARNING: {e}", file=sys.stderr)
        return False


def make_dest_path(board, doc_type, meeting_date, output_dir):
    date_prefix = meeting_date.strftime("%Y-%m-%d")
    month_dir = os.path.join(output_dir, meeting_date.strftime("%Y-%m"))
    os.makedirs(month_dir, exist_ok=True)
    board_slug = slugify(board)
    return os.path.join(month_dir, f"{date_prefix}-{board_slug}-{doc_type}.pdf")


# --- Main ---

def main():
    parser = argparse.ArgumentParser(
        description=(
            "Download Greenwich CT municipal agendas and minutes "
            "for meetings within the past N days."
        )
    )
    parser.add_argument(
        "--days", type=int, default=DAYS_BACK, metavar="N",
        help=f"Look back N days by meeting date (default: {DAYS_BACK})",
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
        help="Only include boards whose name contains NAME (case-insensitive)",
    )
    parser.add_argument(
        "--no-minutes", action="store_true",
        help="Skip minutes, download agendas only",
    )
    parser.add_argument(
        "--no-agendas", action="store_true",
        help="Skip agendas, download minutes only",
    )

    vid_group = parser.add_mutually_exclusive_group()
    vid_group.add_argument(
        "--include-video", action="store_true",
        help="Also download GCTV meeting videos (docs + video)",
    )
    vid_group.add_argument(
        "--video-only", action="store_true",
        help="Download GCTV videos only, skip PDFs",
    )
    vid_group.add_argument(
        "--docs-only", action="store_true",
        help="Download PDFs only, skip video (default behavior)",
    )
    args = parser.parse_args()

    now = datetime.datetime.now()
    if (now.weekday() == 5 and now.hour >= 18) or (now.weekday() == 6 and now.hour < 12):  # Saturday night, Sunday morning
        print("Skipping — no downloads on Saturday nights or Sunday mornings.")
        sys.exit(0)

    do_docs = not args.video_only
    do_video = args.include_video or args.video_only

    today = datetime.date.today()
    cutoff = today - datetime.timedelta(days=args.days)
    future_limit = today + datetime.timedelta(days=args.ahead)

    print(f"Date window : {cutoff} to {future_limit}")
    if do_docs:
        print(f"Hub page    : {HUB_URL}")
    if do_video:
        print(f"GCTV channel: {YOUTUBE_CHANNEL}")
    if not args.dry_run:
        print(f"Output dir  : {args.output_dir}")
    print()

    # --- Step 1: fetch and parse PDF documents ---
    docs = []
    if do_docs:
        print("Fetching Agenda Center page...")
        html_content = fetch_html(HUB_URL)
        if not html_content:
            print("ERROR: Could not fetch the Agenda Center page.", file=sys.stderr)
            sys.exit(1)

        agenda_parser = AgendaParser()
        agenda_parser.feed(html_content)
        all_items = agenda_parser.get_items()

        if not all_items:
            print(
                "WARNING: No meeting items found — the page structure may have changed.",
                file=sys.stderr,
            )
        else:
            print(f"  Parsed {len(all_items)} meeting row(s) across all boards.")

            if args.board:
                filter_str = args.board.lower()
                all_items = [i for i in all_items if filter_str in i["board"].lower()]
                print(f"  Filtered to {len(all_items)} row(s) matching '{args.board}'.")

            in_window = [
                i for i in all_items
                if cutoff <= i["meeting_date"] <= future_limit
            ]
            in_window.sort(key=lambda x: (x["meeting_date"], x["board"]), reverse=True)

            for item in in_window:
                if not args.no_agendas and item["agenda_url"]:
                    docs.append({**item, "doc_type": "agenda", "url": item["agenda_url"]})
                if not args.no_minutes and item["minutes_url"]:
                    docs.append({**item, "doc_type": "minutes", "url": item["minutes_url"]})

            print(
                f"  Found {len(docs)} document(s) across "
                f"{len({i['board'] for i in in_window})} board(s) in date window."
            )
        print()

    # --- Step 2: scan GCTV YouTube channel ---
    videos = []
    if do_video:
        print("Scanning GCTV YouTube channel...")
        all_videos = list_channel_videos(YOUTUBE_CHANNEL)
        print(f"  {len(all_videos)} datable video(s) found on channel.")

        board_filter = args.board.lower() if args.board else None
        for vid_id, vdate, title in all_videos:
            if not (cutoff <= vdate <= future_limit):
                continue
            if board_filter and board_filter not in title.lower():
                continue
            videos.append({"vid_id": vid_id, "vdate": vdate, "title": title})

        print(f"  {len(videos)} video(s) match the date window.")
        print()

    if not docs and not videos:
        print("No documents or videos found within the date window.")
        return

    if args.dry_run:
        if docs:
            print(f"{'Board':<50} {'Date':<12} Type")
            print("-" * 70)
            for d in docs:
                print(f"{d['board'][:49]:<50} {d['meeting_date']!s:<12} {d['doc_type']}")
            print(f"\n{len(docs)} document(s).")
        if videos:
            if docs:
                print()
            print(f"{'Title':<60} {'Date':<12}")
            print("-" * 74)
            for v in sorted(videos, key=lambda x: x["vdate"], reverse=True):
                print(f"{v['title'][:59]:<60} {v['vdate']!s:<12}")
            print(f"\n{len(videos)} video(s).")
        print("Re-run without --dry-run to download.")
        return

    # --- Step 3: download PDFs ---
    os.makedirs(args.output_dir, exist_ok=True)
    log_path = os.path.join(args.output_dir, "download-log.txt")
    log_lines = []
    downloaded = skipped = failed = 0

    for d in docs:
        dest = make_dest_path(d["board"], d["doc_type"], d["meeting_date"], args.output_dir)
        label = os.path.basename(dest)

        if os.path.exists(dest):
            print(f"  skip (exists)  {label}")
            skipped += 1
            continue

        print(f"  [{d['meeting_date']}] {d['board']} — {d['doc_type']}")
        print(f"  downloading    {label}")

        if download_pdf(d["url"], dest):
            downloaded += 1
            log_lines.append(f"{datetime.datetime.now().isoformat()}  OK       {dest}")
        else:
            failed += 1
            log_lines.append(
                f"{datetime.datetime.now().isoformat()}  FAILED   {BASE_URL + d['url']}"
            )
            if os.path.exists(dest):
                os.remove(dest)

        time.sleep(DELAY_SECONDS)

    # --- Step 4: download videos ---
    vid_downloaded = vid_skipped = vid_failed = 0
    if videos:
        if docs:
            print()
        print("Downloading videos...")
        for v in sorted(videos, key=lambda x: x["vdate"], reverse=True):
            title_slug_raw = _VIDEO_DATE_RE.sub("", v["title"]).rstrip(", ").strip()
            title_slug = slugify(title_slug_raw)
            date_str = v["vdate"].strftime("%Y-%m-%d")
            month_dir = os.path.join(args.output_dir, v["vdate"].strftime("%Y-%m"))
            os.makedirs(month_dir, exist_ok=True)
            dest_template = os.path.join(month_dir, f"{date_str}-{title_slug}.%(ext)s")
            dest_mp4 = os.path.join(month_dir, f"{date_str}-{title_slug}.mp4")

            if os.path.exists(dest_mp4):
                print(f"  skip (exists)  {os.path.basename(dest_mp4)}")
                vid_skipped += 1
                continue

            print(f"  [{v['vdate']}] {v['title']}")
            print(f"  source URL:    https://www.youtube.com/watch?v={v['vid_id']}")
            if download_video(v["vid_id"], dest_template):
                vid_downloaded += 1
                log_lines.append(f"{datetime.datetime.now().isoformat()}  OK       {dest_mp4}")
            else:
                vid_failed += 1
                log_lines.append(
                    f"{datetime.datetime.now().isoformat()}  FAILED   "
                    f"https://www.youtube.com/watch?v={v['vid_id']}"
                )

    if log_lines:
        with open(log_path, "a") as f:
            f.write("\n".join(log_lines) + "\n")

    print()
    if do_docs:
        print(f"PDFs  — downloaded: {downloaded}  skipped: {skipped}  failed: {failed}")
    if do_video:
        print(f"Video — downloaded: {vid_downloaded}  skipped: {vid_skipped}  failed: {vid_failed}")
    if downloaded + skipped + vid_downloaded + vid_skipped:
        print(f"Files in: {args.output_dir}")
    if log_lines:
        print(f"Log:      {log_path}")


if __name__ == "__main__":
    main()


# --- Tips ---
#
# 1. Preview without downloading:
#    python3 scripts/download-greenwich-agendas.py --dry-run
#
# 2. Docs + GCTV video recordings:
#    python3 scripts/download-greenwich-agendas.py --include-video
#
# 3. Video only (skip PDFs):
#    python3 scripts/download-greenwich-agendas.py --video-only
#
# 4. Narrow to one board (applies to both docs and video):
#    python3 scripts/download-greenwich-agendas.py --board "Planning & Zoning"
#
# 5. Agendas only (skip minutes):
#    python3 scripts/download-greenwich-agendas.py --no-minutes
#
# 6. Change the lookback window:
#    python3 scripts/download-greenwich-agendas.py --days 7
#
# 7. Save files somewhere else:
#    python3 scripts/download-greenwich-agendas.py --output-dir ~/Downloads/greenwich
#
# 8. Run on a schedule (cron — 8 AM daily):
#    0 8 * * * cd /path/to/repo && python3 scripts/download-greenwich-agendas.py
#
# 9. Process downloaded files with Claude afterward:
#    python3 scripts/download-greenwich-agendas.py && bash scripts/batch-process.sh beat-archive/greenwich-agendas/
#
# NOTE: The --ahead flag (default: 7 days) captures agendas for upcoming
# meetings already published. Run daily to stay current.
#
# NOTE: The Agenda Center page embeds only the current year's meetings in its
# initial HTML. Older years load dynamically via JavaScript. This script covers
# only what is in the initial page load, which is sufficient for a 30-day
# lookback but will not reach back into prior calendar years.
#
# NOTE: Greenwich offers HTML and packet variants of agendas via ?html=true and
# ?packet=true query parameters. This script downloads only the default PDF.
# To get the compiled packet instead, change the download URL to append ?packet=true.
#
# NOTE: GCTV videos use the title format "Board Name, Month DD, YYYY". Videos
# without a date in the title (live stream placeholders, announcements) are
# automatically skipped. The channel has 1,100+ videos; the flat-playlist scan
# takes about 2-3 seconds and parses dates from titles rather than metadata.
