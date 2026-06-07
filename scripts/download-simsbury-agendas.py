#!/usr/bin/env python3
"""Download municipal meeting agendas, minutes, and recording index from Simsbury, CT.

Simsbury uses CivicPlus AgendaCenter (simsbury-ct.gov/AgendaCenter).
The current year is pre-loaded in the main page HTML; other years are fetched
via a POST to /AgendaCenter/UpdateCategoryList.

Document URLs:
  /AgendaCenter/ViewFile/Agenda/_MMDDYYYY-NNN  → agenda PDF
  /AgendaCenter/ViewFile/Minutes/_MMDDYYYY-NNN → minutes PDF

Recording/media links (YouTube or Zoom) are saved to recording-index.csv
when --recordings is passed.
"""

import argparse
import csv
import datetime
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

BASE_URL = "https://simsbury-ct.gov"
AGENDA_CENTER_URL = f"{BASE_URL}/AgendaCenter"
UPDATE_URL = f"{BASE_URL}/AgendaCenter/UpdateCategoryList"
OUTPUT_DIR = "beat-archive/simsbury-agendas"
DAYS_BACK = 4
MIN_DATE = datetime.date(2018, 1, 1)

_UA = "Simsbury-Agendas-Downloader/1.0 (journalism research)"

_HEADERS = {
    "User-Agent": _UA,
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
    "Referer": AGENDA_CENTER_URL,
}


# --- HTTP helpers ---

def fetch_html(url, post_data=None, retries=3):
    headers = dict(_HEADERS)
    if post_data:
        headers["Content-Type"] = "application/x-www-form-urlencoded"
        headers["X-Requested-With"] = "XMLHttpRequest"
    req = urllib.request.Request(url, data=post_data, headers=headers)
    for attempt in range(retries + 1):
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                return r.read().decode("utf-8", errors="replace")
        except (urllib.error.URLError, TimeoutError, OSError) as e:
            if attempt < retries:
                time.sleep(3 * (attempt + 1))
            else:
                print(f"  Error fetching {url}: {e}", file=sys.stderr)
    return None


def download_file(path, dest_path, retries=3):
    url = BASE_URL + path if path.startswith("/") else path
    url = url.split("?")[0]  # strip ?html=true so server returns PDF
    req = urllib.request.Request(url, headers={"User-Agent": _UA})
    for attempt in range(retries + 1):
        try:
            with urllib.request.urlopen(req, timeout=60) as r:
                data = r.read()
            os.makedirs(os.path.dirname(dest_path), exist_ok=True)
            with open(dest_path, "wb") as f:
                f.write(data)
            return True
        except (urllib.error.URLError, TimeoutError, OSError) as e:
            if attempt < retries:
                time.sleep(3 * (attempt + 1))
            else:
                print(f"  Error downloading {url}: {e}", file=sys.stderr)
    return False


# --- HTML parsing ---

def parse_boards(html):
    """Return [(cat_id, board_name, sorted_available_years)] from main page HTML."""
    boards = []
    for cat_id, name in re.findall(
        r'aria-controls="category-panel-(\d+)"[^>]*>\s*([^<]+)\s*</h2>', html
    ):
        sec_m = re.search(
            rf'id="section{cat_id}".*?<ul class="years">(.*?)</ul>',
            html, re.DOTALL,
        )
        years = set()
        if sec_m:
            years_html = sec_m.group(0)
            years.update(int(y) for y in re.findall(r'changeYear\((\d{4})', years_html))
            cur_m = re.search(r'class=current[^>]*>[^<]*?(\d{4})', years_html)
            if cur_m:
                years.add(int(cur_m.group(1)))
        boards.append((int(cat_id), name.strip(), sorted(years)))
    return boards


def _parse_chunk(chunk):
    """Parse catAgendaRow entries from an HTML chunk (panel or UpdateCategoryList fragment)."""
    rows = re.findall(r'<tr[^>]+class="catAgendaRow"[^>]*>(.*?)</tr>', chunk, re.DOTALL)
    items = []
    for row in rows:
        date_m = re.search(r'aria-label="Agenda for ([^"]+)"', row)
        if not date_m:
            continue
        try:
            meeting_date = datetime.datetime.strptime(
                date_m.group(1).strip(), "%B %d, %Y"
            ).date()
        except ValueError:
            continue

        agenda_m = re.search(r'href="(/AgendaCenter/ViewFile/Agenda/[^"?]+)', row)
        minutes_m = re.search(r'href="(/AgendaCenter/ViewFile/Minutes/[^"?]+)', row)
        title_m = re.search(r'<p[^>]*>.*?<a[^>]+>\s*([^<]+)\s*</a>', row, re.DOTALL)
        media_m = re.search(r'<td class="media">(.*?)</td>', row, re.DOTALL)

        media_url = None
        if media_m:
            mu = re.search(r'href="([^"]+)"', media_m.group(1))
            if mu:
                media_url = mu.group(1).strip()

        items.append({
            "date": meeting_date,
            "agenda_url": agenda_m.group(1) if agenda_m else None,
            "minutes_url": minutes_m.group(1) if minutes_m else None,
            "title": title_m.group(1).strip() if title_m else "",
            "media_url": media_url,
        })
    return items


def parse_rows_from_main(html, cat_id):
    """Parse rows for cat_id from main AgendaCenter page HTML."""
    marker = f'id="category-panel-{cat_id}"'
    start = html.find(marker)
    if start < 0:
        return []
    end = html.find('id="category-panel-', start + 1)
    chunk = html[start: end if end > 0 else len(html)]
    return _parse_chunk(chunk)


def parse_rows_from_fragment(html):
    """Parse rows from an UpdateCategoryList response fragment."""
    return _parse_chunk(html)


# --- Filename helpers ---

def slugify(text):
    text = text.lower().strip()
    text = re.sub(r"[/\\]", "-", text)
    text = re.sub(r"\s+-\s+", "-", text)
    text = re.sub(r"[^\w\s-]", "", text)
    text = re.sub(r"[\s_]+", "-", text)
    text = re.sub(r"-{2,}", "-", text)
    return text.strip("-")[:60]


def doc_id_from_url(url):
    """Extract numeric ID from /AgendaCenter/ViewFile/{Type}/_MMDDYYYY-NNN."""
    m = re.search(r"-(\d+)$", url.rstrip("/"))
    return m.group(1) if m else "0"


def dest_path(board_name, doc_type, meeting_date, doc_url, output_dir):
    month_dir = os.path.join(output_dir, meeting_date.strftime("%Y-%m"))
    doc_id = doc_id_from_url(doc_url)
    filename = (
        f"{meeting_date.isoformat()}_{slugify(board_name)}_{doc_type}_{doc_id}.pdf"
    )
    return os.path.join(month_dir, filename)


# --- Main ---

def main():
    ap = argparse.ArgumentParser(
        description="Download Simsbury CT municipal meeting agendas and minutes (CivicPlus AgendaCenter)"
    )
    ap.add_argument("--days", type=int, default=DAYS_BACK,
                    help=f"Days back to fetch (default: {DAYS_BACK})")
    ap.add_argument("--ahead", type=int, default=90,
                    help="Days ahead for upcoming agendas (default: 90)")
    ap.add_argument("--all", action="store_true",
                    help=f"Fetch all docs back to {MIN_DATE}")
    ap.add_argument("--dry-run", action="store_true",
                    help="List what would be downloaded without downloading")
    ap.add_argument("--board", metavar="NAME",
                    help="Only process boards whose name contains NAME (case-insensitive)")
    ap.add_argument("--no-agendas", action="store_true", help="Skip agenda PDFs")
    ap.add_argument("--no-minutes", action="store_true", help="Skip minutes PDFs")
    ap.add_argument("--recordings", action="store_true",
                    help="Save recording/media URLs to recording-index.csv")
    ap.add_argument("--verbose", "-v", action="store_true")
    ap.add_argument("--output-dir", default=OUTPUT_DIR,
                    help=f"Output directory (default: {OUTPUT_DIR})")
    args = ap.parse_args()

    today = datetime.date.today()
    cutoff_back = MIN_DATE if args.all else today - datetime.timedelta(days=args.days)
    cutoff_ahead = today + datetime.timedelta(days=args.ahead)

    os.makedirs(args.output_dir, exist_ok=True)

    print(f"Fetching {AGENDA_CENTER_URL} ...")
    main_html = fetch_html(AGENDA_CENTER_URL)
    if not main_html:
        print("ERROR: Could not fetch AgendaCenter page.", file=sys.stderr)
        sys.exit(1)

    all_boards = parse_boards(main_html)
    if not all_boards:
        print("ERROR: No boards found — page structure may have changed.", file=sys.stderr)
        sys.exit(1)

    print(f"Found {len(all_boards)} boards.")

    if args.board:
        filt = args.board.lower()
        all_boards = [(cid, name, yrs) for cid, name, yrs in all_boards if filt in name.lower()]
        if not all_boards:
            print(f"No boards match '{args.board}'. Available boards:")
            for _, name, _ in parse_boards(main_html):
                print(f"  {name}")
            return
        print(f"Filtered to {len(all_boards)} board(s) matching '{args.board}'.")

    current_year = today.year
    years_needed = set(range(cutoff_back.year, cutoff_ahead.year + 1))

    total_dl = total_skip = total_fail = 0
    rec_rows = []

    for cat_id, board_name, board_years in all_boards:
        fetch_years = sorted(years_needed & set(board_years))
        if not fetch_years:
            continue

        if args.verbose:
            print(f"\n{board_name} (catID={cat_id}, years={fetch_years})")
        else:
            print(f"{board_name}...")

        board_items = []

        for year in fetch_years:
            if year == current_year:
                rows = parse_rows_from_main(main_html, cat_id)
                if not rows:
                    post = urllib.parse.urlencode({"year": year, "catID": cat_id}).encode()
                    frag = fetch_html(UPDATE_URL, post_data=post)
                    if frag:
                        rows = parse_rows_from_fragment(frag)
                    time.sleep(0.3)
            else:
                post = urllib.parse.urlencode({"year": year, "catID": cat_id}).encode()
                frag = fetch_html(UPDATE_URL, post_data=post)
                rows = parse_rows_from_fragment(frag) if frag else []
                time.sleep(0.3)

            board_items.extend(rows)

        in_range = [
            item for item in board_items
            if cutoff_back <= item["date"] <= cutoff_ahead
        ]

        if args.verbose and not in_range:
            print(f"  (no meetings in range)")
            continue

        for item in sorted(in_range, key=lambda x: x["date"], reverse=True):
            d = item["date"]
            title = item["title"] or "(no title)"

            if item["media_url"] and args.recordings:
                rec_rows.append((board_name, str(d), title, item["media_url"]))

            # Agenda
            if not args.no_agendas and item["agenda_url"]:
                out = dest_path(board_name, "agenda", d, item["agenda_url"], args.output_dir)
                if os.path.exists(out):
                    total_skip += 1
                    if args.verbose:
                        print(f"  skip  {os.path.basename(out)}")
                elif args.dry_run:
                    print(f"  [dry] {os.path.basename(out)}")
                    total_dl += 1
                else:
                    if args.verbose:
                        print(f"  dl    {os.path.basename(out)}")
                    if download_file(item["agenda_url"], out):
                        total_dl += 1
                        time.sleep(0.5)
                    else:
                        total_fail += 1

            # Minutes
            if not args.no_minutes and item["minutes_url"]:
                out = dest_path(board_name, "minutes", d, item["minutes_url"], args.output_dir)
                if os.path.exists(out):
                    total_skip += 1
                    if args.verbose:
                        print(f"  skip  {os.path.basename(out)}")
                elif args.dry_run:
                    print(f"  [dry] {os.path.basename(out)}")
                    total_dl += 1
                else:
                    if args.verbose:
                        print(f"  dl    {os.path.basename(out)}")
                    if download_file(item["minutes_url"], out):
                        total_dl += 1
                        time.sleep(0.5)
                    else:
                        total_fail += 1

        if not args.dry_run and not args.verbose:
            n = sum(
                (1 if not args.no_agendas and item["agenda_url"] else 0) +
                (1 if not args.no_minutes and item["minutes_url"] else 0)
                for item in in_range
            )
            print(f"  {len(in_range)} meetings, up to {n} docs")

    # Recording index
    if args.recordings and rec_rows:
        csv_path = os.path.join(args.output_dir, "recording-index.csv")
        existing = set()
        if os.path.exists(csv_path):
            with open(csv_path, newline="", encoding="utf-8") as f:
                for row in csv.DictReader(f):
                    existing.add(row["media_url"])
        new_rows = [r for r in rec_rows if r[3] not in existing]
        if new_rows:
            write_header = not os.path.exists(csv_path)
            with open(csv_path, "a", newline="", encoding="utf-8") as f:
                w = csv.writer(f)
                if write_header:
                    w.writerow(["board", "meeting_date", "title", "media_url"])
                w.writerows(new_rows)
            print(f"\nAdded {len(new_rows)} recording links → {csv_path}")
        else:
            print("\nNo new recording links.")

    label = "Would download" if args.dry_run else "Downloaded"
    print(f"\n{label}: {total_dl}  skipped: {total_skip}  failed: {total_fail}")
    if not args.dry_run and total_dl:
        print(f"Files in: {args.output_dir}")


if __name__ == "__main__":
    main()
