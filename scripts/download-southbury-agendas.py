#!/usr/bin/env python3
# download-southbury-agendas.py
# Download Southbury CT municipal agendas and minutes posted in the past N days.
#
# USAGE:
#   python3 scripts/download-southbury-agendas.py [options]
#
# REQUIREMENTS:
#   - Python 3.8+
#   - No third-party libraries needed (uses only stdlib)
#
# WHAT IT DOES:
#   1. Fetches https://www.southbury-ct.org/minutes, which renders a cascading
#      dropdown UI backed by Telerik RadAjaxPanel / ASP.NET UpdatePanel
#   2. For each of the 44 boards, makes an AJAX POST to discover year sub-folders
#   3. For each year that overlaps the date window, makes another AJAX POST to
#      discover leaf sub-folders (e.g. "Agendas", "Minutes", per-meeting folders)
#   4. For each leaf folder, calls the QScend qcontent REST API to list files and
#      their last-modified Unix timestamps
#   5. Filters files whose modified timestamp falls within the lookback window
#   6. Downloads matching PDFs to beat-archive/southbury-agendas/YYYY-MM/
#   7. Appends a download log to beat-archive/southbury-agendas/download-log.txt
#
# SITE STRUCTURE (QScend CMS, ASP.NET WebForms + Telerik RadAjax):
#   Hub:     https://www.southbury-ct.org/minutes
#   AJAX:    POST /minutes with X-MicrosoftAjax: Delta=true
#   API:     https://www.southbury-ct.org/qcontent/api/v1/files/get/?folder=NNNNN
#   Files:   /filestorage/20556/828/{board_id}/{year_id}/{subfolder_id}/file.pdf
#
# FOLDER HIERARCHY:
#   Pattern A (some boards): Board → Year → {Agendas, Minutes, per-meeting sub-folders}
#     - qcontent API on the year folder returns [] (files live in sub-folders)
#   Pattern B (other boards): Board → Year → files stored directly in year folder
#     - qcontent API on the year folder returns the file list
#   This script handles both patterns automatically.
#
# NOTES:
#   - No bot protection; plain urllib works.
#   - The Telerik RadAjaxPanel intercepts __doPostBack and enriches the POST with
#     FB$SM=FB$FB$APPanel|<trigger> and RadAJAXControlID=FB_AP. Plain urllib
#     works once these fields are included; no Playwright/Selenium required.
#   - The initial __VIEWSTATE/EVENTVALIDATION can be reused for all board
#     selections without re-fetching the page.
#   - Recordings: Southbury posts videos to YouTube (@SouthburyIT/streams) but
#     does not embed recording links on the minutes page. Not included here.

import argparse
import calendar
import datetime
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import http.cookiejar

BASE_URL = "https://www.southbury-ct.org"
MINUTES_URL = f"{BASE_URL}/minutes"
API_BASE = f"{BASE_URL}/qcontent/api/v1/files/get/"
OUTPUT_DIR = "beat-archive/southbury-agendas"
DAYS_BACK = 4
DELAY_SECONDS = 0.5

UA = (
    "Mozilla/5.0 (X11; Linux x86_64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)

BOARD_SELECT = "FB$F_828"


def slugify(text):
    text = text.lower().strip()
    text = re.sub(r"[/\\]", "-", text)
    text = re.sub(r"[^\w\s-]", "", text)
    text = re.sub(r"[\s_]+", "-", text)
    text = re.sub(r"-{2,}", "-", text)
    return text.strip("-")[:60]


def parse_select_options(html, select_name):
    """Return [(value, label), ...] for a <select> element by name."""
    m = re.search(
        rf'<select[^>]+name="{re.escape(select_name)}"[^>]*>(.*?)</select>',
        html, re.DOTALL | re.IGNORECASE,
    )
    if not m:
        return []
    return [
        (v, lbl.strip())
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


class SouthburySession:
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
            "FB$SM": f"FB$FB$APPanel|{trigger_name}",
            "RadAJAXControlID": "FB_AP",
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
        panel = parts.get("updatePanel:FB_FB_APPanel", "")
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

    def get_years(self, board_id):
        """Select a board. Returns ([(year_id, label)], vs, vsg, ev)."""
        panel, vs, vsg, ev = self._ajax_post(
            BOARD_SELECT, board_id,
            self._init_vs, self._init_vsg, self._init_ev,
        )
        year_opts = parse_select_options(panel, f"FB$F_{board_id}")
        return year_opts, vs, vsg, ev

    def get_subfolders(self, board_id, year_id, vs, vsg, ev):
        """Select a year. Returns [(subfolder_id, label)]."""
        panel, _, _, _ = self._ajax_post(
            f"FB$F_{board_id}", year_id, vs, vsg, ev,
            extra_fields={BOARD_SELECT: board_id},
        )
        return parse_select_options(panel, f"FB$F_{year_id}")

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
    if no_minutes and "minutes" in lc:
        return False
    if no_agendas and "agenda" in lc:
        return False
    return True


def main():
    parser = argparse.ArgumentParser(
        description="Download Southbury CT municipal agendas and minutes "
                    "posted in the past N days."
    )
    parser.add_argument("--days", type=int, default=DAYS_BACK, metavar="N",
                        help=f"Look back N days (default: {DAYS_BACK})")
    parser.add_argument("--output-dir", default=OUTPUT_DIR, metavar="DIR",
                        help=f"Destination directory (default: {OUTPUT_DIR})")
    parser.add_argument("--dry-run", action="store_true",
                        help="List matching files without downloading")
    parser.add_argument("--board", metavar="NAME",
                        help="Only process boards whose name contains NAME "
                             "(case-insensitive)")
    parser.add_argument("--no-minutes", action="store_true",
                        help="Skip sub-folders named 'Minutes'")
    parser.add_argument("--no-agendas", action="store_true",
                        help="Skip sub-folders named 'Agendas'")
    args = parser.parse_args()

    if datetime.date.today().weekday() in (6, 0):  # Sunday, Monday
        print("Skipping — no downloads on Sunday or Monday.")
        sys.exit(0)

    today = datetime.date.today()
    cutoff = today - datetime.timedelta(days=args.days)
    cutoff_ts = calendar.timegm(cutoff.timetuple())
    years_needed = set(range(cutoff.year, today.year + 1))

    print(f"Date window : {cutoff} to {today}  ({args.days} days back)")
    print(f"Hub URL     : {MINUTES_URL}")
    print(f"Output dir  : {args.output_dir}")
    print()

    sess = SouthburySession()

    print("Fetching board list...")
    boards = sess.init()
    if not boards:
        print("ERROR: Could not load board list.", file=sys.stderr)
        sys.exit(1)

    print(f"Discovered {len(boards)} board(s).")

    if args.board:
        filt = args.board.lower()
        boards = [(bid, bname) for bid, bname in boards
                  if filt in bname.lower()]
        print(f"Filtered to {len(boards)} board(s) matching '{args.board}'.")

    print()
    candidates = []

    for board_id, board_name in boards:
        print(f"  Scanning: {board_name}")

        year_opts, bvs, bvsg, bev = sess.get_years(board_id)
        if not year_opts:
            continue

        for year_id, year_label in year_opts:
            m = re.search(r'\b(20\d\d)\b', year_label)
            if m and int(m.group(1)) not in years_needed:
                continue

            subfolders = sess.get_subfolders(board_id, year_id, bvs, bvsg, bev)
            time.sleep(DELAY_SECONDS)

            if subfolders:
                # Pattern A: files live in sub-folders
                for sub_id, sub_label in subfolders:
                    if not label_ok(sub_label, args.no_minutes, args.no_agendas):
                        continue
                    for f in sess.api_files(sub_id):
                        if f.get("modified", 0) >= cutoff_ts:
                            candidates.append({
                                "board": board_name,
                                "year": year_label.strip(),
                                "context": sub_label.strip(),
                                "href": f["href"],
                                "name": f["name"],
                                "modified": f["modified"],
                            })
            else:
                # Pattern B: files live directly in year folder
                if not label_ok(year_label, args.no_minutes, args.no_agendas):
                    continue
                for f in sess.api_files(year_id):
                    if f.get("modified", 0) >= cutoff_ts:
                        candidates.append({
                            "board": board_name,
                            "year": year_label.strip(),
                            "context": "",
                            "href": f["href"],
                            "name": f["name"],
                            "modified": f["modified"],
                        })

    candidates.sort(key=lambda x: (-x["modified"], x["board"]))

    total = len(candidates)
    board_names = sorted({c["board"] for c in candidates})
    print(f"\nDocuments in window : {total}")
    if board_names:
        print(f"Boards with matches : {len(board_names)}")
    print()

    if not candidates:
        print("No documents found within the date window.")
        sys.exit(0)

    if args.dry_run:
        print(f"{'Board':<40} {'Modified':<12} {'Context':<22} {'File'}")
        print("-" * 105)
        for c in candidates:
            mod_date = datetime.date.fromtimestamp(c["modified"]).isoformat()
            ctx = c["context"][:21] if c["context"] else c["year"][:21]
            print(f"{c['board'][:39]:<40} {mod_date:<12} {ctx:<22} {c['name'][:35]}")
        print(f"\n{total} document(s). Re-run without --dry-run to download.")
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

        print(f"  [{c['board']}] {c['name']}")
        print(f"  downloading    {os.path.basename(dest)}")

        ok = sess.download(c["href"], dest)
        time.sleep(DELAY_SECONDS)

        if ok:
            dl_ok += 1
            log_lines.append(
                f"{datetime.datetime.now().isoformat()}  OK      {dest}")
        else:
            dl_fail += 1
            log_lines.append(
                f"{datetime.datetime.now().isoformat()}  FAIL    {BASE_URL + c['href']}")
            if os.path.exists(dest):
                os.remove(dest)

    if log_lines:
        with open(log_path, "a") as f:
            f.write("\n".join(log_lines) + "\n")

    print()
    print(f"Downloaded: {dl_ok}  Skipped: {dl_skip}  Failed: {dl_fail}")
    if dl_ok + dl_skip:
        print(f"Files in: {args.output_dir}")
    if log_lines:
        print(f"Log:      {log_path}")


if __name__ == "__main__":
    main()


# --- Tips ---
#
# 1. Preview without downloading:
#    python3 scripts/download-southbury-agendas.py --dry-run
#
# 2. Narrow to one board:
#    python3 scripts/download-southbury-agendas.py --board "Board of Selectmen"
#
# 3. Change the lookback window:
#    python3 scripts/download-southbury-agendas.py --days 60
#
# 4. Save files somewhere else:
#    python3 scripts/download-southbury-agendas.py --output-dir ~/Downloads/southbury
#
# 5. Agendas only (skip Minutes sub-folders):
#    python3 scripts/download-southbury-agendas.py --no-minutes
#
# 6. Minutes only (skip Agendas sub-folders):
#    python3 scripts/download-southbury-agendas.py --no-agendas
#
# 7. Run on a schedule (cron — 7 AM daily):
#    0 7 * * * cd /path/to/repo && python3 scripts/download-southbury-agendas.py
#
# SITE NOTES:
#   - No bot protection; plain urllib works.
#   - The QScend CMS uses a cascading folder hierarchy: Board → Year → Sub-folders.
#     Some boards skip the sub-folder level (files directly in year folder).
#   - The qcontent REST API returns Unix modification timestamps used for filtering.
#   - Recordings: Southbury posts videos to YouTube (@SouthburyIT/streams) but
#     does not link recordings from the minutes page. Manual check recommended.
