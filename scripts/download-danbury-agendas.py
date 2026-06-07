#!/usr/bin/env python3
# download-danbury-agendas.py
# Download Danbury CT municipal agendas, minutes, and meeting recordings
# posted in the past N days.
#
# USAGE:
#   python3 scripts/download-danbury-agendas.py [options]
#
# REQUIREMENTS:
#   - Python 3.8+
#   - pip install beautifulsoup4
#
# WHAT IT DOES:
#   1. Fetches the Danbury CT Agenda Center page (all data is inline HTML)
#   2. For each board section, parses meeting rows (date, agenda, minutes,
#      recording links)
#   3. Filters rows whose meeting date falls within the lookback window
#   4. Downloads PDFs directly from /AgendaCenter/ViewFile/(Agenda|Minutes)/
#   5. Saves recording links as .url shortcut files (Windows Internet Shortcut
#      format — opens in any browser on any OS)
#   6. Saves files to beat-archive/danbury-agendas/YYYY-MM/
#   7. Appends a download log to beat-archive/danbury-agendas/download-log.txt
#
# SITE STRUCTURE (CivicPlus CivicEngage):
#   Hub:     https://www.danbury-ct.gov/agendacenter
#   Agenda:  https://www.danbury-ct.gov/AgendaCenter/ViewFile/Agenda/_MMDDYYYY-ID
#   Minutes: https://www.danbury-ct.gov/AgendaCenter/ViewFile/Minutes/_MMDDYYYY-ID
#
# RECORDING LINKS (embedded in the same hub page):
#   Granicus: https://danbury.granicus.com/player/clip/{ID}?redirect=true
#   Zoom:     https://us02web.zoom.us/rec/share/...
#   Both are saved as .url shortcuts (not downloaded).
#
# NOTES:
#   - No bot protection; plain urllib works.
#   - The hub page embeds all meeting rows for all boards inline (no AJAX needed).
#   - The date is encoded in every ViewFile URL as _MMDDYYYY.
#   - ViewFile URLs serve PDFs directly (content-type: application/pdf).
#   - Granicus recordings are full meeting videos; Zoom links are shared recordings.

import argparse
import datetime
import os
import re
import sys
import time
import urllib.error
import urllib.request

try:
    from bs4 import BeautifulSoup
except ImportError:
    print("ERROR: beautifulsoup4 is not installed.\n  pip install beautifulsoup4",
          file=sys.stderr)
    sys.exit(1)

BASE_URL = "https://www.danbury-ct.gov"
HUB_URL = f"{BASE_URL}/agendacenter"
OUTPUT_DIR = "beat-archive/danbury-agendas"
DAYS_BACK = 4
DELAY_SECONDS = 0.8

UA = "Danbury-CT-Agendas-Downloader/1.0 (journalism research)"

# Matches _MMDDYYYY-meetingID in ViewFile anchor IDs
_DATE_ID_RE = re.compile(r"^_(\d{2})(\d{2})(\d{4})-(\d+)$")

# Recording link patterns
_GRANICUS_RE = re.compile(r"danbury\.granicus\.com/", re.IGNORECASE)
_ZOOM_RE = re.compile(r"zoom\.us/rec/", re.IGNORECASE)


def fetch_html(url):
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            raw = r.read()
            charset = r.headers.get_content_charset() or "utf-8"
            return raw.decode(charset, errors="replace")
    except urllib.error.HTTPError as e:
        print(f"  HTTP {e.code} — {url}", file=sys.stderr)
        return None
    except urllib.error.URLError as e:
        print(f"  ERROR fetching {url}: {e}", file=sys.stderr)
        return None


def download_binary(url, dest_path):
    full_url = url if url.startswith("http") else BASE_URL + url
    req = urllib.request.Request(full_url, headers={"User-Agent": UA})
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            with open(dest_path, "wb") as f:
                f.write(r.read())
        return True
    except Exception as e:
        print(f"  WARNING: {e} — {full_url}", file=sys.stderr)
        return False


def save_url_shortcut(url, dest_path):
    with open(dest_path, "w", encoding="utf-8") as f:
        f.write(f"[InternetShortcut]\nURL={url}\n")
    return True


def slugify(text):
    text = text.lower().strip()
    text = re.sub(r"[/\\]", "-", text)
    text = re.sub(r"[^\w\s-]", "", text)
    text = re.sub(r"[\s_]+", "-", text)
    text = re.sub(r"-{2,}", "-", text)
    return text.strip("-")[:60]


def parse_meetings(html):
    """
    Parse all meeting rows from the hub page.
    Returns a list of dicts:
      {board, meeting_date, meeting_id, agenda_url, minutes_url,
       recording_urls}
    meeting_date is a datetime.date or None.
    agenda_url and minutes_url are absolute URL strings or None.
    recording_urls is a list of absolute URL strings (Granicus or Zoom).
    """
    soup = BeautifulSoup(html, "html.parser")
    meetings = []

    for span in soup.find_all("span", id=re.compile(r"^section\d+$")):
        h2 = span.find_previous_sibling("h2")
        board = h2.get_text(" ", strip=True) if h2 else "Unknown"

        for row in span.find_all("tr", class_="catAgendaRow"):
            # Date and meeting ID from the named anchor: id="_MMDDYYYY-NNN"
            anchor = row.find("a", id=_DATE_ID_RE)
            meeting_date = None
            meeting_id = None
            if anchor:
                m = _DATE_ID_RE.match(anchor["id"])
                if m:
                    mm, dd, yyyy, mid = m.groups()
                    meeting_id = mid
                    try:
                        meeting_date = datetime.date(int(yyyy), int(mm), int(dd))
                    except ValueError:
                        pass

            # Agenda URL (always the <a> in the first td that isn't the anchor)
            agenda_url = None
            first_td = row.find("td")
            if first_td:
                for a in first_td.find_all("a", href=True):
                    if "ViewFile" in a["href"]:
                        href = a["href"]
                        agenda_url = href if href.startswith("http") else BASE_URL + href
                        break

            # Minutes URL (td.minutes may be empty or contain an <a>)
            minutes_url = None
            minutes_td = row.find("td", class_="minutes")
            if minutes_td:
                a = minutes_td.find("a", href=True)
                if a:
                    href = a["href"]
                    minutes_url = href if href.startswith("http") else BASE_URL + href

            # Recording links: Granicus player or Zoom shared recording
            recording_urls = []
            for a in row.find_all("a", href=True):
                href = a["href"]
                if _GRANICUS_RE.search(href) or _ZOOM_RE.search(href):
                    if href not in recording_urls:
                        recording_urls.append(href)

            if agenda_url or minutes_url or recording_urls:
                meetings.append({
                    "board": board,
                    "meeting_date": meeting_date,
                    "meeting_id": meeting_id,
                    "agenda_url": agenda_url,
                    "minutes_url": minutes_url,
                    "recording_urls": recording_urls,
                })

    return meetings


def recording_label(url):
    if _GRANICUS_RE.search(url):
        m = re.search(r"/clip/(\d+)", url)
        return f"Granicus clip {m.group(1)}" if m else "Granicus"
    if _ZOOM_RE.search(url):
        return "Zoom recording"
    return "recording"


def main():
    parser = argparse.ArgumentParser(
        description="Download Danbury CT municipal agendas, minutes, and "
                    "recording shortcuts posted in the past N days."
    )
    parser.add_argument("--days", type=int, default=DAYS_BACK, metavar="N",
                        help=f"Look back N days (default: {DAYS_BACK})")
    parser.add_argument("--output-dir", default=OUTPUT_DIR, metavar="DIR",
                        help=f"Destination directory (default: {OUTPUT_DIR})")
    parser.add_argument("--dry-run", action="store_true",
                        help="List matching items without downloading")
    parser.add_argument("--include-undated", action="store_true",
                        help="Also process rows where no meeting date could be parsed")
    parser.add_argument("--board", metavar="NAME",
                        help="Only process boards whose name contains NAME (case-insensitive)")
    parser.add_argument("--no-minutes", action="store_true",
                        help="Skip minutes, download agendas only")
    parser.add_argument("--no-agendas", action="store_true",
                        help="Skip agendas, download minutes only")
    parser.add_argument("--no-video", action="store_true",
                        help="Skip recording links (Granicus/Zoom .url shortcuts)")
    args = parser.parse_args()

    if datetime.date.today().weekday() in (6, 0):  # Sunday, Monday
        print("Skipping — no downloads on Sunday or Monday.")
        sys.exit(0)

    cutoff = datetime.date.today() - datetime.timedelta(days=args.days)

    print(f"Date window : {cutoff} to {datetime.date.today()}  ({args.days} days back)")
    print(f"Hub page    : {HUB_URL}")
    print(f"Output dir  : {args.output_dir}")
    print()

    print("Fetching hub page...")
    hub_html = fetch_html(HUB_URL)
    if not hub_html:
        print("ERROR: Could not fetch the hub page.", file=sys.stderr)
        sys.exit(1)

    all_meetings = parse_meetings(hub_html)
    if not all_meetings:
        print("WARNING: No meeting rows found — the page structure may have changed.",
              file=sys.stderr)
        sys.exit(1)

    print(f"Found {len(all_meetings)} meeting row(s) across all boards.")

    if args.board:
        filter_name = args.board.lower()
        all_meetings = [m for m in all_meetings if filter_name in m["board"].lower()]
        print(f"Filtered to {len(all_meetings)} row(s) matching '{args.board}'.")

    # Filter by date window
    in_window = []
    no_date_count = 0
    for mtg in all_meetings:
        if mtg["meeting_date"] is None:
            no_date_count += 1
            if args.include_undated:
                in_window.append(mtg)
        elif mtg["meeting_date"] >= cutoff:
            in_window.append(mtg)

    in_window.sort(key=lambda x: (x["meeting_date"] or datetime.date.min), reverse=True)

    undated_note = (
        f"  (+{no_date_count} undated included via --include-undated)"
        if args.include_undated and no_date_count
        else f"  ({no_date_count} undated skipped; use --include-undated to add)"
        if no_date_count else ""
    )

    doc_count = sum(
        bool(m["agenda_url"]) + bool(m["minutes_url"]) for m in in_window
    )
    rec_count = sum(len(m["recording_urls"]) for m in in_window)

    print(f"Meetings in window  : {len(in_window)}{undated_note}")
    print(f"Documents           : {doc_count} PDF(s)")
    if not args.no_video:
        print(f"Recordings          : {rec_count} link(s) (saved as .url shortcuts)")
    print()

    if not in_window:
        print("No meetings found within the date window.")
        sys.exit(0)

    if args.dry_run:
        print(f"{'Board':<42} {'Date':<12} {'Agenda':<6} {'Min':<4} {'Rec'}")
        print("-" * 72)
        for mtg in in_window:
            date_s = str(mtg["meeting_date"]) if mtg["meeting_date"] else "unknown"
            has_a = "yes" if mtg["agenda_url"] else "no"
            has_m = "yes" if mtg["minutes_url"] else "no"
            rec_n = str(len(mtg["recording_urls"])) if not args.no_video else "-"
            print(f"{mtg['board'][:41]:<42} {date_s:<12} {has_a:<6} {has_m:<4} {rec_n}")

        if not args.no_video and rec_count:
            print()
            print(f"{'Board':<42} {'Date':<12} {'Recording URL'}")
            print("-" * 100)
            for mtg in in_window:
                for url in mtg["recording_urls"]:
                    date_s = str(mtg["meeting_date"]) if mtg["meeting_date"] else "unknown"
                    print(f"{mtg['board'][:41]:<42} {date_s:<12} {url}")

        print(f"\n{doc_count} PDF document(s), {rec_count} recording link(s).")
        print("Re-run without --dry-run to download.")
        return

    os.makedirs(args.output_dir, exist_ok=True)
    log_path = os.path.join(args.output_dir, "download-log.txt")
    log_lines = []
    dl_ok = dl_skip = dl_fail = 0

    for mtg in in_window:
        date_s = str(mtg["meeting_date"]) if mtg["meeting_date"] else "unknown"
        board = mtg["board"]
        board_slug = slugify(board)
        date_str = mtg["meeting_date"].strftime("%Y-%m-%d") if mtg["meeting_date"] else "unknown"
        month_dir_name = mtg["meeting_date"].strftime("%Y-%m") if mtg["meeting_date"] else "unknown"
        month_dir = os.path.join(args.output_dir, month_dir_name)

        print(f"[{date_s}] {board}")

        items = []
        if not args.no_agendas and mtg["agenda_url"]:
            items.append(("agenda", mtg["agenda_url"], False))
        if not args.no_minutes and mtg["minutes_url"]:
            items.append(("minutes", mtg["minutes_url"], False))
        if not args.no_video:
            for i, rec_url in enumerate(mtg["recording_urls"], 1):
                suffix = f"recording-{i}" if len(mtg["recording_urls"]) > 1 else "recording"
                items.append((suffix, rec_url, True))

        for doc_type, url, is_shortcut in items:
            os.makedirs(month_dir, exist_ok=True)
            mid = mtg["meeting_id"] or "0"
            ext = ".url" if is_shortcut else ".pdf"
            dest = os.path.join(month_dir, f"{date_str}-{board_slug}-{mid}-{doc_type}{ext}")

            if os.path.exists(dest):
                print(f"  skip (exists)  {os.path.basename(dest)}")
                dl_skip += 1
                continue

            print(f"  {'saving   ' if is_shortcut else 'downloading'} {os.path.basename(dest)}")

            if is_shortcut:
                ok = save_url_shortcut(url, dest)
            else:
                ok = download_binary(url, dest)
                time.sleep(DELAY_SECONDS)

            if ok:
                dl_ok += 1
                log_lines.append(
                    f"{datetime.datetime.now().isoformat()}  OK      {dest}")
            else:
                dl_fail += 1
                log_lines.append(
                    f"{datetime.datetime.now().isoformat()}  FAIL    {url}")
                if os.path.exists(dest):
                    os.remove(dest)

    if log_lines:
        with open(log_path, "a") as f:
            f.write("\n".join(log_lines) + "\n")

    print()
    print(f"Downloaded/saved: {dl_ok}  Skipped: {dl_skip}  Failed: {dl_fail}")
    if dl_ok + dl_skip:
        print(f"Files in: {args.output_dir}")
    if log_lines:
        print(f"Log:      {log_path}")


if __name__ == "__main__":
    main()


# --- Tips ---
#
# 1. Preview without downloading:
#    python3 scripts/download-danbury-agendas.py --dry-run
#
# 2. Narrow to one board:
#    python3 scripts/download-danbury-agendas.py --board "City Council"
#
# 3. Change the lookback window:
#    python3 scripts/download-danbury-agendas.py --days 7
#
# 4. Save files somewhere else:
#    python3 scripts/download-danbury-agendas.py --output-dir ~/Downloads/danbury
#
# 5. Agendas only (skip minutes):
#    python3 scripts/download-danbury-agendas.py --no-minutes
#
# 6. PDFs only (skip recording shortcuts):
#    python3 scripts/download-danbury-agendas.py --no-video
#
# 7. Open a Granicus .url file:
#    - Double-click it on Windows/macOS, or
#    - xdg-open file.url  (Linux), or
#    - grep URL file.url  (to get the raw link)
#
# 8. Run on a schedule (cron — 7 AM daily):
#    0 7 * * * cd /path/to/repo && python3 scripts/download-danbury-agendas.py
