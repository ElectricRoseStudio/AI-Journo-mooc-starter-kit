#!/usr/bin/env python3
# download-haddam-agendas.py
# Download municipal meeting agendas and minutes from Haddam CT AgendaCenter
# for meetings whose date falls within the past N days.
#
# USAGE:
#   python3 scripts/download-haddam-agendas.py [options]
#
# REQUIREMENTS:
#   - Python 3.6+  (no third-party packages needed)
#   - Internet connection
#
# WHAT IT DOES:
#   1. Fetches the Haddam CT AgendaCenter listing page
#   2. Finds all agendas and minutes whose meeting date falls within the
#      lookback window (note: no "posted date" is exposed by this site —
#      meeting date is used instead)
#   3. Downloads them to beat-archive/haddam-agendas/YYYY-MM/
#   4. Appends a download log to beat-archive/haddam-agendas/download-log.txt
#
#   Video (--include-video or --video-only; off by default — see VIDEO
#   NOTE below): searches the regional Cablecast archive for shows
#   matching a known board name within the same lookback window.
#
# SITE STRUCTURE:
#   Haddam CT uses CivicPlus AgendaCenter. Board sections are collapsible
#   panels; the current year is pre-loaded in the page HTML. Previous years
#   load via a POST to /AgendaCenter/UpdateCategoryList.
#
#   Document URLs:
#     /AgendaCenter/ViewFile/Agenda/_MMDDYYYY-NNNN   → agenda PDF
#     /AgendaCenter/ViewFile/Minutes/_MMDDYYYY-NNNN  → minutes PDF
#
# VIDEO NOTE — No reliable per-meeting video source was found for Haddam.
#   Checked directly: no YouTube/Vimeo/PEG link anywhere on haddam.org.
#   The regional Cablecast (Tightrope Media Systems) instance at
#   reflect-vsctv.cablecast.tv — the same platform already used for
#   Durham/Middlefield/Chester/Essex elsewhere in this repo — does have a
#   "Haddam Meetings" project (ID 10), but its most recent posting is from
#   July 2024 and its most recent actual Board of Selectmen recording is
#   from April 2022 — regular meeting recording appears to have stopped
#   years ago. Video search is included anyway for completeness and left
#   OFF by default (--include-video/--video-only to try it) — expect it
#   to find nothing until/unless Haddam resumes recording meetings
#   somewhere discoverable.

import argparse
import datetime
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

# --- Configuration ---
BASE_URL = "https://www.haddam.org"
AGENDA_CENTER_URL = f"{BASE_URL}/AgendaCenter"
UPDATE_URL = f"{BASE_URL}/AgendaCenter/UpdateCategoryList"
CABLECAST_BASE = "https://reflect-vsctv.cablecast.tv/cablecastapi/v1"
OUTPUT_DIR = "beat-archive/haddam-agendas"
DAYS_BACK = 4
DELAY_SECONDS = 1

UA = "Haddam-Agendas-Downloader/1.0 (journalism research)"


# --- HTTP helpers ---

def fetch_html(url, post_data=None):
    """GET or POST url; return decoded HTML or None on error."""
    req = urllib.request.Request(
        url,
        data=post_data,
        headers={
            "User-Agent": UA,
            "Content-Type": "application/x-www-form-urlencoded" if post_data else "text/html",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return r.read().decode("utf-8", errors="replace")
    except urllib.error.URLError as e:
        print(f"  ERROR fetching {url}: {e}", file=sys.stderr)
        return None


def download_file(path, dest_path):
    """Download BASE_URL + path (PDF) to dest_path. Returns True on success."""
    url = BASE_URL + path if path.startswith("/") else path
    url = url.split("?")[0]  # strip ?html=true if present
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            if r.status != 200:
                print(f"  WARNING: HTTP {r.status} — {url}", file=sys.stderr)
                return False
            with open(dest_path, "wb") as f:
                f.write(r.read())
        return True
    except Exception as e:
        print(f"  WARNING: {e} — {url}", file=sys.stderr)
        return False


# --- HTML parsing ---

def parse_boards(html):
    """Return list of (cat_id, board_name) from the AgendaCenter page."""
    pattern = r'aria-controls="category-panel-(\d+)"[^>]*>\s*([^<]+)\s*</h2>'
    return [
        (cat_id, name.strip())
        for cat_id, name in re.findall(pattern, html)
    ]


def parse_rows(html, cat_id):
    """
    Return list of {date, agenda_url, minutes_url, title} for the rows
    belonging to category panel cat_id in html.
    minutes_url may be None.
    """
    panel_start = html.find(f'id="category-panel-{cat_id}"')
    if panel_start < 0:
        return []

    next_panel = html.find('id="category-panel-', panel_start + 1)
    chunk = html[panel_start: next_panel if next_panel > 0 else len(html)]

    rows = re.findall(r'<tr[^>]+class="catAgendaRow"[^>]*>(.*?)</tr>', chunk, re.DOTALL)
    items = []
    for row in rows:
        date_m = re.search(r'aria-label="Agenda for ([^"]+)"', row)
        if not date_m:
            continue
        try:
            meeting_date = datetime.datetime.strptime(date_m.group(1), "%B %d, %Y").date()
        except ValueError:
            continue

        agenda_m = re.search(r'href="(/AgendaCenter/ViewFile/Agenda/[^"?]+)', row)
        minutes_m = re.search(r'href="(/AgendaCenter/ViewFile/Minutes/[^"?]+)', row)

        title_m = re.search(r'<p[^>]*>.*?<a[^>]+>\s*([^<]+)\s*</a>', row, re.DOTALL)
        title = title_m.group(1).strip() if title_m else ""

        items.append({
            "date": meeting_date,
            "agenda_url": agenda_m.group(1) if agenda_m else None,
            "minutes_url": minutes_m.group(1) if minutes_m else None,
            "title": title,
        })
    return items


def _parse_rows_from_fragment(html):
    """Parse catAgendaRow entries from an UpdateCategoryList HTML fragment."""
    rows = re.findall(r'<tr[^>]+class="catAgendaRow"[^>]*>(.*?)</tr>', html, re.DOTALL)
    items = []
    for row in rows:
        date_m = re.search(r'aria-label="Agenda for ([^"]+)"', row)
        if not date_m:
            continue
        try:
            meeting_date = datetime.datetime.strptime(date_m.group(1), "%B %d, %Y").date()
        except ValueError:
            continue
        agenda_m = re.search(r'href="(/AgendaCenter/ViewFile/Agenda/[^"?]+)', row)
        minutes_m = re.search(r'href="(/AgendaCenter/ViewFile/Minutes/[^"?]+)', row)
        title_m = re.search(r'<p[^>]*>.*?<a[^>]+>\s*([^<]+)\s*</a>', row, re.DOTALL)
        items.append({
            "date": meeting_date,
            "agenda_url": agenda_m.group(1) if agenda_m else None,
            "minutes_url": minutes_m.group(1) if minutes_m else None,
            "title": title_m.group(1).strip() if title_m else "",
        })
    return items


# --- Utilities ---

def slugify(text):
    text = text.lower().strip()
    text = re.sub(r"[/\\]", "-", text)
    text = re.sub(r"\s+-\s+", "-", text)
    text = re.sub(r"[^\w\s-]", "", text)
    text = re.sub(r"[\s_]+", "-", text)
    text = re.sub(r"-{2,}", "-", text)
    return text.strip("-")[:60]


def make_dest_path(board_name, doc_type, meeting_date, output_dir, ext=".pdf", counter=0):
    date_prefix = meeting_date.strftime("%Y-%m-%d")
    month_dir = meeting_date.strftime("%Y-%m")
    board_slug = slugify(board_name)
    month_path = os.path.join(output_dir, month_dir)
    os.makedirs(month_path, exist_ok=True)
    suffix = f"-{counter}" if counter else ""
    return os.path.join(month_path, f"{date_prefix}-{board_slug}-{doc_type}{suffix}{ext}")


# --- Video (best-effort — see VIDEO NOTE above) ---

def fetch_json(url):
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return json.loads(r.read().decode("utf-8", errors="replace"))
    except Exception as e:
        print(f"  WARNING: fetch failed for {url}: {e}", file=sys.stderr)
        return None


def download_video_file(video_url, dest_path):
    # Written to a .part temp file and only renamed to dest_path once the
    # whole transfer succeeds, so a run killed mid-download (e.g. by the
    # cron timeout wrapper) can't leave a truncated file that future runs'
    # "if os.path.exists(dest): skip" check would treat as already done.
    tmp_path = dest_path + ".part"
    req = urllib.request.Request(video_url, headers={"User-Agent": UA})
    try:
        with urllib.request.urlopen(req, timeout=120) as r, open(tmp_path, "wb") as f:
            while True:
                chunk = r.read(1024 * 256)
                if not chunk:
                    break
                f.write(chunk)
        os.replace(tmp_path, dest_path)
        return True
    except Exception as e:
        print(f"  WARNING: {e}", file=sys.stderr)
        if os.path.exists(tmp_path):
            os.remove(tmp_path)
        return False


def fetch_cablecast_shows(search, page_size=25):
    params = {"pageSize": page_size, "sort": "-eventDate", "search": search}
    data = fetch_json(f"{CABLECAST_BASE}/shows?{urllib.parse.urlencode(params)}")
    return (data or {}).get("shows", [])


def fetch_cablecast_vod_url(vod_id):
    data = fetch_json(f"{CABLECAST_BASE}/vods/{vod_id}")
    return (data or {}).get("vod", {}).get("url")


def collect_videos(cutoff, today, board_filter, known_boards):
    boards = set(known_boards)
    if board_filter:
        boards = {b for b in boards if board_filter in b.lower()}
    videos = []
    seen_show_ids = set()
    for board in sorted(boards):
        for show in fetch_cablecast_shows(search=f"Haddam {board}"):
            if show["id"] in seen_show_ids:
                continue
            title = show.get("title") or ""
            if "haddam" not in title.lower() or board.lower() not in title.lower():
                continue
            raw_date = show.get("eventDate") or ""
            try:
                meeting_date = datetime.date.fromisoformat(raw_date[:10])
            except ValueError:
                continue
            if not (cutoff <= meeting_date <= today):
                continue
            if not show.get("vods"):
                continue  # scheduled but not yet recorded/published
            seen_show_ids.add(show["id"])
            videos.append({
                "board": board, "date": meeting_date,
                "vod_id": show["vods"][0], "title": title,
            })
    return videos


def download_video(vod_id, dest_path):
    url = fetch_cablecast_vod_url(vod_id)
    if not url:
        return False
    return download_video_file(url, dest_path)


def assign_counters(items, key_fn):
    seen = {}
    for item in items:
        key = key_fn(item)
        item["counter"] = seen.get(key, 0)
        seen[key] = item["counter"] + 1


# --- Main ---

def main():
    parser = argparse.ArgumentParser(
        description="Download Haddam CT municipal agendas and minutes for meetings in the past N days."
    )
    parser.add_argument("--days", type=int, default=DAYS_BACK, metavar="N",
                        help=f"Look back N days (default: {DAYS_BACK})")
    parser.add_argument("--output-dir", default=OUTPUT_DIR, metavar="DIR",
                        help=f"Destination directory (default: {OUTPUT_DIR})")
    parser.add_argument("--dry-run", action="store_true",
                        help="List matching items without downloading")
    parser.add_argument("--board", metavar="NAME",
                        help="Only process boards whose name contains NAME (case-insensitive)")
    parser.add_argument("--include-video", action="store_true",
                        help="Also search for Cablecast video recordings (see VIDEO NOTE — low/no yield expected)")
    parser.add_argument("--video-only", action="store_true", help="Search only for video recordings")
    args = parser.parse_args()

    now = datetime.datetime.now()
    if (now.weekday() == 5 and now.hour >= 18) or (now.weekday() == 6 and now.hour < 12):  # Saturday night, Sunday morning
        print("Skipping — no downloads on Saturday nights or Sunday mornings.")
        sys.exit(0)

    do_docs = not args.video_only
    do_video = args.include_video or args.video_only

    today = datetime.date.today()
    cutoff = today - datetime.timedelta(days=args.days)

    # If the lookback window crosses a year boundary, fetch the prior year too
    years_needed = {today.year}
    if cutoff.year != today.year:
        years_needed.add(cutoff.year)

    print(f"Cutoff date : {cutoff}  ({args.days} days back)")
    print(f"Fetching    : {AGENDA_CENTER_URL}")
    if not args.dry_run:
        print(f"Output dir  : {args.output_dir}")
    print()

    # --- Step 1: fetch the main page ---
    print("Fetching AgendaCenter index...")
    main_html = fetch_html(AGENDA_CENTER_URL)
    if not main_html:
        print("ERROR: Could not fetch AgendaCenter page.", file=sys.stderr)
        sys.exit(1)

    boards = parse_boards(main_html)
    if not boards:
        print("ERROR: No boards found — page structure may have changed.", file=sys.stderr)
        sys.exit(1)

    print(f"Found {len(boards)} board(s).\n")

    if args.board:
        filter_name = args.board.lower()
        boards = [(cid, name) for cid, name in boards if filter_name in name.lower()]
        print(f"Filtered to {len(boards)} board(s) matching '{args.board}'.\n")

    # --- Step 2: collect matching meetings ---
    matches = []

    if do_docs:
        for cat_id, board_name in boards:
            rows = parse_rows(main_html, cat_id)

            # Fetch prior year data if the lookback window crosses a year boundary
            if len(years_needed) > 1:
                prior_year = min(years_needed)
                post_data = urllib.parse.urlencode(
                    {"year": prior_year, "catID": cat_id}
                ).encode()
                prior_html = fetch_html(UPDATE_URL, post_data=post_data)
                if prior_html:
                    rows += _parse_rows_from_fragment(prior_html)
                time.sleep(0.2)

            for row in rows:
                if row["date"] < cutoff or not row["agenda_url"]:
                    continue
                matches.append({
                    "board": board_name,
                    "date": row["date"],
                    "title": row["title"],
                    "agenda_url": row["agenda_url"],
                    "minutes_url": row["minutes_url"],
                })

        matches.sort(key=lambda x: (x["date"], x["board"]), reverse=True)

        total_docs = sum(1 + bool(m["minutes_url"]) for m in matches)
        print(f"Found {len(matches)} meeting(s) with up to {total_docs} document(s) in the past {args.days} days.")
        print()

    videos = []
    if do_video:
        known_boards = {name for _, name in boards}
        print(f"Searching Cablecast for {len(known_boards)} known board name(s)...")
        videos = collect_videos(cutoff, today, args.board.lower() if args.board else None, known_boards)
        assign_counters(videos, lambda v: (v["board"], v["date"]))
        print(f"Video       : {len(videos)} found\n")

    if not matches and not videos:
        sys.exit(0)

    if args.dry_run:
        if matches:
            print(f"{'Board':<42} {'Date':<12} Docs")
            print("-" * 65)
            for m in matches:
                docs = ["agenda"] + (["minutes"] if m["minutes_url"] else [])
                print(f"{m['board'][:41]:<42} {m['date']!s:<12} {', '.join(docs)}")
            print(f"\n{len(matches)} meeting(s).")
        if videos:
            print(f"\n{'Board':<42} {'Date':<12} Video")
            print("-" * 65)
            for v in videos:
                print(f"{v['board'][:41]:<42} {v['date']!s:<12} {v['title']}")
        print("\nRe-run without --dry-run to download.")
        return

    # --- Step 3: download ---
    os.makedirs(args.output_dir, exist_ok=True)
    log_path = os.path.join(args.output_dir, "download-log.txt")
    log_lines = []
    downloaded = skipped = failed = 0

    for m in matches:
        board = m["board"]
        date = m["date"]
        print(f"[{date}] {board}")

        for doc_type, url in (
            ("agenda", m["agenda_url"]),
            ("minutes", m["minutes_url"]),
        ):
            if not url:
                continue

            dest = make_dest_path(board, doc_type, date, args.output_dir)
            label = os.path.basename(dest)

            if os.path.exists(dest):
                print(f"  skip (exists)  {label}")
                skipped += 1
                continue

            print(f"  downloading    {label}")
            if download_file(url, dest):
                downloaded += 1
                log_lines.append(f"{datetime.datetime.now().isoformat()}  OK       {dest}")
            else:
                failed += 1
                log_lines.append(f"{datetime.datetime.now().isoformat()}  FAILED   {BASE_URL + url}")
                if os.path.exists(dest):
                    os.remove(dest)

            time.sleep(DELAY_SECONDS)

    for v in videos:
        dest = make_dest_path(v["board"], "video", v["date"], args.output_dir, ext=".mp4", counter=v["counter"])
        label = os.path.basename(dest)
        if os.path.exists(dest):
            print(f"  skip (exists)  {label}")
            skipped += 1
            continue
        print(f"  [{v['date']}] {v['board']} — video ({v['title']})")
        print(f"  downloading    {label}")
        if download_video(v["vod_id"], dest):
            downloaded += 1
            log_lines.append(f"{datetime.datetime.now().isoformat()}  OK       {dest}")
        else:
            failed += 1
            log_lines.append(f"{datetime.datetime.now().isoformat()}  FAILED   video {v['vod_id']}")

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
#    python3 scripts/download-haddam-agendas.py --dry-run
#
# 2. Narrow to one board:
#    python3 scripts/download-haddam-agendas.py --board "Board of Selectmen"
#
# 3. Change the lookback window:
#    python3 scripts/download-haddam-agendas.py --days 7
#
# 4. Save files somewhere else:
#    python3 scripts/download-haddam-agendas.py --output-dir ~/Downloads/haddam
#
# 5. Run on a schedule (cron — 8 AM daily):
#    0 8 * * * cd /path/to/repo && python3 scripts/download-haddam-agendas.py
#
# 6. Process downloaded files with Claude afterward:
#    python3 scripts/download-haddam-agendas.py && bash scripts/batch-process.sh beat-archive/haddam-agendas/
#
# 7. Also try Cablecast video search (see VIDEO NOTE — expect little/nothing):
#    python3 scripts/download-haddam-agendas.py --include-video
#
# NOTE: CivicPlus AgendaCenter exposes meeting dates, not upload/posted dates.
# The script filters by meeting date. A future meeting whose agenda was posted
# early will appear once its meeting date enters the lookback window.
