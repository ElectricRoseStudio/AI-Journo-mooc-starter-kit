#!/usr/bin/env python3
# download-peabody-agendas.py
# Download municipal meeting agendas, minutes, and video recordings for
# Peabody, MA for meetings within the past N days (and up to 7 days
# ahead).
#
# USAGE:
#   python3 scripts/download-peabody-agendas.py [options]
#
# REQUIREMENTS:
#   - Python 3.6+, requests, beautifulsoup4
#   - Internet connection
#
# SITE STRUCTURE — Peabody does NOT use CivicPlus's Agenda Center (that
# platform belongs to a different entity, Peabody Municipal Light Plant,
# at pmlp.com — not the city). The city's own site, peabody-ma.gov, is a
# hand-built static HTML site with no agenda page linked from its own
# navigation at all. The real sources, found by reverse-engineering the
# page (not linked from any menu):
#
#   1. AGENDAS (all municipal boards, including Parks Commission) — a
#      public Google Calendar embedded on peabody-ma.gov/full-calendarG3.html
#      via FullCalendar's Google Calendar plugin. View-source on that page
#      reveals both the calendar ID and a public read-only API key
#      hardcoded into the page's own JS (the same key any visitor's
#      browser uses to render the widget):
#        Calendar ID: sctwest@gmail.com
#        API key:     AIzaSyCNchPaMva8Fl8zopQOwmxwtsX4Hyj7bX0
#      Queried directly via the Google Calendar API v3 (no login):
#        GET https://www.googleapis.com/calendar/v3/calendars/{id}/events
#      This calendar mixes government meetings with unrelated community
#      events (concerts, film screenings, walks) — matched against a
#      known-board list (scraped from the minutes page below, plus
#      "School Committee") the same way Cablecast video titles are
#      matched elsewhere in this repo. Each meeting event's description
#      contains an "Agenda" link once posted (typically ~48 hours ahead,
#      per Open Meeting Law), pointing to a PDF under peabody-ma.gov's
#      own /meetings/ path — a directory that, like the calendar itself,
#      is not linked from anywhere in the site's own navigation.
#
#   2. MINUTES (most municipal boards, NOT School Committee or Parks
#      Commission — see below) — a single static page,
#      peabody-ma.gov/meeting%20minutes.html, with one Bootstrap tab per
#      board (13 total: City Council, Planning Board, Zoning Board,
#      Conservation Commission, Historical Commission, CPC, Licensing
#      Board, Board of Health, Parks Commission, Retirement Board,
#      Commission on Disability, Board of Assessors, Cemetery
#      Commission), each with year-grouped accordions of dated PDF
#      links. Parsed directly with BeautifulSoup (which — deliberately
#      relied on here — does not surface HTML comments as elements: the
#      Parks Commission tab's entire link list is wrapped in an HTML
#      comment on the live page, i.e. genuinely unpublished, not just
#      hard to find, so it correctly yields zero results). Coverage is
#      uneven across boards — several tabs (Zoning Board, Conservation
#      Commission, Historical Commission, CPC, Licensing Board, Parks
#      Commission, Commission on Disability, Cemetery Commission) had
#      zero live entries at all when checked directly, even though the
#      calendar confirms these boards meet regularly — minutes lag well
#      behind meetings for most boards on this page; agendas via the
#      calendar are the more reliably current source.
#
#   3. SCHOOL COMMITTEE (Board of Education) — a third, separate system
#      run by Peabody Public Schools, not the city: a public Google
#      Drive folder tree linked from peabody.k12.ma.us/school-committee/,
#      root folder id 1hBNZ7E8v-StL3M64wTqMNNIAnWRUmURl ("School
#      Committee"), containing one "{YYYY} Meeting Agendas and Minutes"
#      subfolder per school year -> 5 category subfolders (Budget
#      Meetings, Executive Session Meetings, Regular School Committee
#      Meetings, Special School Committee Meetings, Subcommittee
#      Meetings) -> one subfolder per meeting date (inconsistently
#      formatted: "1/13/26", "4.28.26", "5/12/2026", sometimes with a
#      trailing note like "*Cancelled Due to Snow*") -> files. Listed via
#      Google's unauthenticated `embeddedfolderview` endpoint, same
#      mechanism used for North Andover MA elsewhere in this repo.
#
#   PARKS COMMISSION — confirmed active (met 2026-01-08 and 2026-05-21
#   per the calendar) but its minutes tab on the city's own page is
#   entirely commented out in the page's HTML — not merely stale, coded
#   as not-to-be-shown. Agendas ARE available for it via the calendar
#   feed like every other board; there is currently no minutes source for
#   it available anywhere online. This is noted directly in the send
#   script's email body rather than silently omitted.
#
# VIDEO — Peabody TV (peabodytv.org) runs on Cablecast (Tightrope Media
# Systems), the same platform already solved for North Andover MA
# elsewhere in this repo, but a different tenant with its own API host
# and its own title-date format:
#   GET https://reflect-peabody.cablecast.tv/cablecastapi/v1/shows?search={board}&sort=-eventDate
# Titles end "... MM.DD.YY" (two-digit year here, unlike North Andover's
# four-digit year — confirmed directly across dozens of samples) rather
# than "Board - MM.DD.YYYY". Cablecast's own "Agenda" custom field exists
# in the schema for every show but was confirmed empty for every show
# checked — the city never used it — so agendas are NOT available via
# Cablecast and must come from the calendar instead. Each matched show's
# direct, unauthenticated MP4 URL comes from:
#   GET https://reflect-peabody.cablecast.tv/cablecastapi/v1/vods/{vod_id}
# Cablecast has no video coverage for Parks Commission (confirmed via a
# direct search — zero results), so Parks Commission has agenda coverage
# only, no video.

import argparse
import datetime
import os
import re
import sys
import time
import urllib.parse
import urllib.request

import requests
from bs4 import BeautifulSoup

# --- Configuration ---
UA = "Peabody-MA-Agendas-Downloader/1.0 (journalism research)"

CITY_BASE = "https://www.peabody-ma.gov"
MINUTES_PAGE_URL = f"{CITY_BASE}/meeting%20minutes.html"

# Public read-only calendar ID and API key, both hardcoded into
# peabody-ma.gov's own calendar widget JS (full-calendarG3.html) — this
# is the same access any visitor's browser gets when that page loads.
CALENDAR_ID = "sctwest@gmail.com"
CALENDAR_API_KEY = "AIzaSyCNchPaMva8Fl8zopQOwmxwtsX4Hyj7bX0"

SCHOOL_COMMITTEE_ROOT = "1hBNZ7E8v-StL3M64wTqMNNIAnWRUmURl"  # "School Committee" Drive folder
SCHOOL_COMMITTEE_BOARD_NAME = "School Committee"

CABLECAST_BASE = "https://reflect-peabody.cablecast.tv/cablecastapi/v1"

OUTPUT_DIR = "beat-archive/peabody-agendas"
DAYS_BACK = 4
DAYS_AHEAD = 7
DELAY_SECONDS = 0.5
MAX_WALK_DEPTH = 4

_YEAR_RE = re.compile(r"^(\d{4})\b")
_MMDDYY_RE = re.compile(r"(\d{1,2})[./](\d{1,2})[./](\d{2,4})")
_MINUTES_LINK_TEXT_RE = re.compile(
    r"\b(\d{1,2})/(\d{1,2})/(\d{2,4})\b"
)
_VIDEO_TITLE_RE = re.compile(r"(\d{1,2})\.(\d{1,2})\.(\d{2,4})\s*$")


def _get(url, **kwargs):
    kwargs.setdefault("headers", {})["User-Agent"] = UA
    kwargs.setdefault("timeout", 30)
    return requests.get(url, **kwargs)


def download_url_to_file(url, dest_path, headers=None):
    # Written to a .part temp file and only renamed to dest_path once the
    # whole transfer succeeds. Large videos can run well past the outer
    # cron timeout wrapper, which kills this process with SIGTERM rather
    # than a catchable exception — without this, a killed mid-download
    # would leave a truncated file sitting at dest_path, and every future
    # run's "if os.path.exists(dest): skip" check would then silently and
    # permanently treat that corrupt file as already downloaded.
    tmp_path = dest_path + ".part"
    req = urllib.request.Request(url, headers={"User-Agent": UA, **(headers or {})})
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


def _parse_2digit_year(yr):
    yr = int(yr)
    return yr + 2000 if yr < 100 else yr


# --- (1) Minutes (static page) ---

def fetch_minutes_boards():
    """Returns {board_name: [(meeting_date, href, link_text), ...]}."""
    try:
        r = _get(MINUTES_PAGE_URL)
        r.raise_for_status()
    except Exception as e:
        print(f"  WARNING: minutes page fetch failed: {e}", file=sys.stderr)
        return {}
    soup = BeautifulSoup(r.text, "html.parser")
    boards = {}
    for pane in soup.find_all("div", class_="tab-pane"):
        font = pane.find("font")
        heading = font.get_text(strip=True) if font else pane.get("id", "")
        board_name = re.sub(r"\s*Meeting Minutes\s*$", "", heading).strip()
        if not board_name:
            continue
        entries = []
        for a in pane.find_all("a", href=True):
            href = a["href"]
            if not href.lower().endswith(".pdf"):
                continue
            m = _MINUTES_LINK_TEXT_RE.search(a.get_text())
            if not m:
                continue
            mo, day, yr = int(m.group(1)), int(m.group(2)), _parse_2digit_year(m.group(3))
            try:
                d = datetime.date(yr, mo, day)
            except ValueError:
                continue
            entries.append((d, href, a.get_text(strip=True)))
        boards[board_name] = entries
    return boards


def collect_minutes(cutoff, future_limit, board_filter):
    docs = []
    for board_name, entries in fetch_minutes_boards().items():
        if board_filter and board_filter not in board_name.lower():
            continue
        for d, href, _text in entries:
            if cutoff <= d <= future_limit:
                docs.append({
                    "board": board_name, "meeting_date": d, "doc_type": "minutes",
                    "source": "citypdf", "href": href, "name": os.path.basename(href),
                })
    return docs


def download_city_pdf(href, dest_path):
    url = href if href.startswith("http") else f"{CITY_BASE}/{href.lstrip('/')}"
    return download_url_to_file(url, dest_path)


# --- (2) Agendas (Google Calendar) ---

def fetch_calendar_events(cutoff, future_limit):
    time_min = f"{cutoff.isoformat()}T00:00:00Z"
    time_max = f"{(future_limit + datetime.timedelta(days=1)).isoformat()}T00:00:00Z"
    params = {
        "key": CALENDAR_API_KEY, "maxResults": 250, "orderBy": "startTime",
        "singleEvents": "true", "timeMin": time_min, "timeMax": time_max,
    }
    url = f"https://www.googleapis.com/calendar/v3/calendars/{urllib.parse.quote(CALENDAR_ID)}/events"
    events = []
    page_token = None
    while True:
        p = dict(params)
        if page_token:
            p["pageToken"] = page_token
        try:
            r = _get(url, params=p)
            r.raise_for_status()
            data = r.json()
        except Exception as e:
            print(f"  WARNING: calendar fetch failed: {e}", file=sys.stderr)
            break
        events += data.get("items", [])
        page_token = data.get("nextPageToken")
        if not page_token:
            break
    return events


def _event_date(event):
    start = event.get("start", {})
    raw = start.get("dateTime") or start.get("date")
    if not raw:
        return None
    try:
        return datetime.date.fromisoformat(raw[:10])
    except ValueError:
        return None


def collect_calendar_agendas(cutoff, future_limit, board_filter, known_boards):
    docs = []
    for event in fetch_calendar_events(cutoff, future_limit):
        title = event.get("summary") or ""
        title_lower = title.lower()
        matched_board = None
        for b in known_boards:
            if b.lower() in title_lower:
                matched_board = b
                break
        if not matched_board:
            continue
        if board_filter and board_filter not in matched_board.lower():
            continue
        d = _event_date(event)
        if not d or not (cutoff <= d <= future_limit):
            continue
        description = event.get("description") or ""
        soup = BeautifulSoup(description, "html.parser")
        for a in soup.find_all("a", href=True):
            if "agenda" not in a.get_text(strip=True).lower():
                continue
            href = a["href"]
            docs.append({
                "board": matched_board, "meeting_date": d, "doc_type": "agenda",
                "source": "citypdf", "href": href, "name": os.path.basename(urllib.parse.urlparse(href).path),
            })
    return docs


# --- (3) School Committee (Google Drive) ---

def list_gdrive_folder(folder_id):
    url = f"https://drive.google.com/embeddedfolderview?id={folder_id}"
    try:
        r = _get(url)
        r.raise_for_status()
    except Exception as e:
        print(f"  WARNING: Drive folder {folder_id} failed: {e}", file=sys.stderr)
        return []
    soup = BeautifulSoup(r.text, "html.parser")
    items = []
    for a in soup.find_all("a"):
        href = a.get("href", "")
        name = a.get_text(strip=True)
        if not href or not name:
            continue
        m = re.search(r"/folders/([-\w]{10,})", href)
        if m:
            items.append(("folder", m.group(1), name))
            continue
        m = re.search(r"/file/d/([-\w]{10,})", href)
        if m:
            items.append(("file", m.group(1), name))
    return items


def _doc_type_from_name(name):
    lower = name.lower()
    if "agenda" in lower:
        return "agenda"
    if "minutes" in lower:
        return "minutes"
    return None


def walk_school_committee_dates(folder_id, cutoff, future_limit, depth=0):
    results = []
    if depth > MAX_WALK_DEPTH:
        return results
    for typ, item_id, name in list_gdrive_folder(folder_id):
        if typ != "folder":
            continue
        m = _MMDDYY_RE.search(name)
        if not m:
            continue
        mo, day, yr = int(m.group(1)), int(m.group(2)), _parse_2digit_year(m.group(3))
        try:
            d = datetime.date(yr, mo, day)
        except ValueError:
            continue
        if not (cutoff <= d <= future_limit):
            continue
        for st, sid, sname in list_gdrive_folder(item_id):
            if st != "file":
                continue
            doc_type = _doc_type_from_name(sname)
            if not doc_type:
                continue
            results.append({
                "board": SCHOOL_COMMITTEE_BOARD_NAME, "meeting_date": d, "doc_type": doc_type,
                "source": "gdrive", "file_id": sid, "name": sname,
            })
    return results


def collect_school_committee(cutoff, future_limit):
    docs = []
    year_folders = [
        (fid, name) for typ, fid, name in list_gdrive_folder(SCHOOL_COMMITTEE_ROOT)
        if typ == "folder"
    ]
    for fid, name in year_folders:
        m = _YEAR_RE.match(name)
        if not m:
            continue
        yr = int(m.group(1))
        if not (cutoff.year <= yr <= future_limit.year):
            continue
        for typ, cat_id, _cat_name in list_gdrive_folder(fid):
            if typ != "folder":
                continue
            docs += walk_school_committee_dates(cat_id, cutoff, future_limit)
    return docs


def download_gdrive_file(file_id, dest_path):
    url = f"https://drive.google.com/uc?export=download&id={file_id}"
    return download_url_to_file(url, dest_path)


# --- (4) Cablecast video ---

def fetch_cablecast_shows(search=None, page_size=20):
    params = {"pageSize": page_size, "sort": "-eventDate"}
    if search:
        params["search"] = search
    url = f"{CABLECAST_BASE}/shows?{urllib.parse.urlencode(params)}"
    try:
        r = _get(url)
        r.raise_for_status()
        return r.json().get("shows", [])
    except Exception as e:
        print(f"  WARNING: Cablecast shows fetch failed: {e}", file=sys.stderr)
        return []


def fetch_cablecast_vod_url(vod_id):
    url = f"{CABLECAST_BASE}/vods/{vod_id}"
    try:
        r = _get(url)
        r.raise_for_status()
        return (r.json().get("vod") or {}).get("url")
    except Exception as e:
        print(f"  WARNING: Cablecast vod {vod_id} fetch failed: {e}", file=sys.stderr)
        return None


def collect_videos(cutoff, future_limit, board_filter, known_boards):
    all_boards = set(known_boards)
    if board_filter:
        all_boards = {b for b in all_boards if board_filter in b.lower()}
    videos = []
    seen_show_ids = set()
    for board in sorted(all_boards):
        for show in fetch_cablecast_shows(search=board):
            if show["id"] in seen_show_ids:
                continue
            title = show.get("title") or ""
            m = _VIDEO_TITLE_RE.search(title)
            if not m:
                continue
            mo, day, yr = int(m.group(1)), int(m.group(2)), _parse_2digit_year(m.group(3))
            try:
                meeting_date = datetime.date(yr, mo, day)
            except ValueError:
                continue
            if not (cutoff <= meeting_date <= future_limit):
                continue
            if not show.get("vods"):
                continue
            if board.lower() not in title.lower():
                continue
            seen_show_ids.add(show["id"])
            videos.append({
                "board": board, "meeting_date": meeting_date,
                "vod_id": show["vods"][0], "title": title,
            })
    return videos


def download_video(vod_id, dest_path):
    url = fetch_cablecast_vod_url(vod_id)
    if not url:
        return False
    return download_url_to_file(url, dest_path)


# --- File naming ---

def slugify(text, max_len=55):
    text = str(text).lower().strip()
    text = re.sub(r"[/\\&]", "-", text)
    text = re.sub(r"[^\w\s-]", "", text)
    text = re.sub(r"[\s_]+", "-", text)
    text = re.sub(r"-{2,}", "-", text)
    return text.strip("-")[:max_len]


def make_path(board, doc_type, meeting_date, output_dir, ext=".pdf", counter=0):
    date_str = meeting_date.strftime("%Y-%m-%d")
    month_dir = os.path.join(output_dir, meeting_date.strftime("%Y-%m"))
    os.makedirs(month_dir, exist_ok=True)
    suffix = f"-{counter}" if counter else ""
    return os.path.join(month_dir, f"{date_str}-{slugify(board)}-{slugify(doc_type)}{suffix}{ext}")


def assign_counters(items, key_fn):
    seen = {}
    for item in items:
        key = key_fn(item)
        item["counter"] = seen.get(key, 0)
        seen[key] = item["counter"] + 1


# --- Main ---

def main():
    parser = argparse.ArgumentParser(
        description=(
            "Download Peabody MA municipal agendas, minutes, and video "
            "recordings for meetings within the past N days (and up to M days ahead)."
        )
    )
    parser.add_argument("--days", type=int, default=DAYS_BACK, metavar="N",
                        help=f"Look back N days (default: {DAYS_BACK})")
    parser.add_argument("--ahead", type=int, default=DAYS_AHEAD, metavar="N",
                        help=f"Also include meetings up to N days ahead (default: {DAYS_AHEAD})")
    parser.add_argument("--output-dir", default=OUTPUT_DIR, metavar="DIR",
                        help=f"Destination directory (default: {OUTPUT_DIR})")
    parser.add_argument("--dry-run", action="store_true", help="List matching items without downloading")
    parser.add_argument("--board", metavar="NAME",
                        help="Only process boards whose name contains NAME (case-insensitive)")
    parser.add_argument("--no-minutes", action="store_true", help="Skip minutes, download agendas only")
    parser.add_argument("--no-agendas", action="store_true", help="Skip agendas, download minutes only")
    parser.add_argument("--include-video", action="store_true", help="Also download Peabody TV video recordings")
    parser.add_argument("--video-only", action="store_true", help="Download only video recordings")
    args = parser.parse_args()

    now = datetime.datetime.now()
    if (now.weekday() == 5 and now.hour >= 18) or (now.weekday() == 6 and now.hour < 12):
        print("Skipping — no downloads on Saturday nights or Sunday mornings.")
        sys.exit(0)

    do_docs = not args.video_only
    do_video = args.include_video or args.video_only

    today = datetime.date.today()
    cutoff = today - datetime.timedelta(days=args.days)
    future_limit = today + datetime.timedelta(days=args.ahead)
    board_filter = args.board.lower() if args.board else None

    print(f"Date window : {cutoff} to {future_limit}")
    if not args.dry_run:
        print(f"Output dir  : {args.output_dir}")
    print()

    minutes_boards_cache = None
    docs = []
    if do_docs:
        print("Fetching minutes board list...")
        minutes_boards_cache = fetch_minutes_boards()
        known_boards = set(minutes_boards_cache.keys()) | {SCHOOL_COMMITTEE_BOARD_NAME}
        print(f"  {len(known_boards)} board(s) known")

        print("Fetching city minutes page...")
        minutes_docs = collect_minutes(cutoff, future_limit, board_filter)
        print(f"  {len(minutes_docs)} found")

        print("Fetching calendar agendas...")
        agenda_docs = collect_calendar_agendas(cutoff, future_limit, board_filter, known_boards)
        print(f"  {len(agenda_docs)} found")

        print("Fetching School Committee agendas/minutes...")
        sc_docs = collect_school_committee(cutoff, future_limit)
        if board_filter:
            sc_docs = [d for d in sc_docs if board_filter in d["board"].lower()]
        print(f"  {len(sc_docs)} found")

        docs = minutes_docs + agenda_docs + sc_docs
        if args.no_minutes:
            docs = [d for d in docs if d["doc_type"] != "minutes"]
        if args.no_agendas:
            docs = [d for d in docs if d["doc_type"] != "agenda"]
        print(f"Documents   : {len(docs)} found\n")
    else:
        minutes_boards_cache = fetch_minutes_boards()

    videos = []
    if do_video:
        known_boards = set(minutes_boards_cache.keys()) | {SCHOOL_COMMITTEE_BOARD_NAME}
        print("Fetching Peabody TV (Cablecast) recent shows...")
        videos = collect_videos(cutoff, future_limit, board_filter, known_boards)
        print(f"Video       : {len(videos)} found\n")

    docs.sort(key=lambda x: (x["meeting_date"], x["board"]), reverse=True)
    videos.sort(key=lambda x: (x["meeting_date"], x["board"]), reverse=True)
    # Keyed on the same fields make_path() uses for the filename (not
    # "source") so that same-day same-type docs from different sources
    # (e.g. a calendar-sourced agenda and a Drive-sourced agenda for the
    # same School Committee meeting) get distinct counters instead of
    # colliding on the same output filename.
    assign_counters(docs, lambda d: (d["board"], d["meeting_date"], d["doc_type"]))
    assign_counters(videos, lambda v: (v["board"], v["meeting_date"]))

    total = len(docs) + len(videos)
    if total == 0:
        print("No items found in the date window.")
        return

    if args.dry_run:
        if docs:
            print(f"{'Board':<30} {'Date':<12} {'Type':<10} Source")
            print("-" * 75)
            for d in docs:
                print(f"{d['board'][:29]:<30} {d['meeting_date']!s:<12} {d['doc_type']:<10} {d['source']}")
            print()
        if videos:
            print(f"{'Board':<30} {'Date':<12} Video")
            print("-" * 75)
            for v in videos:
                print(f"{v['board'][:29]:<30} {v['meeting_date']!s:<12} {v['title']}")
            print()
        print(f"{total} item(s). Re-run without --dry-run to download.")
        return

    os.makedirs(args.output_dir, exist_ok=True)
    log_path = os.path.join(args.output_dir, "download-log.txt")
    log_lines = []
    downloaded = skipped = failed = 0

    for d in docs:
        dest = make_path(d["board"], d["doc_type"], d["meeting_date"], args.output_dir, ext=".pdf", counter=d["counter"])
        label = os.path.basename(dest)
        if os.path.exists(dest):
            print(f"  skip (exists)  {label}")
            skipped += 1
            continue
        print(f"  [{d['meeting_date']}] {d['board']} — {d['doc_type']} ({d['source']})")
        print(f"  downloading    {label}")
        if d["source"] == "gdrive":
            ok = download_gdrive_file(d["file_id"], dest)
        else:  # citypdf
            ok = download_city_pdf(d["href"], dest)
        if ok:
            downloaded += 1
            log_lines.append(f"{datetime.datetime.now().isoformat()}  OK       {dest}")
        else:
            failed += 1
            log_lines.append(f"{datetime.datetime.now().isoformat()}  FAILED   {d.get('name', dest)}")
        time.sleep(DELAY_SECONDS)

    for v in videos:
        dest = make_path(v["board"], "video", v["meeting_date"], args.output_dir, ext=".mp4", counter=v["counter"])
        label = os.path.basename(dest)
        if os.path.exists(dest):
            print(f"  skip (exists)  {label}")
            skipped += 1
            continue
        print(f"  [{v['meeting_date']}] {v['board']} — video ({v['title']})")
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
    print(f"Files in: {args.output_dir}")
    if log_lines:
        print(f"Log:      {log_path}")


if __name__ == "__main__":
    main()


# --- Tips ---
#
# 1. Preview without downloading:
#    python3 scripts/download-peabody-agendas.py --dry-run
#
# 2. Just City Council:
#    python3 scripts/download-peabody-agendas.py --board "City Council"
#
# 3. PDFs only (no video — the default; --include-video/--video-only turn it on):
#    python3 scripts/download-peabody-agendas.py
#
# 4. Video only:
#    python3 scripts/download-peabody-agendas.py --video-only
#
# 5. Change the lookback window:
#    python3 scripts/download-peabody-agendas.py --days 14
#
# 6. Run on a schedule (cron — evening):
#    0 19 * * 1-5 cd /path/to/repo && python3 scripts/download-peabody-agendas.py
#
# COVERAGE: School Committee (Board of Education) is covered via its own
# Google Drive-based system on peabody.k12.ma.us (separate from every
# other board's calendar/minutes-page/Cablecast setup — see the platform
# notes at the top of this file). Parks Commission is confirmed active
# (agendas available via the calendar, video not covered by Cablecast,
# and no minutes source available anywhere online — its minutes tab on
# the city's own site is coded as hidden, not merely unpopulated).
