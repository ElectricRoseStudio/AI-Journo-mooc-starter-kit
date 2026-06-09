#!/usr/bin/env python3
# download-naugatuck-agendas.py
# Download municipal meeting agendas, notices, and minutes from Naugatuck CT
# for documents posted in the past N days (and optionally N days ahead).
#
# USAGE:
#   python3 scripts/download-naugatuck-agendas.py [options]
#
# REQUIREMENTS:
#   - Python 3.6+  (no third-party packages needed)
#   - Internet connection
#
# WHAT IT DOES:
#   1. Fetches the Naugatuck CT Agendas & Minutes page to discover all boards
#   2. For each board, navigates the 3-level folder hierarchy via ASP.NET postbacks:
#      Board folder → Category folder → Year folder → File listing
#   3. Parses each file's upload date from the listing
#   4. Downloads PDFs whose upload date falls within the lookback window
#   5. Appends a download log to beat-archive/naugatuck-agendas/download-log.txt
#
# SITE STRUCTURE:
#   Naugatuck CT uses QScend Technologies CMS (naugatuck-ct.gov).
#   /agendas-minutes — master board selector (dropdown: agendas$F_79)
#   Board dropdown shows folder IDs for all ~28 boards.
#   Selecting a board triggers an ASP.NET postback revealing a category dropdown
#   (agendas$F_{boardID}) with entries like Notices, Agendas, Minutes.
#   Selecting a category reveals a year dropdown (agendas$F_{catID}).
#   Selecting a year loads the file listing inside div#agendas_F.
#   PDFs are at naugatuck-ct.gov/filestorage/77/79/{boardID}/{catID}/{yearID}/{file}
#   No authentication required; only the ASP.NET session cookie must be maintained.

import argparse
import datetime
import html as html_module
import http.cookiejar
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

# --- Configuration ---
BASE_URL = "https://www.naugatuck-ct.gov"
AGENDA_URL = f"{BASE_URL}/agendas-minutes"
OUTPUT_DIR = "beat-archive/naugatuck-agendas"
DAYS_BACK = 4
DAYS_AHEAD = 7
DELAY_SECONDS = 1
PAGE_DELAY = 0.5

# Category names to include (case-insensitive). None = include all.
# "recordings" / "recording" is included so audio/video files are downloaded
# automatically if Naugatuck ever populates the BAA Recordings folder.
DEFAULT_CATEGORIES = {
    "notices", "notice",
    "agendas", "agenda",
    "minutes", "minute",
    "recordings", "recording",
}

UA = "Naugatuck-Agendas-Downloader/1.0 (journalism research)"


# --- Session (maintains cookies and ViewState across postbacks) ---

class Session:
    """Manages an ASP.NET session with persistent cookies and ViewState."""

    def __init__(self):
        self.jar = http.cookiejar.CookieJar()
        self.opener = urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(self.jar)
        )
        self.vs = ""
        self.vsg = ""
        self.ev = ""

    def get(self, url):
        req = urllib.request.Request(
            url, headers={"User-Agent": UA, "Accept": "text/html,*/*"}
        )
        try:
            with self.opener.open(req, timeout=30) as r:
                html = r.read().decode("utf-8", errors="replace")
            self._update_state(html)
            return html
        except urllib.error.URLError as e:
            print(f"  ERROR fetching {url}: {e}", file=sys.stderr)
            return None

    def post(self, url, event_target, fields):
        """POST an ASP.NET synchronous postback and return the response HTML."""
        data = {
            "__EVENTTARGET": event_target,
            "__EVENTARGUMENT": "",
            "__LASTFOCUS": "",
            "__VIEWSTATE": self.vs,
            "__VIEWSTATEGENERATOR": self.vsg,
            "__EVENTVALIDATION": self.ev,
        }
        data.update(fields)
        encoded = urllib.parse.urlencode(data).encode("utf-8")
        req = urllib.request.Request(
            url,
            data=encoded,
            headers={
                "User-Agent": UA,
                "Content-Type": "application/x-www-form-urlencoded",
                "Referer": url,
                "Accept": "text/html,*/*",
            },
        )
        try:
            with self.opener.open(req, timeout=30) as r:
                html = r.read().decode("utf-8", errors="replace")
            self._update_state(html)
            return html
        except urllib.error.URLError as e:
            print(f"  ERROR posting to {url}: {e}", file=sys.stderr)
            return None

    def save_state(self):
        return (self.vs, self.vsg, self.ev)

    def restore_state(self, state):
        self.vs, self.vsg, self.ev = state

    def _update_state(self, html):
        m = re.search(r'id="__VIEWSTATE" value="([^"]+)"', html)
        if m:
            self.vs = m.group(1)
        m = re.search(r'id="__VIEWSTATEGENERATOR" value="([^"]+)"', html)
        if m:
            self.vsg = m.group(1)
        m = re.search(r'id="__EVENTVALIDATION" value="([^"]+)"', html)
        if m:
            self.ev = m.group(1)


# --- Parsing ---

def parse_boards(html, select_name="agendas$F_79"):
    """Return list of (folder_id, board_name) from the top-level board dropdown."""
    sel = re.search(
        rf'<select[^>]+name="{re.escape(select_name)}"[^>]*>(.*?)</select>',
        html,
        re.DOTALL | re.IGNORECASE,
    )
    if not sel:
        return []
    boards = []
    for val, label in re.findall(
        r'<option[^>]+value="(\d+)"[^>]*>([^<]+)<', sel.group(1)
    ):
        name = html_module.unescape(label.strip())
        if val:
            boards.append((val, name))
    return boards


def parse_subfolders(html, parent_id, prefix="agendas"):
    """
    Return (folder_id, name) list from the dropdown that appears after selecting
    parent_id.  Control name: {prefix}$F_{parent_id}.
    Returns [] if no such dropdown exists (no sub-level for this folder).
    """
    select_name = f"{prefix}$F_{parent_id}"
    sel = re.search(
        rf'<select[^>]+name="{re.escape(select_name)}"[^>]*>(.*?)</select>',
        html,
        re.DOTALL | re.IGNORECASE,
    )
    if not sel:
        return []
    items = []
    for val, label in re.findall(
        r'<option[^>]+value="([^"]*)"[^>]*>([^<]+)<', sel.group(1)
    ):
        name = html_module.unescape(label.strip())
        if val:  # skip the empty "Make Your Selection" / "Choose from following..."
            items.append((val, name))
    return items


def parse_file_listing(html, prefix="agendas"):
    """
    Parse the file listing inside div#{prefix}_F.
    Returns list of dicts: {href, label, description, uploaded_date, ext}.
    Matches PDFs and audio/video files (MP3, MP4, M4A, M4V, WAV, MOV, WMV).
    """
    div_id = f"{prefix}_F"
    m = re.search(
        rf'id="{re.escape(div_id)}">(.*?)</div>\s*<div class="FB_Footer',
        html,
        re.DOTALL,
    )
    if not m:
        return []

    content = m.group(1)
    files = []

    for li in re.findall(r"<LI[^>]*>(.*?)</LI>", content, re.DOTALL | re.IGNORECASE):
        link = re.search(
            r'href="(/filestorage/[^"]+\.(?:pdf|mp[34]|m4[av]|wav|mov|wmv|avi))"'
            r'[^>]*title="([^"]*)"[^>]*>([^<]+)<',
            li,
            re.IGNORECASE,
        )
        if not link:
            continue
        href = link.group(1)
        description = html_module.unescape(link.group(2).strip())
        label = html_module.unescape(link.group(3).strip())
        ext = os.path.splitext(href)[1].lower() or ".pdf"

        uploaded_date = None
        up_m = re.search(r"uploaded on (\d{1,2}/\d{1,2}/\d{4} \d{1,2}:\d{2} [APap][Mm])", li)
        if up_m:
            try:
                uploaded_date = datetime.datetime.strptime(
                    up_m.group(1), "%m/%d/%Y %I:%M %p"
                ).date()
            except ValueError:
                pass

        files.append(
            {
                "href": href,
                "label": label,
                "description": description,
                "uploaded_date": uploaded_date,
                "ext": ext,
            }
        )
    return files


def parse_upload_date_from_label(label):
    """
    Try to extract a meeting date from a label like '12-30-2025 Special Minutes'.
    Returns a date object or None.
    """
    m = re.match(r"(\d{2})-(\d{2})-(\d{4})", label)
    if m:
        try:
            return datetime.date(int(m.group(3)), int(m.group(1)), int(m.group(2)))
        except ValueError:
            pass
    return None


# --- Download ---

def download_file(url, dest_path):
    """Download a PDF to dest_path. Returns True on success."""
    full_url = BASE_URL + url if url.startswith("/") else url
    req = urllib.request.Request(full_url, headers={"User-Agent": UA})
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            if r.status != 200:
                print(f"  WARNING: HTTP {r.status} — {full_url}", file=sys.stderr)
                return False
            with open(dest_path, "wb") as f:
                f.write(r.read())
        return True
    except Exception as e:
        print(f"  WARNING: {e} — {full_url}", file=sys.stderr)
        return False


def slugify(text, max_len=50):
    text = text.lower().strip()
    text = re.sub(r"[/\\]", "-", text)
    text = re.sub(r"\s+-\s+|\s+", "-", text)
    text = re.sub(r"[^\w-]", "", text)
    text = re.sub(r"-{2,}", "-", text)
    return text.strip("-")[:max_len]


def make_dest_path(board_name, category_name, file_info, output_dir):
    label = file_info["label"]
    uploaded = file_info["uploaded_date"]

    # Use the uploaded date for directory organization; fall back to today
    ref_date = uploaded or datetime.date.today()
    month_dir = ref_date.strftime("%Y-%m")
    month_path = os.path.join(output_dir, month_dir)
    os.makedirs(month_path, exist_ok=True)

    board_slug = slugify(board_name, max_len=35)
    cat_slug = slugify(category_name, max_len=10)
    label_slug = slugify(label, max_len=40)
    ext = file_info.get("ext", ".pdf")

    fname = f"{board_slug}-{cat_slug}-{label_slug}{ext}"
    return os.path.join(month_path, fname)


# --- Main ---

def main():
    parser = argparse.ArgumentParser(
        description=(
            "Download Naugatuck CT municipal agendas, notices, and minutes "
            "posted within the past N days."
        )
    )
    parser.add_argument(
        "--days", type=int, default=DAYS_BACK, metavar="N",
        help=f"Look back N days by upload date (default: {DAYS_BACK})",
    )
    parser.add_argument(
        "--ahead", type=int, default=DAYS_AHEAD, metavar="N",
        help=f"Also include docs uploaded up to N days ahead (default: {DAYS_AHEAD})",
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
        "--all-categories", action="store_true",
        help="Include all category types (default: Notices, Agendas, Minutes only)",
    )
    args = parser.parse_args()

    now = datetime.datetime.now()
    if (now.weekday() == 5 and now.hour >= 18) or (now.weekday() == 6 and now.hour < 12):  # Saturday night, Sunday morning
        print("Skipping — no downloads on Saturday nights or Sunday mornings.")
        sys.exit(0)

    today = datetime.date.today()
    cutoff = today - datetime.timedelta(days=args.days)
    future_limit = today + datetime.timedelta(days=args.ahead)
    start_year = cutoff.year

    print(f"Upload date window : {cutoff} to {future_limit}")
    print(f"Site               : {BASE_URL}")
    if not args.dry_run:
        print(f"Output dir         : {args.output_dir}")
    print()

    session = Session()

    # --- Step 0: fetch the board list ---
    print(f"Fetching board list from {AGENDA_URL} ...")
    html0 = session.get(AGENDA_URL)
    if not html0:
        print("ERROR: Could not fetch the Agendas & Minutes page.", file=sys.stderr)
        sys.exit(1)

    state0 = session.save_state()

    boards = parse_boards(html0, "agendas$F_79")
    if not boards:
        print("ERROR: No boards found — page structure may have changed.", file=sys.stderr)
        sys.exit(1)

    if args.board:
        filter_name = args.board.lower()
        boards = [(bid, bname) for bid, bname in boards if filter_name in bname.lower()]
        if not boards:
            print(f"ERROR: No boards match '{args.board}'.", file=sys.stderr)
            sys.exit(1)

    print(f"Found {len(boards)} board(s).\n")

    # Collect all matching documents
    matches = []

    for board_id, board_name in boards:
        print(f"  [{board_id}] {board_name}")

        # Restore to initial state so each board starts fresh
        session.restore_state(state0)

        # --- Step 1: select the board ---
        html1 = session.post(
            AGENDA_URL,
            event_target="agendas$F_79",
            fields={"agendas$F_79": board_id},
        )
        if not html1:
            print(f"    WARNING: Could not load board {board_name!r}", file=sys.stderr)
            time.sleep(PAGE_DELAY)
            continue

        state1 = session.save_state()
        categories = parse_subfolders(html1, board_id, "agendas")

        if not categories:
            print(f"    (no categories found)", file=sys.stderr)
            time.sleep(PAGE_DELAY)
            continue

        for cat_id, cat_name in categories:
            if not args.all_categories and cat_name.lower() not in DEFAULT_CATEGORIES:
                continue

            # --- Step 2: select the category ---
            session.restore_state(state1)
            html2 = session.post(
                AGENDA_URL,
                event_target=f"agendas$F_{board_id}",
                fields={
                    "agendas$F_79": board_id,
                    f"agendas$F_{board_id}": cat_id,
                },
            )
            if not html2:
                print(f"    WARNING: Could not load category {cat_name!r}", file=sys.stderr)
                time.sleep(PAGE_DELAY)
                continue

            state2 = session.save_state()
            years = parse_subfolders(html2, cat_id, "agendas")

            if not years:
                # No year level — files may be listed directly; try parsing now
                files = parse_file_listing(html2, "agendas")
                for f in files:
                    ud = f["uploaded_date"]
                    if ud and cutoff <= ud <= future_limit:
                        matches.append({
                            "board": board_name,
                            "category": cat_name,
                            "year": "—",
                            **f,
                        })
                time.sleep(PAGE_DELAY)
                continue

            for year_id, year_name in years:
                try:
                    year_int = int(year_name)
                except ValueError:
                    year_int = 9999
                if year_int < start_year:
                    break  # years are newest-first; stop when too old

                # --- Step 3: select the year ---
                session.restore_state(state2)
                html3 = session.post(
                    AGENDA_URL,
                    event_target=f"agendas$F_{cat_id}",
                    fields={
                        "agendas$F_79": board_id,
                        f"agendas$F_{board_id}": cat_id,
                        f"agendas$F_{cat_id}": year_id,
                    },
                )
                if not html3:
                    print(
                        f"    WARNING: Could not load year {year_name} for {cat_name!r}",
                        file=sys.stderr,
                    )
                    time.sleep(PAGE_DELAY)
                    continue

                files = parse_file_listing(html3, "agendas")
                for f in files:
                    ud = f["uploaded_date"]
                    if ud and cutoff <= ud <= future_limit:
                        matches.append({
                            "board": board_name,
                            "category": cat_name,
                            "year": year_name,
                            **f,
                        })

                time.sleep(PAGE_DELAY)

        time.sleep(PAGE_DELAY)

    matches.sort(key=lambda x: (x.get("uploaded_date") or datetime.date.min), reverse=True)

    print()
    print(f"Documents found in window: {len(matches)}")
    print()

    if not matches:
        print("No documents found in the date window.")
        sys.exit(0)

    if args.dry_run:
        print(f"{'Board':<38} {'Category':<10} {'Label':<40} Uploaded")
        print("-" * 100)
        for m in matches:
            ud = str(m.get("uploaded_date") or "unknown")
            print(
                f"{m['board'][:37]:<38} {m['category'][:9]:<10} "
                f"{m['label'][:39]:<40} {ud}"
            )
        print(f"\n{len(matches)} document(s). Re-run without --dry-run to download.")
        return

    # --- Download ---
    os.makedirs(args.output_dir, exist_ok=True)
    log_path = os.path.join(args.output_dir, "download-log.txt")
    log_lines = []
    downloaded = skipped = failed = 0

    for m in matches:
        dest = make_dest_path(m["board"], m["category"], m, args.output_dir)
        label = os.path.basename(dest)

        if os.path.exists(dest):
            print(f"  skip (exists)  {label}")
            skipped += 1
            continue

        ud = m.get("uploaded_date") or "?"
        print(f"  [uploaded {ud}] {m['board']} / {m['category']}")
        print(f"  downloading    {label}")

        if download_file(m["href"], dest):
            downloaded += 1
            log_lines.append(
                f"{datetime.datetime.now().isoformat()}  OK       {dest}"
            )
        else:
            failed += 1
            log_lines.append(
                f"{datetime.datetime.now().isoformat()}  FAILED   {BASE_URL}{m['href']}"
            )
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


# --- Tips for customizing this script ---
#
# 1. Preview before downloading:
#    python3 scripts/download-naugatuck-agendas.py --dry-run
#
# 2. Narrow to one board:
#    python3 scripts/download-naugatuck-agendas.py --board "Planning Commission"
#
# 3. Change the lookback window:
#    python3 scripts/download-naugatuck-agendas.py --days 7
#
# 4. Include all document categories (recordings, applications, etc.):
#    python3 scripts/download-naugatuck-agendas.py --all-categories
#
# 5. Save files somewhere else:
#    python3 scripts/download-naugatuck-agendas.py --output-dir ~/Downloads/naugatuck
#
# 6. Run on a schedule (cron — 8 AM daily):
#    0 8 * * * cd /path/to/repo && python3 scripts/download-naugatuck-agendas.py
#
# 7. Process downloaded files with Claude afterward:
#    python3 scripts/download-naugatuck-agendas.py && \
#    bash scripts/batch-process.sh beat-archive/naugatuck-agendas/
#
# NOTE: The site uses QScend Technologies CMS (not CivicPlus AgendaCenter).
# The folder hierarchy is navigated via ASP.NET synchronous form postbacks.
# Each board requires up to 3 postback steps to reach the file listing.
# The --ahead flag (default: 7 days) catches agendas posted early for upcoming meetings.
#
# NOTE: Upload date (not meeting date) is used for the lookback window.
# A file uploaded on 2026-04-30 for a meeting on 2026-05-07 will be caught
# if your window includes 2026-04-30.
#
# NOTE: Files in the year folder matching the lookback window include all document
# types posted in that year — set --days to a smaller value to reduce noise.
