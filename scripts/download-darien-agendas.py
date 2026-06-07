#!/usr/bin/env python3
# download-darien-agendas.py
# Download municipal meeting agendas, minutes, and video recordings from the
# Darien CT Agenda Center for meetings within the past N days (and up to 7
# days ahead, to catch agendas posted early for upcoming meetings).
#
# USAGE:
#   python3 scripts/download-darien-agendas.py [options]
#
# REQUIREMENTS:
#   - Python 3.6+  (no third-party packages needed for docs)
#   - yt-dlp       (for video: pip install yt-dlp or brew install yt-dlp)
#   - Internet connection
#
# WHAT IT DOES:
#   Documents (default or --docs-only):
#     1. Fetches the Darien CT Agenda Center listing page (all current-year data is inline)
#     2. Parses each board section and meeting row for board name, date, agenda URL,
#        and minutes URL
#     3. Downloads PDFs whose meeting date falls within the date window to
#        beat-archive/darien-agendas/YYYY-MM/
#     4. Appends a download log to beat-archive/darien-agendas/download-log.txt
#
#   Video (--include-video or --video-only):
#     5. Extracts DarienTV video links from each meeting row (Vimeo-backed)
#     6. Downloads recordings with yt-dlp using Vimeo URL + darientv.com referer
#
# SITE STRUCTURE (CivicPlus CivicEngage):
#   Hub:     https://www.darienct.gov/AgendaCenter
#   Agenda:  https://www.darienct.gov/AgendaCenter/ViewFile/Agenda/_MMDDYYYY-ID
#   Minutes: https://www.darienct.gov/AgendaCenter/ViewFile/Minutes/_MMDDYYYY-ID
#   Video:   https://darientv.com/?vimeography_gallery=N&vimeography_video=VIMEO_ID
#
#   Page layout per meeting row:
#     <h3><strong>Mon DD, YYYY</strong></h3>
#     <a href="/AgendaCenter/ViewFile/Agenda/_MMDDYYYY-ID">...</a>
#     <td class="minutes"><a href="/AgendaCenter/ViewFile/Minutes/..."></td>
#     <td class="media"><a href="https://darientv.com/?...vimeography_video=VIMEO_ID"></td>
#
# NOTE: DarienTV videos are hosted on Vimeo but set as embed-only on the
# Vimeo platform. yt-dlp must be called with https://vimeo.com/{ID} as the URL
# and the darientv.com page as the --referer to authenticate the embed context.
#
# NOTE: The Agenda Center page embeds only the current year's meetings in its
# initial HTML. Older years are loaded dynamically via javascript:changeYear().
# This script covers only what is in the initial page load — typically the full
# current calendar year plus whatever is still visible for the prior year.
# For a standard 30-day lookback window this is always sufficient.
#
# NOTE: The meeting date is encoded in every ViewFile URL as _MMDDYYYY, which
# this script uses as a fallback when the <h3> date text cannot be parsed.
#
# NOTE: The server requires no authentication. A plain urllib request with a
# browser-like User-Agent is sufficient.

import argparse
import datetime
import html.parser
import os
import re
import subprocess
import sys
import time
import urllib.error
import urllib.request

# --- Configuration ---
BASE_URL = "https://www.darienct.gov"
HUB_URL = f"{BASE_URL}/AgendaCenter"
OUTPUT_DIR = "beat-archive/darien-agendas"
DAYS_BACK = 4
DAYS_AHEAD = 7
DELAY_SECONDS = 0.8

UA = "Darien-CT-Agendas-Downloader/1.0 (journalism research)"

_URL_DATE_RE = re.compile(r"_(\d{2})(\d{2})(\d{4})-\d+")


# --- HTML parser ---

class AgendaParser(html.parser.HTMLParser):
    """
    Single-pass parser for the Darien Agenda Center page.

    Tracks h2 tags (board name) and h3 tags (meeting date), collecting
    ViewFile/Agenda, ViewFile/Minutes, and darientv.com video links between
    h3 boundaries.
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

            elif "darientv.com" in href and self._video_url is None:
                self._video_url = href

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
            parsed = _parse_date_text(text)
            if parsed:
                self._current_date = parsed
            self._buf = ""

    def _flush(self):
        if self._current_date and (self._agenda_url or self._minutes_url or self._video_url):
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


# --- Helpers ---

def _parse_date_text(text):
    """Parse 'Mon DD, YYYY' (e.g. 'Mar 10, 2026') from an h3 heading."""
    m = re.search(r"([A-Za-z]{3})\s+(\d{1,2}),?\s+(\d{4})", text)
    if not m:
        return None
    try:
        return datetime.datetime.strptime(
            f"{m.group(1)} {int(m.group(2)):02d} {m.group(3)}", "%b %d %Y"
        ).date()
    except ValueError:
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


_VIMEO_ID_RE = re.compile(r"vimeography_video=(\d+)")


def _vimeo_id_from_url(darientv_url):
    """Extract Vimeo video ID from a darientv.com gallery URL."""
    m = _VIMEO_ID_RE.search(darientv_url)
    return m.group(1) if m else None


def download_video(darientv_url, dest_path, dry_run=False):
    """
    Download a DarienTV video via yt-dlp.

    DarienTV videos are on Vimeo as embed-only. yt-dlp must be called with
    the vimeo.com URL and the darientv.com page as --referer.
    Returns True on success.
    """
    vimeo_id = _vimeo_id_from_url(darientv_url)
    if not vimeo_id:
        print(f"  WARNING: could not extract Vimeo ID from {darientv_url}", file=sys.stderr)
        return False
    if dry_run:
        return True
    vimeo_url = f"https://vimeo.com/{vimeo_id}"
    cmd = [
        "yt-dlp",
        "--no-playlist",
        "-f", "bestvideo+bestaudio/best",
        "--merge-output-format", "mp4",
        "--referer", darientv_url,
        "-o", dest_path,
        "--no-overwrites",
        "--quiet",
        "--no-warnings",
        vimeo_url,
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

def main():
    parser = argparse.ArgumentParser(
        description=(
            "Download Darien CT municipal agendas and minutes "
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
    parser.add_argument(
        "--include-video", action="store_true",
        help="Also download video recordings via yt-dlp",
    )
    parser.add_argument(
        "--video-only", action="store_true",
        help="Download only video recordings (skip documents)",
    )
    args = parser.parse_args()

    if datetime.date.today().weekday() in (6, 0):  # Sunday, Monday
        print("Skipping — no downloads on Sunday or Monday.")
        sys.exit(0)

    do_docs  = not args.video_only
    do_video = args.include_video or args.video_only

    today = datetime.date.today()
    cutoff = today - datetime.timedelta(days=args.days)
    future_limit = today + datetime.timedelta(days=args.ahead)

    print(f"Date window : {cutoff} to {future_limit}")
    print(f"Hub page    : {HUB_URL}")
    if not args.dry_run:
        print(f"Output dir  : {args.output_dir}")
    print()

    # --- Step 1: fetch and parse the hub page ---
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
        sys.exit(1)

    print(f"  Parsed {len(all_items)} meeting row(s) across all boards.")

    # --- Step 2: filter ---
    if args.board:
        filter_str = args.board.lower()
        all_items = [i for i in all_items if filter_str in i["board"].lower()]
        print(f"  Filtered to {len(all_items)} row(s) matching '{args.board}'.")

    in_window = [
        i for i in all_items
        if cutoff <= i["meeting_date"] <= future_limit
    ]
    in_window.sort(key=lambda x: (x["meeting_date"], x["board"]), reverse=True)

    # Expand to one entry per doc/video per row
    docs = []
    for item in in_window:
        if do_docs:
            if not args.no_agendas and item["agenda_url"]:
                docs.append({**item, "doc_type": "agenda", "url": item["agenda_url"]})
            if not args.no_minutes and item["minutes_url"]:
                docs.append({**item, "doc_type": "minutes", "url": item["minutes_url"]})
        if do_video and item["video_url"]:
            docs.append({**item, "doc_type": "video", "url": item["video_url"]})

    n_docs  = sum(1 for d in docs if d["doc_type"] != "video")
    n_vids  = sum(1 for d in docs if d["doc_type"] == "video")
    n_boards = len({i["board"] for i in in_window})
    parts = []
    if n_docs:
        parts.append(f"{n_docs} document(s)")
    if n_vids:
        parts.append(f"{n_vids} recording(s)")
    print(f"  Found {', '.join(parts) or '0 items'} across {n_boards} board(s) in date window.")
    print()

    if not docs:
        print("No documents found within the date window.")
        return

    if args.dry_run:
        print(f"{'Board':<45} {'Date':<12} Type")
        print("-" * 70)
        for d in docs:
            extra = f"  {d['url'][:40]}..." if d["doc_type"] == "video" else ""
            print(f"{d['board'][:44]:<45} {d['meeting_date']!s:<12} {d['doc_type']}{extra}")
        noun = "item(s)" if (do_docs and do_video) else ("recording(s)" if do_video else "document(s)")
        print(f"\n{len(docs)} {noun}. Re-run without --dry-run to download.")
        return

    # --- Step 3: download ---
    os.makedirs(args.output_dir, exist_ok=True)
    log_path = os.path.join(args.output_dir, "download-log.txt")
    log_lines = []
    downloaded = skipped = failed = 0

    for d in docs:
        ext = ".mp4" if d["doc_type"] == "video" else ".pdf"
        dest = make_dest_path(d["board"], d["doc_type"], d["meeting_date"], args.output_dir, ext)
        label = os.path.basename(dest)

        if os.path.exists(dest):
            print(f"  skip (exists)  {label}")
            skipped += 1
            continue

        print(f"  [{d['meeting_date']}] {d['board']} — {d['doc_type']}")
        print(f"  downloading    {label}")

        if d["doc_type"] == "video":
            ok = download_video(d["url"], dest)
        else:
            ok = download_pdf(d["url"], dest)

        if ok:
            downloaded += 1
            log_lines.append(f"{datetime.datetime.now().isoformat()}  OK       {dest}")
        else:
            failed += 1
            src = d["url"] if d["doc_type"] == "video" else BASE_URL + d["url"]
            log_lines.append(f"{datetime.datetime.now().isoformat()}  FAILED   {src}")
            if os.path.exists(dest):
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
#    python3 scripts/download-darien-agendas.py --dry-run
#
# 2. Narrow to one board:
#    python3 scripts/download-darien-agendas.py --board "Board of Selectmen"
#
# 3. Agendas only (skip minutes):
#    python3 scripts/download-darien-agendas.py --no-minutes
#
# 4. Change the lookback window:
#    python3 scripts/download-darien-agendas.py --days 7
#
# 5. Save files somewhere else:
#    python3 scripts/download-darien-agendas.py --output-dir ~/Downloads/darien
#
# 6. Download documents AND video recordings:
#    python3 scripts/download-darien-agendas.py --include-video
#
# 7. Download only video recordings (skip PDFs):
#    python3 scripts/download-darien-agendas.py --video-only
#
# 8. Preview video recordings without downloading:
#    python3 scripts/download-darien-agendas.py --video-only --dry-run
#
# 9. Run on a schedule (cron — 8 AM daily):
#    0 8 * * * cd /path/to/repo && python3 scripts/download-darien-agendas.py
#
# 10. Process downloaded files with Claude afterward:
#    python3 scripts/download-darien-agendas.py && bash scripts/batch-process.sh beat-archive/darien-agendas/
#
# BOARDS (as of 2026):
#   Advisory Board of Health, Architectural Review Board, Board of Finance,
#   Board of Selectmen, Conservation Commission, Environmental Protection
#   Commission, Housing Authority, Parks & Recreation Commission, Planning &
#   Zoning Commission, Representative Town Meeting, Zoning Board of Appeals,
#   and RTM standing committees (Education, Finance & Budget, etc.)
#
# NOTE: The --ahead flag (default: 7 days) captures agendas for upcoming
# meetings already published. Run daily to stay current.
#
# NOTE: The Agenda Center page embeds only the current year's meetings in its
# initial HTML. Older years load dynamically via JavaScript. This script covers
# only what is in the initial page load, which is sufficient for a 30-day
# lookback but will not reach back into prior years.
