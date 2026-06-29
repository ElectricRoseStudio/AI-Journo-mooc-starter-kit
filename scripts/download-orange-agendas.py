#!/usr/bin/env python3
"""
Download municipal meeting agendas and minutes from the Orange CT Agenda Center
(CivicPlus CivicEngage CMS) for meetings within the past DAYS_BACK days and up
to DAYS_AHEAD days ahead, so recently posted agendas and minutes are captured.

USAGE:
    python3 scripts/download-orange-agendas.py [--dry-run] [--days N] [--ahead N]

SITE STRUCTURE:
    Hub:     https://www.orange-ct.gov/agendacenter
    Search:  https://www.orange-ct.gov/AgendaCenter/Search/?term=&CIDs=all
               &startDate=MM/DD/YYYY&endDate=MM/DD/YYYY
    Agenda:  https://www.orange-ct.gov/AgendaCenter/ViewFile/Agenda/_MMDDYYYY-ID
    Minutes: https://www.orange-ct.gov/AgendaCenter/ViewFile/Minutes/_MMDDYYYY-ID

    Search result layout per board section:
      <h2>Board Name</h2>
      <h3><strong>Mon DD, YYYY</strong> — Posted Mon DD, YYYY H:MM AM/PM</h3>
      <a href="/AgendaCenter/ViewFile/Agenda/_MMDDYYYY-ID">Title</a>
      <td class="minutes"><a href="/AgendaCenter/ViewFile/Minutes/_MMDDYYYY-ID">...</a>
"""

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

BASE_URL = "https://www.orange-ct.gov"
SEARCH_URL = f"{BASE_URL}/AgendaCenter/Search/"
OUTPUT_DIR = "beat-archive/orange-agendas"
DAYS_BACK = 3
DAYS_AHEAD = 7
DELAY_SECONDS = 0.8

UA = "Orange-CT-Agendas-Downloader/1.0 (journalism research)"

_URL_DATE_RE = re.compile(r"_(\d{2})(\d{2})(\d{4})-\d+")
_H3_DATE_RE = re.compile(
    r"\b(Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)\s+(\d{1,2}),\s+(\d{4})\b"
)
_MONTH_ABBR = {
    "Jan": 1, "Feb": 2, "Mar": 3, "Apr": 4, "May": 5, "Jun": 6,
    "Jul": 7, "Aug": 8, "Sep": 9, "Oct": 10, "Nov": 11, "Dec": 12,
}


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


class AgendaParser(html.parser.HTMLParser):
    """
    Single-pass parser for Orange CT CivicPlus Agenda Center search results.

    Tracks h2 (board name) and h3 (meeting date, with optional Posted/Amended
    secondary date), collecting ViewFile/Agenda and ViewFile/Minutes links.
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
            # h3 text: "Jun 17, 2026 — Posted Jun 15, 2026 3:47 PM"
            # First date match is the meeting date.
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


def slugify(text, max_len=50):
    text = text.lower().strip()
    text = re.sub(r"[/\\&]", "-", text)
    text = re.sub(r"\s+-\s+", "-", text)
    text = re.sub(r"[^\w\s-]", "", text)
    text = re.sub(r"[\s_]+", "-", text)
    text = re.sub(r"-{2,}", "-", text)
    return text.strip("-")[:max_len]


def make_dest_path(board, doc_type, meeting_date, output_dir):
    date_prefix = meeting_date.strftime("%Y-%m-%d")
    month_dir = os.path.join(output_dir, meeting_date.strftime("%Y-%m"))
    os.makedirs(month_dir, exist_ok=True)
    return os.path.join(month_dir, f"{date_prefix}-{slugify(board)}-{doc_type}.pdf")


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
        description="Download Orange CT municipal agendas and minutes."
    )
    parser.add_argument("--days", type=int, default=DAYS_BACK, metavar="N",
                        help=f"Look back N days (default: {DAYS_BACK})")
    parser.add_argument("--ahead", type=int, default=DAYS_AHEAD, metavar="N",
                        help=f"Include meetings up to N days ahead (default: {DAYS_AHEAD})")
    parser.add_argument("--output-dir", default=OUTPUT_DIR, metavar="DIR",
                        help=f"Destination directory (default: {OUTPUT_DIR})")
    parser.add_argument("--dry-run", action="store_true",
                        help="List matching documents without downloading")
    parser.add_argument("--board", metavar="NAME",
                        help="Only process boards whose name contains NAME (case-insensitive)")
    parser.add_argument("--no-minutes", action="store_true",
                        help="Skip minutes, download agendas only")
    parser.add_argument("--no-agendas", action="store_true",
                        help="Skip agendas, download minutes only")
    args = parser.parse_args()

    now = datetime.datetime.now()
    if (now.weekday() == 5 and now.hour >= 18) or (now.weekday() == 6 and now.hour < 12):
        print("Skipping — no downloads on Saturday nights or Sunday mornings.")
        sys.exit(0)

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
        print("ERROR: Could not fetch search results.", file=sys.stderr)
        sys.exit(1)

    agenda_parser = AgendaParser()
    agenda_parser.feed(html_text)
    all_items = agenda_parser.get_items()

    if not all_items:
        print("No agenda items found in date window.")
        return

    if args.board:
        all_items = [i for i in all_items if args.board.lower() in i["board"].lower()]
        print(f"Filtered to {len(all_items)} item(s) matching '{args.board}'.")

    print(f"Found {len(all_items)} meeting(s) across "
          f"{len({i['board'] for i in all_items})} board(s).")
    print()

    tasks = []
    for item in sorted(all_items, key=lambda x: x["meeting_date"], reverse=True):
        if item["agenda_url"] and not args.no_agendas:
            tasks.append({**item, "doc_type": "agenda", "href": item["agenda_url"]})
        if item["minutes_url"] and not args.no_minutes:
            tasks.append({**item, "doc_type": "minutes", "href": item["minutes_url"]})

    if not tasks:
        print("No downloadable items found within the date window.")
        return

    if args.dry_run:
        print(f"{'Board':<45} {'Date':<12} Type")
        print("-" * 70)
        for t in tasks:
            print(f"{t['board'][:44]:<45} {t['meeting_date']!s:<12} {t['doc_type']}")
        print(f"\n{len(tasks)} document(s). Re-run without --dry-run to download.")
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

        base = make_dest_path(t["board"], t["doc_type"], t["meeting_date"], args.output_dir)
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

        ok = download_pdf(t["href"], dest)
        if ok:
            downloaded += 1
            log_lines.append(f"{datetime.datetime.now().isoformat()}  OK       {dest}")
        else:
            failed += 1
            src = BASE_URL + t["href"] if t["href"].startswith("/") else t["href"]
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
