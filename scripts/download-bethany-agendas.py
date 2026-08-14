#!/usr/bin/env python3
# download-bethany-agendas.py
# Download municipal meeting agendas, minutes, and video recordings from
# Bethany, CT for meetings within the past N days (and up to M days ahead).
#
# USAGE:
#   python3 scripts/download-bethany-agendas.py [options]
#
# REQUIREMENTS:
#   - Python 3.8+  (no third-party packages needed for docs)
#   - yt-dlp       (for video: pip install yt-dlp)
#   - Internet connection
#
# WHAT IT DOES:
#   Documents (default):
#     1. Fetches https://bethanyct.gov/agendas-minutes, which renders a
#        cascading dropdown UI backed by Telerik RadAjaxPanel / ASP.NET
#        UpdatePanel (QScend CMS — same platform as Southbury, Canton,
#        Monroe, etc. elsewhere in this repo, but Bethany's control-ID
#        prefix is "category" rather than "FB", and it has one extra
#        hierarchy level)
#     2. For each of the ~16 boards, makes an AJAX POST to discover its
#        document categories (typically "Agendas & Cancellations",
#        "Minutes", and sometimes "Meeting Packets" or "Notices")
#     3. For each category whose label looks like agendas/minutes, makes
#        another AJAX POST to discover year sub-folders
#     4. For each year that overlaps the date window, calls the QScend
#        qcontent REST API to list files and their last-modified Unix
#        timestamps
#     5. Filters files whose modified timestamp falls within the window
#     6. Downloads matching PDFs to beat-archive/bethany-agendas/YYYY-MM/
#     7. Appends a download log to beat-archive/bethany-agendas/download-log.txt
#
#   Video (default on; --no-video to skip):
#     8. Downloads recent videos from the Bethany Town Clerk YouTube
#        channel using yt-dlp — unlike most other QScend-CMS CT towns in
#        this repo, Bethany actually posts regular per-meeting recordings
#        here (titled "MM/DD/YYYY <Board Name> | Bethany, CT"), so this is
#        real coverage, not a best-effort long shot.
#
# SITE STRUCTURE (QScend CMS, ASP.NET WebForms + Telerik RadAjax):
#   Hub:      https://bethanyct.gov/agendas-minutes
#             (NOTE: www.bethany-ct.com/agendas-minutes 302-redirects to
#             https://bethanyct.gov/ — the ROOT, dropping the path — so
#             this script hits the .gov domain directly.)
#   AJAX:     POST /agendas-minutes with X-MicrosoftAjax: Delta=true
#   API:      https://bethanyct.gov/qcontent/api/v1/files/get/?folder=NNNNN
#   Files:    /filestorage/128/{board_id}/{category_id}/{year_id}/file.pdf
#   YouTube:  https://www.youtube.com/@bethanytownclerk
#
# FOLDER HIERARCHY: Board (root folder 128) -> Category -> Year -> files.
#   Confirmed uniform across every board checked (Board of Selectmen,
#   Planning & Zoning, Town Meetings, 250th Committee): there is always a
#   category level between board and year, unlike Southbury where some
#   boards skip straight from board to year. As a safety net this script
#   still falls back to calling the qcontent API directly on a category
#   folder if it has no year sub-select (mirrors Southbury's Pattern A/B
#   handling) in case a board is ever configured differently.
#
# NOTES:
#   - No bot protection; plain urllib works.
#   - The Telerik RadAjaxPanel intercepts __doPostBack and enriches the
#     POST with category$SM=category$category$APPanel|<trigger> and
#     RadAJAXControlID=category_AP. Plain urllib works once these fields
#     are included; no Playwright/Selenium required.
#   - The initial __VIEWSTATE/__EVENTVALIDATION can be reused for every
#     sibling selection at a given level without re-fetching the page.

import argparse
import calendar
import datetime
import json
import os
import re
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import http.cookiejar

BASE_URL = "https://bethanyct.gov"
MINUTES_URL = f"{BASE_URL}/agendas-minutes"
API_BASE = f"{BASE_URL}/qcontent/api/v1/files/get/"
YOUTUBE_CHANNEL = "https://www.youtube.com/@bethanytownclerk"
OUTPUT_DIR = "beat-archive/bethany-agendas"
DAYS_BACK = 4
DAYS_AHEAD = 7
DELAY_SECONDS = 0.5

YT_DLP_NODE = "node:/home/richkirby/.local/bin/yt-dlp-node"  # yt-dlp needs Node 22+; symlink kept current by scripts/update-yt-dlp-node.sh

UA = (
    "Mozilla/5.0 (X11; Linux x86_64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)

PANEL_PREFIX = "category"
BOARD_SELECT = "category$F_128"


def slugify(text, max_len=60):
    text = text.lower().strip()
    text = re.sub(r"[/\\]", "-", text)
    text = re.sub(r"[^\w\s-]", "", text)
    text = re.sub(r"[\s_]+", "-", text)
    text = re.sub(r"-{2,}", "-", text)
    return text.strip("-")[:max_len]


def parse_select_options(html, select_name):
    """Return [(value, label), ...] for a <select> element by name."""
    m = re.search(
        rf'<select[^>]+name="{re.escape(select_name)}"[^>]*>(.*?)</select>',
        html, re.DOTALL | re.IGNORECASE,
    )
    if not m:
        return []
    return [
        (v, re.sub(r"&amp;", "&", lbl).strip())
        for v, lbl in re.findall(
            r'<option[^>]+value="(\d+)">([^<]+)</option>', m.group(1)
        )
    ]


def parse_delta(text):
    """Parse an ASP.NET ScriptManager delta response into a dict."""
    result = {}
    i = 0
    while i < len(text):
        pipe1 = text.find('|', i)
        if pipe1 < 0:
            break
        try:
            length = int(text[i:pipe1])
        except ValueError:
            break
        i = pipe1 + 1
        pipe2 = text.find('|', i)
        if pipe2 < 0:
            break
        type_ = text[i:pipe2]
        i = pipe2 + 1
        pipe3 = text.find('|', i)
        if pipe3 < 0:
            break
        id_ = text[i:pipe3]
        i = pipe3 + 1
        content = text[i:i + length]
        i = i + length + 1
        result[f"{type_}:{id_}" if id_ else type_] = content
    return result


class BethanySession:
    """Manages HTTP session and ASP.NET form state across AJAX calls."""

    def __init__(self):
        jar = http.cookiejar.CookieJar()
        self.opener = urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(jar)
        )
        self._init_vs = ""
        self._init_vsg = ""
        self._init_ev = ""

    def _fetch(self, url, data=None, ajax=False):
        headers = {"User-Agent": UA}
        if ajax:
            headers.update({
                "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
                "X-MicrosoftAjax": "Delta=true",
                "X-Requested-With": "XMLHttpRequest",
                "Referer": MINUTES_URL,
                "Origin": BASE_URL,
            })
        elif data:
            headers["Content-Type"] = "application/x-www-form-urlencoded; charset=UTF-8"
        req = urllib.request.Request(
            url,
            data=urllib.parse.urlencode(data).encode() if data else None,
            headers=headers,
        )
        try:
            with self.opener.open(req, timeout=30) as r:
                charset = r.headers.get_content_charset() or "utf-8"
                return r.read().decode(charset, "replace")
        except urllib.error.HTTPError as e:
            print(f"  HTTP {e.code} — {url}", file=sys.stderr)
            return None
        except urllib.error.URLError as e:
            print(f"  ERROR: {e} — {url}", file=sys.stderr)
            return None

    def _ajax_post(self, trigger_name, trigger_value, vs, vsg, ev,
                   extra_fields=None):
        """POST one Telerik AJAX step. Returns (panel_html, vs, vsg, ev)."""
        data = {
            f"{PANEL_PREFIX}$SM": f"{PANEL_PREFIX}${PANEL_PREFIX}$APPanel|{trigger_name}",
            "RadAJAXControlID": f"{PANEL_PREFIX}_AP",
            "__EVENTTARGET": trigger_name,
            "__VIEWSTATE": vs,
            "__VIEWSTATEGENERATOR": vsg,
            "__EVENTVALIDATION": ev,
            trigger_name: trigger_value,
            "__ASYNCPOST": "true",
        }
        if extra_fields:
            data.update(extra_fields)
        resp = self._fetch(MINUTES_URL, data=data, ajax=True)
        if not resp:
            return "", vs, vsg, ev
        parts = parse_delta(resp)
        panel = parts.get(f"updatePanel:{PANEL_PREFIX}_{PANEL_PREFIX}_APPanel", "")
        new_vs = parts.get("hiddenField:__VIEWSTATE", vs)
        new_vsg = parts.get("hiddenField:__VIEWSTATEGENERATOR", vsg)
        new_ev = parts.get("hiddenField:__EVENTVALIDATION", ev)
        return panel, new_vs, new_vsg, new_ev

    def init(self):
        """Load the hub page and save initial form tokens. Returns board list."""
        html = self._fetch(MINUTES_URL)
        if not html:
            return []
        vs = re.search(r'id="__VIEWSTATE"\s+value="([^"]+)"', html)
        vsg = re.search(r'id="__VIEWSTATEGENERATOR"\s+value="([^"]+)"', html)
        ev = re.search(r'id="__EVENTVALIDATION"\s+value="([^"]+)"', html)
        if not (vs and vsg and ev):
            return []
        self._init_vs = vs.group(1)
        self._init_vsg = vsg.group(1)
        self._init_ev = ev.group(1)
        return parse_select_options(html, BOARD_SELECT)

    def get_categories(self, board_id):
        """Select a board. Returns ([(cat_id, label)], vs, vsg, ev)."""
        panel, vs, vsg, ev = self._ajax_post(
            BOARD_SELECT, board_id,
            self._init_vs, self._init_vsg, self._init_ev,
        )
        cat_opts = parse_select_options(panel, f"{PANEL_PREFIX}$F_{board_id}")
        return cat_opts, vs, vsg, ev

    def get_years(self, board_id, cat_id, vs, vsg, ev):
        """Select a category. Returns [(year_id, label)]."""
        panel, _, _, _ = self._ajax_post(
            f"{PANEL_PREFIX}$F_{board_id}", cat_id, vs, vsg, ev,
            extra_fields={BOARD_SELECT: board_id},
        )
        return parse_select_options(panel, f"{PANEL_PREFIX}$F_{cat_id}")

    def api_files(self, folder_id):
        """Fetch file list for a folder via the qcontent REST API."""
        raw = self._fetch(f"{API_BASE}?folder={folder_id}")
        if not raw:
            return []
        try:
            data = json.loads(raw)
            return data if isinstance(data, list) else []
        except json.JSONDecodeError:
            return []

    def download(self, href, dest_path):
        """Download a file by its relative href. Returns True on success."""
        url = href if href.startswith("http") else BASE_URL + href
        req = urllib.request.Request(url, headers={"User-Agent": UA})
        try:
            with self.opener.open(req, timeout=60) as r:
                with open(dest_path, "wb") as f:
                    f.write(r.read())
            return True
        except Exception as e:
            print(f"  WARNING: {e}", file=sys.stderr)
            return False


def label_ok(label, no_minutes, no_agendas):
    lc = label.lower()
    if "minutes" in lc:
        return not no_minutes
    if "agenda" in lc:
        return not no_agendas
    return False  # skip Meeting Packets, Notices, Notice & Warning, etc.


# --- Video (YouTube channel via yt-dlp) ---

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
        # Channel listing is newest-first; without this, yt-dlp fully
        # extracts every video in the channel's history just to check its
        # date, which trips YouTube's per-session rate limit.
        "--break-match-filters", f"upload_date>={date_str}",
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


# --- Main ---

def main():
    parser = argparse.ArgumentParser(
        description="Download Bethany, CT municipal agendas, minutes, and "
                    "video for meetings within the past N days (and up to M days ahead)."
    )
    parser.add_argument("--days", type=int, default=DAYS_BACK, metavar="N",
                        help=f"Look back N days (default: {DAYS_BACK})")
    parser.add_argument("--ahead", type=int, default=DAYS_AHEAD, metavar="N",
                        help=f"Also include meetings up to N days ahead (default: {DAYS_AHEAD})")
    parser.add_argument("--output-dir", default=OUTPUT_DIR, metavar="DIR",
                        help=f"Destination directory (default: {OUTPUT_DIR})")
    parser.add_argument("--dry-run", action="store_true",
                        help="List matching items without downloading")
    parser.add_argument("--board", metavar="NAME",
                        help="Only process boards whose name contains NAME (case-insensitive)")
    parser.add_argument("--no-minutes", action="store_true", help="Skip Minutes categories")
    parser.add_argument("--no-agendas", action="store_true", help="Skip Agendas categories")
    parser.add_argument("--no-video", action="store_true", help="Skip YouTube channel video downloads")
    parser.add_argument("--video-only", action="store_true", help="Download only video recordings")
    args = parser.parse_args()

    now = datetime.datetime.now()
    if (now.weekday() == 5 and now.hour >= 18) or (now.weekday() == 6 and now.hour < 12):  # Saturday night, Sunday morning
        print("Skipping — no downloads on Saturday nights or Sunday mornings.")
        sys.exit(0)

    do_docs = not args.video_only
    do_video = not args.no_video or args.video_only

    today = datetime.date.today()
    cutoff = today - datetime.timedelta(days=args.days)
    future_limit = today + datetime.timedelta(days=args.ahead)
    cutoff_ts = calendar.timegm(cutoff.timetuple())
    future_ts = calendar.timegm((future_limit + datetime.timedelta(days=1)).timetuple())
    years_needed = set(range(cutoff.year, future_limit.year + 1))

    print(f"Date window : {cutoff} to {future_limit}")
    print(f"Hub URL     : {MINUTES_URL}")
    if not args.dry_run:
        print(f"Output dir  : {args.output_dir}")
    print()

    candidates = []

    if do_docs:
        sess = BethanySession()

        print("Fetching board list...")
        boards = sess.init()
        if not boards:
            print("ERROR: Could not load board list.", file=sys.stderr)
            sys.exit(1)
        print(f"Discovered {len(boards)} board(s).")

        if args.board:
            filt = args.board.lower()
            boards = [(bid, bname) for bid, bname in boards if filt in bname.lower()]
            print(f"Filtered to {len(boards)} board(s) matching '{args.board}'.")

        print()
        for board_id, board_name in boards:
            print(f"  Scanning: {board_name}")

            cat_opts, bvs, bvsg, bev = sess.get_categories(board_id)
            time.sleep(DELAY_SECONDS)

            for cat_id, cat_label in cat_opts:
                if not label_ok(cat_label, args.no_minutes, args.no_agendas):
                    continue

                year_opts = sess.get_years(board_id, cat_id, bvs, bvsg, bev)
                time.sleep(DELAY_SECONDS)

                folder_ids = []
                if year_opts:
                    for year_id, year_label in year_opts:
                        m = re.search(r"\b(20\d\d)\b", year_label)
                        if m and int(m.group(1)) not in years_needed:
                            continue
                        folder_ids.append((year_id, year_label.strip()))
                else:
                    # No year sub-select — files may live directly in the category.
                    folder_ids.append((cat_id, ""))

                for folder_id, year_label in folder_ids:
                    for f in sess.api_files(folder_id):
                        modified = f.get("modified", 0)
                        if cutoff_ts <= modified < future_ts:
                            candidates.append({
                                "board": board_name,
                                "category": cat_label,
                                "year": year_label,
                                "href": f["href"],
                                "name": f["name"],
                                "modified": modified,
                            })

        candidates.sort(key=lambda x: (-x["modified"], x["board"]))
        print(f"\nDocuments in window : {len(candidates)}\n")

    videos = []
    if do_video and args.dry_run:
        print(f"Fetching YouTube channel listing (uploaded since {cutoff})...")
        videos = download_channel_videos(YOUTUBE_CHANNEL, args.output_dir, cutoff, dry_run=True)
        print(f"Video       : {len(videos)} found\n")

    if not candidates and not videos and not do_video:
        print("No documents found within the date window.")
        sys.exit(0)

    if args.dry_run:
        if candidates:
            print(f"{'Board':<35} {'Category':<24} {'Modified':<12} {'File'}")
            print("-" * 110)
            for c in candidates:
                mod_date = datetime.date.fromtimestamp(c["modified"]).isoformat()
                print(f"{c['board'][:34]:<35} {c['category'][:23]:<24} {mod_date:<12} {c['name'][:40]}")
            print(f"\n{len(candidates)} document(s).")
        if videos:
            print(f"\nYouTube channel videos (uploaded since {cutoff}):")
            for line in videos:
                print(f"  {line}")
        print("\nRe-run without --dry-run to download.")
        return

    os.makedirs(args.output_dir, exist_ok=True)
    log_path = os.path.join(args.output_dir, "download-log.txt")
    log_lines = []
    dl_ok = dl_skip = dl_fail = 0

    for c in candidates:
        mod_dt = datetime.datetime.fromtimestamp(c["modified"])
        month_dir = os.path.join(args.output_dir, mod_dt.strftime("%Y-%m"))
        os.makedirs(month_dir, exist_ok=True)

        board_slug = slugify(c["board"])
        dest = os.path.join(month_dir, f"{board_slug}_{c['name']}")

        if os.path.exists(dest):
            print(f"  skip (exists)  {os.path.basename(dest)}")
            dl_skip += 1
            continue

        print(f"  [{c['board']}] {c['category']} — {c['name']}")
        print(f"  downloading    {os.path.basename(dest)}")

        ok = sess.download(c["href"], dest)
        time.sleep(DELAY_SECONDS)

        if ok:
            dl_ok += 1
            log_lines.append(f"{datetime.datetime.now().isoformat()}  OK       {dest}")
        else:
            dl_fail += 1
            log_lines.append(f"{datetime.datetime.now().isoformat()}  FAILED   {BASE_URL + c['href']}")
            if os.path.exists(dest):
                os.remove(dest)

    if log_lines:
        with open(log_path, "a") as f:
            f.write("\n".join(log_lines) + "\n")

    print()
    print(f"Documents — downloaded: {dl_ok}  skipped: {dl_skip}  failed: {dl_fail}")

    if do_video:
        print()
        print(f"Downloading YouTube channel videos (since {cutoff})...")
        print(f"  Channel: {YOUTUBE_CHANNEL}")
        print(f"  Output:  {os.path.join(args.output_dir, 'videos')}/")
        v_dl, v_skip, v_fail = download_channel_videos(YOUTUBE_CHANNEL, args.output_dir, cutoff)
        if v_fail:
            print("  WARNING: one or more video downloads failed", file=sys.stderr)

    print()
    if dl_ok + dl_skip:
        print(f"Files in: {args.output_dir}")
    if log_lines:
        print(f"Log:      {log_path}")


if __name__ == "__main__":
    main()


# --- Tips ---
#
# 1. Preview without downloading:
#    python3 scripts/download-bethany-agendas.py --dry-run
#
# 2. Narrow to one board:
#    python3 scripts/download-bethany-agendas.py --board "Board of Selectmen"
#
# 3. Change the lookback/lookahead window:
#    python3 scripts/download-bethany-agendas.py --days 30 --ahead 14
#
# 4. Documents only (skip YouTube channel videos):
#    python3 scripts/download-bethany-agendas.py --no-video
#
# 5. Video only:
#    python3 scripts/download-bethany-agendas.py --video-only
#
# 6. Run on a schedule (cron — evening):
#    0 20 * * 1-5 cd /path/to/repo && python3 scripts/download-bethany-agendas.py
#
# SITE NOTES:
#   - No bot protection; plain urllib works.
#   - www.bethany-ct.com/agendas-minutes redirects to the .gov root, not
#     the sub-page — always use bethanyct.gov directly.
#   - The qcontent REST API returns Unix modification timestamps used for
#     filtering; API calls on a pure category folder (no files, only year
#     sub-folders) correctly return [].
#   - Video: @bethanytownclerk on YouTube posts real per-meeting
#     recordings titled "MM/DD/YYYY <Board Name> | Bethany, CT" — unlike
#     some other small CT towns in this repo, this is genuine, current
#     coverage, not a long-shot search.
