#!/usr/bin/env python3
# download-healdsburg-agendas.py
# Download Healdsburg, CA municipal agendas and minutes posted in the past N days.
#
# USAGE:
#   python3 scripts/download-healdsburg-agendas.py [options]
#
# REQUIREMENTS:
#   - Python 3.8+
#   - pip install beautifulsoup4
#
# WHAT IT DOES:
#   1. Fetches the Healdsburg AgendaCenter main page to discover all boards
#      (catID → board name) and their available years
#   2. Gets an anti-forgery token (required for the AJAX year-listing API)
#   3. POSTs to /AgendaCenter/UpdateCategoryList for each board × year combination
#      that overlaps the date window
#   4. Parses meeting rows, filters by cutoff date
#   5. Downloads agenda and/or minutes PDFs to beat-archive/healdsburg-agendas/YYYY-MM/
#   6. Saves a shortcut (.url) to the legacy Granicus recording archive
#      (pre-July 2025 recordings at healdsburgca.iqm2.com)
#   7. Appends a download log to beat-archive/healdsburg-agendas/download-log.txt
#
# SITE NOTES:
#   Platform:  CivicPlus CivicEngage AgendaCenter
#   Agendas:   /AgendaCenter/ViewFile/Agenda/_MMDDYYYY-{id}  (direct PDF download)
#   Minutes:   /AgendaCenter/ViewFile/Minutes/_MMDDYYYY-{id} (direct PDF download)
#   AJAX API:  POST /AgendaCenter/UpdateCategoryList  {year, catID}
#              Headers: RequestVerificationToken (from GET /antiforgery)
#   Recordings: Healdsburg moved to CivicPlus in July 2025; pre-July 2025 recordings
#               are on Granicus IQM2 at https://healdsburgca.iqm2.com/Citizens/Media.aspx
#               The CivicPlus AgendaCenter does not host recording links.

import argparse
import datetime
import http.cookiejar
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

try:
    from bs4 import BeautifulSoup
except ImportError:
    print("ERROR: beautifulsoup4 is not installed.\n  pip install beautifulsoup4",
          file=sys.stderr)
    sys.exit(1)

BASE_URL = "https://www.healdsburg.gov"
AGENDA_CENTER_URL = f"{BASE_URL}/agendacenter"
ANTIFORGERY_URL = f"{BASE_URL}/antiforgery"
UPDATE_CAT_URL = f"{BASE_URL}/AgendaCenter/UpdateCategoryList"
IQM2_MEDIA_URL = "https://healdsburgca.iqm2.com/Citizens/Media.aspx"

OUTPUT_DIR = "beat-archive/healdsburg-agendas"
DAYS_BACK = 4
DELAY_SECONDS = 1.0

UA = ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36")

# Matches the anchor id="_MMDDYYYY-{docID}" embedded in each meeting row
_ROW_ID_RE = re.compile(r"_(\d{2})(\d{2})(\d{4})-(\d+)")


def make_opener():
    """Return a urllib opener with cookie support."""
    jar = http.cookiejar.CookieJar()
    return urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar))


def fetch_html(opener, url, *, data=None, extra_headers=None, timeout=30):
    """GET or POST url; return decoded HTML string or None on error."""
    headers = {"User-Agent": UA}
    if extra_headers:
        headers.update(extra_headers)
    req = urllib.request.Request(url, data=data, headers=headers)
    try:
        with opener.open(req, timeout=timeout) as r:
            raw = r.read()
            charset = r.headers.get_content_charset() or "utf-8"
            return raw.decode(charset, errors="replace")
    except urllib.error.HTTPError as e:
        print(f"  HTTP {e.code} — {url}", file=sys.stderr)
        return None
    except urllib.error.URLError as e:
        print(f"  ERROR fetching {url}: {e}", file=sys.stderr)
        return None


def download_binary(opener, url, dest_path):
    """Download url to dest_path; return True on success."""
    full_url = url if url.startswith("http") else BASE_URL + url
    req = urllib.request.Request(full_url, headers={"User-Agent": UA})
    try:
        with opener.open(req, timeout=60) as r:
            with open(dest_path, "wb") as f:
                f.write(r.read())
        return True
    except Exception as e:
        print(f"  WARNING: {e} — {full_url}", file=sys.stderr)
        return False


def get_antiforgery_token(opener):
    """Fetch a fresh anti-forgery token from the CivicPlus endpoint."""
    html = fetch_html(opener, ANTIFORGERY_URL, timeout=15)
    if not html:
        return None
    try:
        return json.loads(html)["token"]
    except Exception:
        return None


def parse_boards(html):
    """
    Parse the main AgendaCenter page and return a dict:
      {catID: {"name": str, "years": [int, ...]}}  (years sorted descending)
    Board names and catIDs come from the aria-labels on year-selector links,
    e.g. aria-label="City Council 2026" href="javascript:changeYear(2026, 7, 'a0')"
    """
    soup = BeautifulSoup(html, "html.parser")
    boards = {}
    for a in soup.find_all("a", href=re.compile(r"changeYear")):
        aria = a.get("aria-label", "")
        href = a.get("href", "")
        m = re.search(r"changeYear\((\d+),\s*(\d+)", href)
        if not (m and aria):
            continue
        year = int(m.group(1))
        catid = int(m.group(2))
        board_name = re.sub(r"\s+\d{4}$", "", aria).strip()
        if catid not in boards:
            boards[catid] = {"name": board_name, "years": []}
        if year not in boards[catid]["years"]:
            boards[catid]["years"].append(year)
    for info in boards.values():
        info["years"].sort(reverse=True)
    return boards


def parse_meetings(html):
    """
    Parse an AgendaCenter HTML fragment and return a list of dicts:
      {date, doc_id, title, agenda_url, minutes_url}
    """
    soup = BeautifulSoup(html, "html.parser")
    meetings = []
    seen_ids = set()

    for row in soup.find_all("tr", class_="catAgendaRow"):
        # Extract document ID and meeting date from anchor id="_MMDDYYYY-{docID}"
        anchor = row.find("a", id=_ROW_ID_RE)
        if not anchor:
            continue
        m = _ROW_ID_RE.search(anchor.get("id", ""))
        if not m:
            continue
        doc_id = m.group(4)
        if doc_id in seen_ids:
            continue
        seen_ids.add(doc_id)

        try:
            meeting_date = datetime.date(
                int(m.group(3)), int(m.group(1)), int(m.group(2)))
        except ValueError:
            continue

        agenda_link = row.find("a", href=re.compile(r"ViewFile/Agenda", re.I))
        title = agenda_link.get_text(" ", strip=True) if agenda_link else ""
        agenda_url = agenda_link["href"] if agenda_link else None

        minutes_td = row.find("td", class_="minutes")
        minutes_link = (
            minutes_td.find("a", href=re.compile(r"ViewFile/Minutes", re.I))
            if minutes_td else None
        )
        minutes_url = minutes_link["href"] if minutes_link else None

        meetings.append({
            "date": meeting_date,
            "doc_id": doc_id,
            "title": title,
            "agenda_url": agenda_url,
            "minutes_url": minutes_url,
        })

    return meetings


def slugify(text):
    text = text.lower().strip()
    text = re.sub(r"[/\\]", "-", text)
    text = re.sub(r"[^\w\s-]", "", text)
    text = re.sub(r"[\s_]+", "-", text)
    text = re.sub(r"-{2,}", "-", text)
    return text.strip("-")[:60]


def save_url_shortcut(url, path):
    """Write a .url shortcut file."""
    with open(path, "w") as f:
        f.write(f"[InternetShortcut]\nURL={url}\n")


def years_needed(cutoff):
    """Return the list of calendar years that overlap [cutoff, today]."""
    today = datetime.date.today()
    return list(range(today.year, cutoff.year - 1, -1))


def main():
    parser = argparse.ArgumentParser(
        description="Download Healdsburg, CA municipal agendas and minutes "
                    "posted in the past N days."
    )
    parser.add_argument("--days", type=int, default=DAYS_BACK, metavar="N",
                        help=f"Look back N days (default: {DAYS_BACK})")
    parser.add_argument("--output-dir", default=OUTPUT_DIR, metavar="DIR",
                        help=f"Destination directory (default: {OUTPUT_DIR})")
    parser.add_argument("--dry-run", action="store_true",
                        help="List matching items without downloading")
    parser.add_argument("--board", metavar="NAME",
                        help="Only process boards whose name contains NAME (case-insensitive)")
    parser.add_argument("--no-agendas", action="store_true",
                        help="Skip agenda PDFs")
    parser.add_argument("--no-minutes", action="store_true",
                        help="Skip minutes PDFs")
    parser.add_argument("--no-video", action="store_true",
                        help="Skip saving the legacy recording archive shortcut")
    args = parser.parse_args()

    now = datetime.datetime.now()
    if (now.weekday() == 5 and now.hour >= 18) or (now.weekday() == 6 and now.hour < 12):  # Saturday night, Sunday morning
        print("Skipping — no downloads on Saturday nights or Sunday mornings.")
        sys.exit(0)

    cutoff = datetime.date.today() - datetime.timedelta(days=args.days)
    needed_years = years_needed(cutoff)

    print(f"Cutoff date  : {cutoff}  ({args.days} days back)")
    print(f"Years        : {needed_years}")
    print(f"Agenda center: {AGENDA_CENTER_URL}")
    print(f"Output dir   : {args.output_dir}")
    print()

    opener = make_opener()

    # Discover all boards from the main page
    print("Fetching agenda center...")
    main_html = fetch_html(opener, AGENDA_CENTER_URL)
    if not main_html:
        print("ERROR: Could not fetch the agenda center.", file=sys.stderr)
        sys.exit(1)

    boards = parse_boards(main_html)
    if not boards:
        print("WARNING: No boards found — page structure may have changed.",
              file=sys.stderr)
        sys.exit(1)
    print(f"Found {len(boards)} board(s).")

    if args.board:
        filter_name = args.board.lower()
        boards = {k: v for k, v in boards.items()
                  if filter_name in v["name"].lower()}
        print(f"Filtered to {len(boards)} board(s) matching '{args.board}'.")
    print()

    # Anti-forgery token required for AJAX year requests
    token = get_antiforgery_token(opener)
    if not token:
        print("ERROR: Could not get anti-forgery token.", file=sys.stderr)
        sys.exit(1)

    # Collect all in-window meetings across all boards
    all_meetings = []  # [(board_name, meeting_dict), ...]

    for catid, board_info in sorted(boards.items(), key=lambda x: x[1]["name"]):
        board_name = board_info["name"]
        board_years = board_info["years"]

        fetch_years = [y for y in needed_years if y in board_years]
        if not fetch_years:
            continue

        for year in fetch_years:
            post_data = urllib.parse.urlencode(
                {"year": year, "catID": catid}).encode()
            frag_html = fetch_html(
                opener, UPDATE_CAT_URL,
                data=post_data,
                extra_headers={
                    "Content-Type": "application/x-www-form-urlencoded",
                    "RequestVerificationToken": token,
                    "X-Requested-With": "XMLHttpRequest",
                    "Referer": AGENDA_CENTER_URL,
                },
                timeout=20,
            )
            if not frag_html:
                print(f"  WARNING: could not fetch {board_name} {year}",
                      file=sys.stderr)
                time.sleep(0.5)
                continue

            meetings = parse_meetings(frag_html)
            in_window = [m for m in meetings if m["date"] >= cutoff]
            all_meetings.extend((board_name, m) for m in in_window)
            time.sleep(DELAY_SECONDS * 0.5)

    all_meetings.sort(key=lambda x: (x[1]["date"], x[0]))

    if not all_meetings:
        print("No meetings found within the date window.")
    elif args.dry_run:
        print(f"{'Board':<45} {'Date':<12} {'Agenda':<7} {'Minutes':<7}")
        print("-" * 75)
        for board_name, m in all_meetings:
            date_s = str(m["date"])
            has_a = "yes" if (m["agenda_url"] and not args.no_agendas) else "no"
            has_m = "yes" if (m["minutes_url"] and not args.no_minutes) else "no"
            print(f"{board_name[:44]:<45} {date_s:<12} {has_a:<7} {has_m:<7}")
        print(f"\n{len(all_meetings)} meeting(s). Re-run without --dry-run to download.")
    else:
        os.makedirs(args.output_dir, exist_ok=True)
        log_path = os.path.join(args.output_dir, "download-log.txt")
        log_lines = []
        dl_ok = dl_skip = dl_fail = 0

        for board_name, m in all_meetings:
            date_s = str(m["date"])
            print(f"[{date_s}] {board_name}")

            month_dir = os.path.join(args.output_dir, m["date"].strftime("%Y-%m"))
            os.makedirs(month_dir, exist_ok=True)

            date_str = m["date"].strftime("%Y-%m-%d")
            board_slug = slugify(board_name)

            doc_types = []
            if not args.no_agendas and m["agenda_url"]:
                doc_types.append(("agenda", m["agenda_url"]))
            if not args.no_minutes and m["minutes_url"]:
                doc_types.append(("minutes", m["minutes_url"]))

            for doc_type, url in doc_types:
                dest = os.path.join(
                    month_dir, f"{date_str}-{board_slug}-{doc_type}.pdf")
                if os.path.exists(dest):
                    print(f"  skip (exists)  {os.path.basename(dest)}")
                    dl_skip += 1
                    continue

                print(f"  downloading    {os.path.basename(dest)}")
                if download_binary(opener, url, dest):
                    dl_ok += 1
                    log_lines.append(
                        f"{datetime.datetime.now().isoformat()}  OK      {dest}")
                else:
                    dl_fail += 1
                    log_lines.append(
                        f"{datetime.datetime.now().isoformat()}  FAIL    {url}")
                    if os.path.exists(dest):
                        os.remove(dest)
                time.sleep(DELAY_SECONDS)

        if log_lines:
            with open(log_path, "a") as f:
                f.write("\n".join(log_lines) + "\n")

        print()
        print(f"Downloaded: {dl_ok}  Skipped: {dl_skip}  Failed: {dl_fail}")
        if dl_ok + dl_skip:
            print(f"Files in:   {args.output_dir}")
        if log_lines:
            print(f"Log:        {log_path}")

    # Legacy recording archive shortcut
    if not args.no_video:
        if args.dry_run:
            print(f"\n[dry] Would save legacy recording archive shortcut")
            print(f"      → recordings/healdsburg-meeting-recordings-archive.url")
            print(f"      (Pre-July 2025 recordings: {IQM2_MEDIA_URL})")
        else:
            rec_dir = os.path.join(args.output_dir, "recordings")
            os.makedirs(rec_dir, exist_ok=True)
            shortcut = os.path.join(
                rec_dir, "healdsburg-meeting-recordings-archive.url")
            if not os.path.exists(shortcut):
                save_url_shortcut(IQM2_MEDIA_URL, shortcut)
                print(f"\nSaved: {shortcut}")
                print("  (Pre-July 2025 City Council recordings via Granicus IQM2)")


if __name__ == "__main__":
    main()


# --- Tips ---
#
# 1. Preview without downloading:
#    python3 scripts/download-healdsburg-agendas.py --dry-run
#
# 2. Narrow to one board:
#    python3 scripts/download-healdsburg-agendas.py --board "City Council"
#
# 3. Change the lookback window:
#    python3 scripts/download-healdsburg-agendas.py --days 7
#
# 4. Save files somewhere else:
#    python3 scripts/download-healdsburg-agendas.py --output-dir ~/Downloads/healdsburg
#
# 5. Agendas only (skip minutes):
#    python3 scripts/download-healdsburg-agendas.py --no-minutes
#
# 6. Skip the recording archive shortcut:
#    python3 scripts/download-healdsburg-agendas.py --no-video
#
# 7. Run on a schedule (cron — 7 AM daily):
#    0 7 * * * cd /path/to/repo && python3 scripts/download-healdsburg-agendas.py
#
# NOTE ON RECORDINGS:
#   Healdsburg transitioned from Granicus IQM2 to CivicPlus in July 2025.
#   Pre-July 2025 meeting recordings are at:
#     https://healdsburgca.iqm2.com/Citizens/Media.aspx
#   The current CivicPlus AgendaCenter does not host recording links.
