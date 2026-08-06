#!/usr/bin/env python3
# download-malden-agendas.py
# Download municipal meeting agendas, minutes, and video recordings from the
# Malden MA Agenda Center for meetings within the past N days (and up to 7
# days ahead, to catch agendas posted early for upcoming meetings).
#
# USAGE:
#   python3 scripts/download-malden-agendas.py [options]
#
# REQUIREMENTS:
#   - Python 3.6+  (no third-party packages needed for docs)
#   - yt-dlp       (for YouTube links only: pip install yt-dlp)
#   - Internet connection
#
# WHAT IT DOES:
#   Documents (default or --docs-only):
#     1. Fetches the Malden MA Agenda Center search endpoint with a date
#        range spanning DAYS_BACK days ago through DAYS_AHEAD days ahead
#        (filtered by meeting date — the site does not expose posted dates
#        in the search parameters, only in each row's display text)
#     2. Parses each board section and meeting row for board name, meeting
#        date, agenda URL, minutes URL, and video URL (if any)
#     3. Downloads PDFs to beat-archive/malden-agendas/YYYY-MM/
#     4. Appends a download log to beat-archive/malden-agendas/download-log.txt
#
#   Video (--include-video or --video-only):
#     5. Extracts each meeting's single video link, if any, from the row's
#        <td class="media"> cell
#     6. YouTube links (youtube.com/watch, youtube.com/live, youtu.be) are
#        downloaded with yt-dlp
#     7. Everything else — SharePoint Stream share links
#        (cityofmalden-my.sharepoint.com), Zoom cloud-recording share links
#        (cityofmalden.zoom.us/rec/share/...), Microsoft Teams links, and
#        tinyurl.com redirects — is saved as a .url Internet Shortcut file
#        instead of downloaded. Confirmed directly: yt-dlp cannot extract
#        from either a live Zoom share URL (returns "'NoneType' object is
#        not subscriptable") or a SharePoint personal-tenant Stream share
#        URL ("Unsupported URL") — both require an authenticated session
#        yt-dlp has no way to establish, so a link is the only thing that
#        can honestly be delivered for these.
#
# SITE STRUCTURE (CivicPlus CivicEngage "Agenda Center"):
#   Hub:     https://www.cityofmalden.org/AgendaCenter
#   Search:  https://www.cityofmalden.org/AgendaCenter/Search/?term=&CIDs=all
#              &startDate=MM/DD/YYYY&endDate=MM/DD/YYYY&dateRange=Custom&dateSelector=0
#   Agenda:  https://www.cityofmalden.org/AgendaCenter/ViewFile/Agenda/_MMDDYYYY-ID
#   Minutes: https://www.cityofmalden.org/AgendaCenter/ViewFile/Minutes/_MMDDYYYY-ID
#   Video:   one link per meeting (if any) in <td class="media">, one of:
#              https://www.youtube.com/watch?v=... or /live/...  (downloadable)
#              https://youtu.be/...                              (downloadable)
#              https://cityofmalden-my.sharepoint.com/:v:/...     (link only)
#              https://cityofmalden.zoom.us/rec/share/...         (link only)
#              https://teams.microsoft.com/...                    (link only)
#              https://tinyurl.com/...  (unresolved redirect)      (link only)
#
#   Search result page layout per board section:
#     <h2 ...>Board Name</h2>
#     <h3 ...><strong>Mon DD, YYYY</strong> — Posted Mon DD, YYYY H:MM AM</h3>
#     <a href="/AgendaCenter/ViewFile/Agenda/_MMDDYYYY-ID">...</a>
#     <td class="minutes"><a href="/AgendaCenter/ViewFile/Minutes/_MMDDYYYY-ID">...</a></td>
#     <td class="media"><a href="{video URL}">...</a></td>
#   The CIDs=all query returns only boards with at least one item in the
#   date window — no separate full board list is needed to drive the scan.
#
# NOTE: The search endpoint returns meetings whose meeting date falls within
# the requested range. Minutes for older meetings appear alongside their
# meeting row once uploaded, same as the agenda.
#
# NOTE: The server requires no authentication for document downloads. A
# plain urllib request suffices (unlike Arlington's PrimeGov portal, which
# needs a real browser session — Malden's AgendaCenter serves PDFs directly
# with Content-Disposition: inline, confirmed via `curl -I`).
#
# COVERAGE: School Committee (Malden's name for what's elsewhere called
# Board of Education) and Open Space & Recreation Committee (Malden's Parks
# & Recreation equivalent — there's no board with "Parks" or "Recreation"
# alone in its name) are both already on the municipal Agenda Center, same
# as every other board — no separate video-archive hunt was needed here,
# unlike Arlington's ACMi situation.

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

YT_DLP_NODE = "node:/home/richkirby/.local/bin/yt-dlp-node"  # yt-dlp needs Node 22+; symlink kept current by scripts/update-yt-dlp-node.sh

# --- Configuration ---
BASE_URL = "https://www.cityofmalden.org"
SEARCH_URL = f"{BASE_URL}/AgendaCenter/Search/"
OUTPUT_DIR = "beat-archive/malden-agendas"
DAYS_BACK = 4
DAYS_AHEAD = 7
DELAY_SECONDS = 0.8

UA = "Malden-MA-Agendas-Downloader/1.0 (journalism research)"

_H3_DATE_RE = re.compile(
    r"\b(Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)\s+(\d{1,2}),\s+(\d{4})\b"
)

_MONTH_ABBR = {
    "Jan": 1, "Feb": 2, "Mar": 3, "Apr": 4, "May": 5, "Jun": 6,
    "Jul": 7, "Aug": 8, "Sep": 9, "Oct": 10, "Nov": 11, "Dec": 12,
}

_VIDEO_HOST_RE = re.compile(
    r"https?://(?:www\.)?(youtube\.com|youtu\.be|[\w.-]*sharepoint\.com|"
    r"[\w.-]*zoom\.us|teams\.microsoft\.com|tinyurl\.com)/",
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
    """True for youtube.com/watch, youtube.com/live, or youtu.be/ links (downloadable)."""
    lower = url.lower()
    return (
        "youtu.be/" in lower
        or "youtube.com/watch" in lower
        or "youtube.com/live/" in lower
    )


def download_video(video_url, dest_path):
    """Download a specific YouTube video via yt-dlp. Returns True on success."""
    cmd = [
        "yt-dlp", "--js-runtimes", YT_DLP_NODE,
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
        subprocess.run(cmd, check=True, timeout=3600)
        return True
    except FileNotFoundError:
        print("  ERROR: yt-dlp not found. Install with: pip install yt-dlp", file=sys.stderr)
        return False
    except subprocess.CalledProcessError as e:
        print(f"  WARNING: yt-dlp failed ({e})", file=sys.stderr)
        return False
    except subprocess.TimeoutExpired:
        print(f"  WARNING: yt-dlp timed out downloading {video_url}", file=sys.stderr)
        return False


def save_url_shortcut(video_url, dest_path):
    """Save a non-YouTube video link as a .url Internet Shortcut file."""
    with open(dest_path, "w") as f:
        f.write(f"[InternetShortcut]\nURL={video_url}\n")
    return True


# --- HTML parser ---

class AgendaParser(html.parser.HTMLParser):
    """
    Single-pass parser for the Malden CivicPlus Agenda Center search results.

    Tracks h2 (board name) and h3 (meeting date), collecting ViewFile/Agenda,
    ViewFile/Minutes, and video links between h3 boundaries. "Previous
    Versions" links (/AgendaCenter/PreviousVersions/...) are deliberately
    not matched — they point to a version-history page, not a PDF.

    Video link detection is restricted to known video-hosting domains
    (YouTube, SharePoint, Zoom, Teams, tinyurl) rather than any href, to
    avoid false positives from unrelated per-row links.
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
            elif self._video_url is None and _VIDEO_HOST_RE.search(href):
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
            "Download Malden MA municipal agendas, minutes, and video recordings "
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
        help="Also download video recordings via yt-dlp (YouTube links) or .url shortcuts (everything else)",
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
        print("No agenda items found in the date window.")
        return

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
                # SharePoint/Zoom/Teams/tinyurl — auth-gated or unresolved,
                # not extractable by yt-dlp; save as Internet Shortcut.
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
#    python3 scripts/download-malden-agendas.py --dry-run
#
# 2. Narrow to one board:
#    python3 scripts/download-malden-agendas.py --board "School Committee"
#
# 3. Agendas only (skip minutes):
#    python3 scripts/download-malden-agendas.py --no-minutes
#
# 4. Change the lookback window:
#    python3 scripts/download-malden-agendas.py --days 7
#
# 5. Save files somewhere else:
#    python3 scripts/download-malden-agendas.py --output-dir ~/Downloads/malden
#
# 6. Download documents AND video recordings:
#    python3 scripts/download-malden-agendas.py --include-video
#
# 7. Download only video recordings (skip PDFs):
#    python3 scripts/download-malden-agendas.py --video-only
#
# 8. Preview video recordings without downloading:
#    python3 scripts/download-malden-agendas.py --video-only --dry-run
#
# 9. Run on a schedule (cron — evening):
#    0 19 * * 1-5 cd /path/to/repo && python3 scripts/download-malden-agendas.py
#
# VIDEO NOTE: Malden posts three kinds of video links, one per meeting at
# most: YouTube (live-stream VODs and regular uploads — downloaded via
# yt-dlp), SharePoint Stream share links on the city's own tenant
# (cityofmalden-my.sharepoint.com), and Zoom cloud-recording share links
# (cityofmalden.zoom.us/rec/share/...). Occasionally a Microsoft Teams link
# or a tinyurl.com redirect appears instead. Only the YouTube links are
# machine-downloadable; SharePoint/Zoom/Teams/tinyurl links are saved as
# .url Internet Shortcut files so the editor can still open them by hand —
# both SharePoint Stream and Zoom's share endpoints require an authenticated
# session that yt-dlp cannot establish (confirmed directly: SharePoint
# returns "Unsupported URL", Zoom returns "'NoneType' object is not
# subscriptable" instead of the underlying media).
#
# COVERAGE NOTE: School Committee (Board of Education) and Open Space &
# Recreation Committee (Parks & Recreation) are both regular categories on
# Malden's own Agenda Center — no external source was needed for either,
# unlike Arlington MA where Parks & Rec video required ACMi's YouTube
# playlists separately.
#
# BOARDS (51 as of 2026-08, per the full category dropdown on the hub page;
# the search endpoint above only returns whichever of these have an item in
# the requested date window):
#   Affordable Housing Trust Fund, Board of Appeal, Board of Assessors,
#   Board of Health, Board of Registrar of Voters, Cable License Renewal,
#   Cannabis Licensing and Enforcement Commission, Cemetery Trustees,
#   City Council, Commission on Climate Action and Sustainability,
#   Committee on Charter Review, Community Outreach,
#   Community Preservation Committee, Complete Streets Taskforce,
#   Conservation Commission, Council on Aging, Cultural Council,
#   Disability Commission, Emergency Management Team,
#   Energy Efficiency Commission, Historical Commission,
#   Human Rights & Fair Housing Commission, Licensing Board,
#   Local Historic District Study Committee,
#   Long-Term Transportation Planning Study Committee,
#   Malden Center for Arts & Culture Steering Committee,
#   Malden Housing Authority, Malden Housing Production Plan Advisory
#   Committee, Massport Community Advisory Committee,
#   Master Plan Steering Committee, MBTA 3A Zoning Working Group,
#   Municipal Building Committee - City Hall,
#   Municipal Building Committee - Malden River Works,
#   Municipal Building Committee - Police Station,
#   Mystic Regional Emergency Planning Committee,
#   Mystic Valley Development Commission,
#   Mystic Valley Public Health Coalition Meeting,
#   Mystic Valley Regional Charter School,
#   Northeast Metropolitan Regional Vocational School District,
#   Northeast Metro Tech Building Committee,
#   Open Space & Recreation Committee, Planning Board,
#   Police Community Advisory Committee, Problem Property Unit,
#   Public Works Commission, Racial Equity Commission,
#   Redevelopment Authority, Region 3E Public Health Emergency Preparedness
#   Coalition, Retirement Board, Rowe's Quarry / Overlook Ridge Committees,
#   School Committee, Sign Design Review Committee,
#   Site Plan Review Committee for ADUs and Educational, Religious &
#   Child Care Facility Uses, Special Education Parent Advisory Council
#   (SEPAC), Stadium and Athletic Field Commission, Traffic Commission,
#   Tree Hearings.
