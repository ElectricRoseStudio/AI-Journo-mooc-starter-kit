#!/usr/bin/env python3
# download-durham-agendas.py
# Download municipal meeting agendas, minutes, and (best-effort) video
# recordings for Durham, CT.
#
# USAGE:
#   python3 scripts/download-durham-agendas.py [options]
#
# REQUIREMENTS:
#   - Python 3.6+  (no third-party packages needed)
#   - Internet connection
#
# WHAT IT DOES:
#   Documents (default):
#     1. Fetches the town's public file directory feed, which lists every
#        agenda/minutes/report PDF posted across all boards, newest first
#     2. Filters to file-type "Agenda" or "Minutes" whose date falls within
#        the lookback/lookahead window
#     3. Downloads each PDF straight from the town's document S3 bucket to
#        beat-archive/durham-agendas/YYYY-MM/
#     4. Appends a download log to beat-archive/durham-agendas/download-log.txt
#
#   Video (--include-video or --video-only; off by default — see VIDEO NOTE):
#     5. Searches the regional Cablecast (Tightrope Media Systems) archive
#        used by Durham's PEG channel for shows whose title matches a known
#        board name and whose event date falls in the window, and downloads
#        any that have a published VOD
#
# SITE STRUCTURE:
#   townofdurhamct.org is a Webflow site whose document data (agendas,
#   minutes, reports, etc.) is fed from a Finsweet CMS-filter collection —
#   NOT the CivicPlus "AgendaCenter" product used by most other CT towns in
#   this repo. Each row carries explicit machine-readable fields:
#     fs-cmsfilter-field="file-date"   -> "Aug 17, 2026"
#     fs-cmsfilter-field="file-title"  -> <a href="https://municipal-documents.s3.amazonaws.com/...pdf">
#     fs-cmsfilter-field="file-type"   -> "Agenda" / "Minutes" / "Report" / etc.
#     fs-cmsfilter-field="entity"      -> "Board of Selectmen" (human board name)
#   The ?file-type= query param on the page URL is a client-side (JS) filter
#   only — the server always renders the same feed regardless of query
#   string, so this script fetches it unfiltered and does the filtering
#   itself in Python.
#
#   Feed URL:  https://www.townofdurhamct.org/file-directory?file-type=Agenda%2CMinutes
#   Files:     served directly and publicly from
#              municipal-documents.s3.amazonaws.com/uploads/durham-ct/...
#
#   The feed is capped at ~100 rows across ALL document types and ALL
#   boards combined (a Webflow CMS collection-list limit), which in
#   practice covers roughly the past 6-8 weeks — far more than this
#   script's default 4-day-back/7-day-ahead window, so no per-board
#   pagination is needed for routine daily runs. It is NOT a substitute for
#   a full historical archive; there is no discovered way to page past the
#   ~100-row cap.
#
#   The board/commission portal at onboard.townofdurhamct.org ("OnBoardGOV"
#   by Clerkbase) is very likely the actual backing system for the
#   municipal-documents S3 bucket above, but it's a client-rendered SPA
#   with no discovered public JSON API, so this script uses the public
#   Webflow feed instead — same underlying documents, no auth needed.
#
# VIDEO NOTE — Durham does not have a reliable per-meeting video archive.
#   Durham's PEG outlet is the shared regional Cablecast (Tightrope Media
#   Systems) instance at reflect-vsctv.cablecast.tv, the same platform
#   already used for other towns in this repo (Swampscott, North Andover,
#   Peabody), but checked directly: Durham's own Cablecast "project" (ID 29,
#   "Durham Meetings") contains only 6 shows, all from Jan-Mar 2020 — i.e.
#   regular board-meeting recording appears to have stopped after COVID.
#   Searching the full regional catalog for "Durham" turns up an occasional
#   "Durham First Selectman Update" talk segment (not a meeting recording)
#   and the same handful of 2020 Board of Selectmen recordings, nothing
#   current. No YouTube channel, Zoom recording archive, or per-meeting
#   video link was found on the town's own site either. Video download is
#   therefore included for completeness and left OFF by default
#   (--include-video/--video-only to try it) — expect it to find little to
#   nothing until/unless Durham resumes recording meetings somewhere
#   discoverable.

import argparse
import datetime
import html
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

# --- Configuration ---
BASE_URL = "https://www.townofdurhamct.org"
FILE_DIRECTORY_URL = f"{BASE_URL}/file-directory?file-type=Agenda%2CMinutes"
CABLECAST_BASE = "https://reflect-vsctv.cablecast.tv/cablecastapi/v1"
OUTPUT_DIR = "beat-archive/durham-agendas"
DAYS_BACK = 4
DAYS_AHEAD = 7
DELAY_SECONDS = 0.8

UA = "Durham-CT-Agendas-Downloader/1.0 (journalism research)"

_MONTH_ABBR = {
    "Jan": 1, "Feb": 2, "Mar": 3, "Apr": 4, "May": 5, "Jun": 6,
    "Jul": 7, "Aug": 8, "Sep": 9, "Oct": 10, "Nov": 11, "Dec": 12,
}
_FILE_DATE_RE = re.compile(r"(Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)\s+(\d{1,2}),\s+(\d{4})")

_ROW_RE = re.compile(
    r'fs-cmsfilter-field="file-date"[^>]*>([^<]+)</div>.*?'
    r'fs-cmsfilter-field="file-title"\s+href="([^"]+)"[^>]*>([^<]+)</a>.*?'
    r'fs-cmsfilter-field="file-type"[^>]*>([^<]+)</div>.*?'
    r'fs-cmsfilter-field="entity"[^>]*>([^<]+)</div>',
    re.S,
)


# --- HTTP helpers ---

def fetch_html(url, retries=2):
    req = urllib.request.Request(
        url, headers={"User-Agent": UA, "Accept": "text/html,application/xhtml+xml,*/*"},
    )
    for attempt in range(retries + 1):
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                charset = r.headers.get_content_charset() or "utf-8"
                return r.read().decode(charset, errors="replace")
        except (urllib.error.URLError, TimeoutError, OSError) as e:
            print(f"  WARNING (attempt {attempt+1}): {e}", file=sys.stderr)
            if attempt < retries:
                time.sleep(3 * (attempt + 1))
    return None


def fetch_json(url):
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            import json
            return json.loads(r.read().decode("utf-8", errors="replace"))
    except Exception as e:
        print(f"  WARNING: fetch failed for {url}: {e}", file=sys.stderr)
        return None


def download_file(url, dest_path):
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "*/*"})
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            with open(dest_path, "wb") as f:
                f.write(r.read())
        return True
    except Exception as e:
        print(f"  WARNING: {e}", file=sys.stderr)
        return False


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


# --- (1) Documents ---

def parse_file_date(text):
    m = _FILE_DATE_RE.search(text)
    if not m:
        return None
    mon, day, yr = m.group(1), int(m.group(2)), int(m.group(3))
    try:
        return datetime.date(yr, _MONTH_ABBR[mon], day)
    except ValueError:
        return None


def collect_docs(cutoff, future_limit, board_filter, no_minutes, no_agendas):
    html_text = fetch_html(FILE_DIRECTORY_URL)
    if not html_text:
        return []

    doc_types_ok = set()
    if not no_agendas:
        doc_types_ok.add("agenda")
    if not no_minutes:
        doc_types_ok.add("minutes")

    docs = []
    for m in _ROW_RE.finditer(html_text):
        date_text, href, title, file_type, entity = m.groups()

        doc_type = file_type.strip().lower()
        if doc_type not in doc_types_ok:
            continue

        meeting_date = parse_file_date(date_text)
        if not meeting_date or not (cutoff <= meeting_date <= future_limit):
            continue

        board = html.unescape(entity.strip())
        if board_filter and board_filter not in board.lower():
            continue

        docs.append({
            "board": board,
            "meeting_date": meeting_date,
            "doc_type": doc_type,
            "title": html.unescape(title.strip()),
            "href": href.strip(),
        })
    return docs


# --- (2) Video (best-effort — see VIDEO NOTE above) ---

def fetch_cablecast_shows(search, page_size=25):
    params = {"pageSize": page_size, "sort": "-eventDate", "search": search}
    data = fetch_json(f"{CABLECAST_BASE}/shows?{urllib.parse.urlencode(params)}")
    return (data or {}).get("shows", [])


def fetch_cablecast_vod_url(vod_id):
    data = fetch_json(f"{CABLECAST_BASE}/vods/{vod_id}")
    return (data or {}).get("vod", {}).get("url")


def collect_videos(cutoff, future_limit, board_filter, known_boards):
    boards = set(known_boards)
    if board_filter:
        boards = {b for b in boards if board_filter in b.lower()}
    videos = []
    seen_show_ids = set()
    for board in sorted(boards):
        for show in fetch_cablecast_shows(search=f"Durham {board}"):
            if show["id"] in seen_show_ids:
                continue
            title = show.get("title") or ""
            if "durham" not in title.lower() or board.lower() not in title.lower():
                continue
            raw_date = show.get("eventDate") or ""
            try:
                meeting_date = datetime.date.fromisoformat(raw_date[:10])
            except ValueError:
                continue
            if not (cutoff <= meeting_date <= future_limit):
                continue
            if not show.get("vods"):
                continue  # scheduled but not yet recorded/published
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
    return download_video_file(url, dest_path)


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
            "Download Durham, CT municipal agendas and minutes for meetings "
            "within the past N days (and up to M days ahead)."
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
    parser.add_argument("--include-video", action="store_true",
                        help="Also search for Cablecast video recordings (see VIDEO NOTE — low/no yield expected)")
    parser.add_argument("--video-only", action="store_true", help="Search only for video recordings")
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

    docs = []
    if do_docs:
        docs = collect_docs(cutoff, future_limit, board_filter, args.no_minutes, args.no_agendas)
        print(f"Documents   : {len(docs)} found\n")

    videos = []
    if do_video:
        known_boards = {d["board"] for d in docs}
        if not known_boards:
            # Fall back to a wide window so there's still a board-name list
            # to search Cablecast against even when nothing falls inside
            # the (narrow, default) document date window.
            wide_docs = collect_docs(today - datetime.timedelta(days=365), future_limit, board_filter, False, False)
            known_boards = {d["board"] for d in wide_docs}
        print(f"Searching Cablecast for {len(known_boards)} known board name(s)...")
        videos = collect_videos(cutoff, future_limit, board_filter, known_boards)
        print(f"Video       : {len(videos)} found\n")

    docs.sort(key=lambda x: (x["meeting_date"], x["board"]), reverse=True)
    videos.sort(key=lambda x: (x["meeting_date"], x["board"]), reverse=True)
    assign_counters(docs, lambda d: (d["board"], d["meeting_date"], d["doc_type"]))
    assign_counters(videos, lambda v: (v["board"], v["meeting_date"]))

    total = len(docs) + len(videos)
    if total == 0:
        print("No items found in the date window.")
        return

    if args.dry_run:
        if docs:
            print(f"{'Board':<40} {'Date':<12} Type")
            print("-" * 65)
            for d in docs:
                print(f"{d['board'][:39]:<40} {d['meeting_date']!s:<12} {d['doc_type']}")
            print()
        if videos:
            print(f"{'Board':<40} {'Date':<12} Video")
            print("-" * 65)
            for v in videos:
                print(f"{v['board'][:39]:<40} {v['meeting_date']!s:<12} {v['title']}")
            print()
        print(f"{total} item(s). Re-run without --dry-run to download.")
        return

    os.makedirs(args.output_dir, exist_ok=True)
    log_path = os.path.join(args.output_dir, "download-log.txt")
    log_lines = []
    downloaded = skipped = failed = 0

    for d in docs:
        dest = make_path(d["board"], d["doc_type"], d["meeting_date"], args.output_dir, counter=d["counter"])
        label = os.path.basename(dest)
        if os.path.exists(dest):
            print(f"  skip (exists)  {label}")
            skipped += 1
            continue
        print(f"  [{d['meeting_date']}] {d['board']} — {d['doc_type']}")
        print(f"  downloading    {label}")
        if download_file(d["href"], dest):
            downloaded += 1
            log_lines.append(f"{datetime.datetime.now().isoformat()}  OK       {dest}")
        else:
            failed += 1
            log_lines.append(f"{datetime.datetime.now().isoformat()}  FAILED   {d['href']}")
            if os.path.exists(dest):
                os.remove(dest)
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
#    python3 scripts/download-durham-agendas.py --dry-run
#
# 2. Just Board of Selectmen:
#    python3 scripts/download-durham-agendas.py --board "Board of Selectmen"
#
# 3. Widen the lookback window (the feed covers ~6-8 weeks):
#    python3 scripts/download-durham-agendas.py --days 45 --dry-run
#
# 4. Also try Cablecast video search (see VIDEO NOTE — expect little/nothing):
#    python3 scripts/download-durham-agendas.py --include-video
#
# 5. Run on a schedule (cron — evening):
#    0 20 * * 1-5 cd /path/to/repo && python3 scripts/download-durham-agendas.py
