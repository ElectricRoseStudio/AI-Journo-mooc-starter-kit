#!/usr/bin/env python3
# download-boston-agendas.py
# Download municipal meeting agendas, minutes, and video recordings from
# Boston MA for meetings whose date falls within the past N days (and up
# to 7 days ahead).
#
# USAGE:
#   python3 scripts/download-boston-agendas.py [options]
#
# REQUIREMENTS:
#   - Python 3.6+  (no third-party packages needed for docs)
#   - yt-dlp       (for video: pip install yt-dlp)
#   - Internet connection
#
# WHAT IT DOES:
#   1. Legistar (City Council, School Committee, and 5 other bodies):
#      queries Boston's public Legistar Web API for events in the date
#      window, downloading each event's Agenda/Minutes PDF directly and
#      its video recording (if any) via Granicus.
#   2. Parks and Recreation Commission (+ related tree-removal hearings):
#      Boston's separate Public Notices system, department-filtered,
#      paginated, with a per-notice detail-page fetch for the PDF link.
#   3. Appends a download log to beat-archive/boston-agendas/download-log.txt
#
# SITE STRUCTURE:
#   (1) Legistar — the same platform used by many large cities. Boston's
#       public API needs no authentication:
#         https://webapi.legistar.com/v1/boston/Events
#           ?$filter=EventDate ge datetime'YYYY-MM-DD' and EventDate le datetime'YYYY-MM-DD'
#       Each event: EventBodyName (board), EventLocation (often the actual
#       committee-hearing name, e.g. "Government Operations Committee
#       Hearing on Docket #0998" — City Council's own body name is just
#       "City Council" for every hearing, so EventLocation is the more
#       specific label), EventAgendaFile / EventMinutesFile (direct PDF
#       URLs, no auth), EventMedia (a Granicus clip ID, present once video
#       is posted — see VIDEO NOTE).
#
#       Checked Boston's full Legistar body list directly (only 9 bodies
#       total, no body filter needed — the date-range query alone returns
#       everything relevant): City Council, Zoning Board of Appeal, Public
#       Improvement Commission, Boston School Committee, BPDA Board of
#       Directors, Disability Commission Advisory Board, Public Facilities,
#       Cable, Press Events. Boston School Committee (Board of Education)
#       is a native Legistar body — no separate scrape needed.
#
#   (2) Parks and Recreation — NOT in Legistar at all (checked directly:
#       zero results for any Parks-related body name). Boston's Parks and
#       Recreation Commission (a real public board distinct from the
#       Parks and Recreation city department — confirmed via direct
#       search) posts through the city's separate "Public Notices" Drupal
#       Views system instead:
#         https://www.boston.gov/archived-public-notices
#           ?field_contact_target_id[]=586        (586 = Parks and Recreation)
#           &page=N                                (0-indexed)
#       This same department filter also surfaces individual tree-removal
#       hearing notices, which are included too — they're genuinely posted
#       under the same department. The listing itself has no PDF link;
#       each matching notice's own page (linked from the listing) has to
#       be fetched separately for its "Resources" section PDF link(s).
#       No video is posted for this source (checked directly — no
#       Granicus/YouTube/etc. references anywhere on a sample notice page).
#
# VIDEO NOTE — Boston's Legistar instance is paired with its own Granicus
# tenant, boston.granicus.com — a different tenant from Somerville's
# (somervillema.granicus.com) but the identical platform and URL shape
# already solved for that town:
#   1. EventMedia gives a numeric clip ID.
#   2. The actual watchable/downloadable URL is
#        https://boston.granicus.com/player/clip/{id}?view_id=1&redirect=true
#      (confirmed by following Legistar's own Video.aspx redirect, which
#      is what its "Watch" link actually points to — a direct guess at
#      boston.granicus.com/mediaplayer.php?event_id={id}, by analogy with
#      the URL fragment visible in Legistar's page JS, 404s; the correct
#      path is capitalized differently AND goes through /player/clip/,
#      not /mediaplayer.php).
#   3. As with Somerville, downloaded via yt-dlp's generic "best" selector
#      rather than a hardcoded bitrate/format ID (bitrate varies by clip).
#   Not every event has EventMedia populated — video is only added once
#   the recording is processed, sometimes a day or more after the meeting.

import argparse
import datetime
import json
import re
import subprocess
import sys
import time
import os
import urllib.error
import urllib.parse
import urllib.request

# --- Configuration ---
LEGISTAR_API = "https://webapi.legistar.com/v1/boston"
GRANICUS_PLAYER_URL = "https://boston.granicus.com/player/clip/{clip_id}?view_id=1&redirect=true"
NOTICES_BASE = "https://www.boston.gov"
NOTICES_ARCHIVE_URL = f"{NOTICES_BASE}/archived-public-notices"
PARKS_DEPT_ID = "586"
OUTPUT_DIR = "beat-archive/boston-agendas"
DAYS_BACK = 4
DAYS_AHEAD = 7
REQUEST_DELAY = 0.4

UA = "Mozilla/5.0 (X11; Linux x86_64; rv:120.0) Gecko/20100101 Firefox/120.0"

_NOTICE_ROW_RE = re.compile(
    r'href="(/public-notices/\d+)"\s+title="([^"]*)">.*?'
    r'When</span>\s*<span class="dl-d">\s*([^<]+?)\s*</span>',
    re.DOTALL,
)
_NOTICE_DATE_RE = re.compile(
    r"(January|February|March|April|May|June|July|August|September|"
    r"October|November|December)\s+(\d{1,2}),\s+(\d{4})"
)
_NOTICE_PDF_RE = re.compile(
    r'download-link[^>]*>\s*<a href="([^"]+\.pdf)"[^>]*>([^<]+)</a>',
    re.DOTALL,
)


# --- HTTP helpers ---

def fetch_json(url):
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.load(r)


def fetch_html(url):
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "text/html,*/*"})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            charset = r.headers.get_content_charset() or "utf-8"
            return r.read().decode(charset, errors="replace")
    except Exception as e:
        print(f"  ERROR fetching {url}: {e}", file=sys.stderr)
        return None


def download_file(url, dest_path):
    safe_url = urllib.parse.quote(url, safe=":/?&=%")
    req = urllib.request.Request(safe_url, headers={"User-Agent": UA, "Accept": "*/*"})
    try:
        with urllib.request.urlopen(req, timeout=120) as r:
            with open(dest_path, "wb") as f:
                f.write(r.read())
        return True
    except Exception as e:
        print(f"  WARNING: {e}", file=sys.stderr)
        return False


# --- (1) Legistar: City Council, School Committee, and other bodies ---

def collect_legistar(cutoff, future_limit, board_filter):
    start = cutoff.strftime("%Y-%m-%d")
    end = future_limit.strftime("%Y-%m-%d")
    filter_expr = f"EventDate ge datetime'{start}' and EventDate le datetime'{end}'"
    params = urllib.parse.urlencode({"$filter": filter_expr, "$top": "1000"})
    url = f"{LEGISTAR_API}/Events?{params}"

    try:
        events = fetch_json(url)
    except Exception as e:
        print(f"  WARNING: Legistar fetch failed: {e}", file=sys.stderr)
        return [], []

    docs, videos = [], []
    for e in events:
        board = e.get("EventBodyName") or "Unknown Body"
        if board_filter and board_filter not in board.lower():
            continue
        try:
            meeting_date = datetime.date.fromisoformat(e["EventDate"][:10])
        except (KeyError, ValueError):
            continue
        extra = (e.get("EventLocation") or "").strip()

        if e.get("EventAgendaFile"):
            docs.append({
                "board": board, "meeting_date": meeting_date, "extra": extra,
                "doc_type": "Agenda", "href": e["EventAgendaFile"],
            })
        if e.get("EventMinutesFile"):
            docs.append({
                "board": board, "meeting_date": meeting_date, "extra": extra,
                "doc_type": "Minutes", "href": e["EventMinutesFile"],
            })
        if e.get("EventMedia"):
            videos.append({
                "board": board, "meeting_date": meeting_date, "extra": extra,
                "source": "granicus", "ref": str(e["EventMedia"]),
            })
    return docs, videos


def download_granicus_video(clip_id, dest_path):
    url = GRANICUS_PLAYER_URL.format(clip_id=clip_id)
    cmd = [
        "yt-dlp", "-f", "best",
        "-o", dest_path,
        "--no-overwrites",
        "--quiet",
        "--no-warnings",
        url,
    ]
    try:
        subprocess.run(cmd, check=True, timeout=7200)
        return True
    except subprocess.CalledProcessError as e:
        print(f"  WARNING: yt-dlp failed ({e})", file=sys.stderr)
        return False
    except subprocess.TimeoutExpired:
        print(f"  WARNING: yt-dlp timed out on Granicus clip {clip_id}", file=sys.stderr)
        return False


# --- (2) Public Notices: Parks and Recreation Commission ---

def collect_parks_notices(cutoff, future_limit, board_filter):
    if board_filter and "park" not in board_filter and "tree" not in board_filter:
        return []

    docs = []
    page = 0
    while True:
        params = urllib.parse.urlencode({"field_contact_target_id[]": PARKS_DEPT_ID, "page": page})
        html = fetch_html(f"{NOTICES_ARCHIVE_URL}?{params}")
        if not html:
            break
        rows = _NOTICE_ROW_RE.findall(html)
        if not rows:
            break

        any_in_window = False
        for href, title, when_text in rows:
            m = _NOTICE_DATE_RE.search(when_text)
            if not m:
                continue
            try:
                notice_date = datetime.datetime.strptime(
                    f"{m.group(1)} {m.group(2)}, {m.group(3)}", "%B %d, %Y"
                ).date()
            except ValueError:
                continue
            if notice_date < cutoff:
                continue  # older than window; page is newest-first so keep scanning this page
            any_in_window = True
            if notice_date > future_limit:
                continue

            title = title.strip()
            time.sleep(REQUEST_DELAY)
            detail_html = fetch_html(NOTICES_BASE + href)
            if not detail_html:
                continue
            for pdf_url, label in _NOTICE_PDF_RE.findall(detail_html):
                docs.append({
                    "board": title, "meeting_date": notice_date, "extra": "",
                    "doc_type": label.strip(), "href": pdf_url,
                })

        # Stop once every row on this page predates the window (newest-first listing).
        if not any_in_window:
            break
        page += 1
        time.sleep(REQUEST_DELAY)
        if page > 10:  # safety cap
            break
    return docs


# --- File naming ---

def slugify(text, max_len=55):
    text = str(text).lower().strip()
    text = re.sub(r"[/\\&]", "-", text)
    text = re.sub(r"[^\w\s-]", "", text)
    text = re.sub(r"[\s_]+", "-", text)
    text = re.sub(r"-{2,}", "-", text)
    return text.strip("-")[:max_len]


def make_path(board, doc_type, meeting_date, extra, output_dir, ext=".pdf", counter=0):
    date_str = meeting_date.strftime("%Y-%m-%d")
    month_dir = os.path.join(output_dir, meeting_date.strftime("%Y-%m"))
    os.makedirs(month_dir, exist_ok=True)
    board_slug = slugify(board, max_len=35)
    extra_slug = slugify(extra, max_len=25) if extra else ""
    type_slug = slugify(doc_type, max_len=20)
    parts = [date_str, board_slug]
    if extra_slug:
        parts.append(extra_slug)
    parts.append(type_slug)
    if counter:
        parts.append(str(counter))
    return os.path.join(month_dir, "-".join(parts) + ext)


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
            "Download Boston MA municipal agendas, minutes, and video "
            "recordings for meetings within the past N days (and up to 7 ahead)."
        )
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
                        help="Only include boards containing NAME (case-insensitive)")
    parser.add_argument("--no-video", action="store_true", help="Skip video (docs only)")
    parser.add_argument("--docs-only", action="store_true", help="Alias for --no-video")
    parser.add_argument("--video-only", action="store_true", help="Skip documents (video only)")
    args = parser.parse_args()

    now = datetime.datetime.now()
    if (now.weekday() == 5 and now.hour >= 18) or (now.weekday() == 6 and now.hour < 12):
        print("Skipping — no downloads on Saturday nights or Sunday mornings.")
        sys.exit(0)

    include_video = not (args.no_video or args.docs_only)
    include_docs = not args.video_only

    today = datetime.date.today()
    cutoff = today - datetime.timedelta(days=args.days)
    future_limit = today + datetime.timedelta(days=args.ahead)
    board_filter = args.board.lower() if args.board else None

    print(f"Date window : {cutoff} to {future_limit}")
    if not args.dry_run:
        print(f"Output dir  : {args.output_dir}")
    print()

    docs, videos = [], []
    if include_docs or include_video:
        print("Fetching Legistar events (City Council, School Committee, etc.)...")
        l_docs, l_videos = collect_legistar(cutoff, future_limit, board_filter)
        if include_docs:
            docs += l_docs
        if include_video:
            videos += l_videos
        print(f"  {len(l_docs)} document(s), {len(l_videos)} recording(s).\n")

    if include_docs:
        print("Fetching Parks and Recreation notices...")
        p_docs = collect_parks_notices(cutoff, future_limit, board_filter)
        docs += p_docs
        print(f"  {len(p_docs)} document(s).\n")

    docs.sort(key=lambda x: (x["meeting_date"], x["board"]), reverse=True)
    videos.sort(key=lambda x: (x["meeting_date"], x["board"]), reverse=True)
    assign_counters(docs, lambda d: (d["board"], d["meeting_date"], d["extra"], d["doc_type"]))
    assign_counters(videos, lambda v: (v["board"], v["meeting_date"], v["extra"]))

    total = len(docs) + len(videos)
    print(f"{total} item(s) total in window.\n")

    if args.dry_run:
        if docs:
            print(f"{'Board':<35} {'Date':<12} {'Extra':<28} Type")
            print("-" * 95)
            for d in docs:
                print(f"{d['board'][:34]:<35} {d['meeting_date']!s:<12} {d['extra'][:27]:<28} {d['doc_type']}")
            print()
        if videos:
            print(f"{'Board':<35} {'Date':<12} {'Extra':<28} Video ID")
            print("-" * 95)
            for v in videos:
                print(f"{v['board'][:34]:<35} {v['meeting_date']!s:<12} {v['extra'][:27]:<28} {v['ref']}")
            print()
        print(f"{total} item(s). Re-run without --dry-run to download.")
        return

    os.makedirs(args.output_dir, exist_ok=True)
    log_path = os.path.join(args.output_dir, "download-log.txt")
    log_lines = []
    downloaded = skipped = failed = 0

    for d in docs:
        dest = make_path(d["board"], d["doc_type"], d["meeting_date"], d["extra"],
                          args.output_dir, counter=d["counter"])
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
        time.sleep(REQUEST_DELAY)

    for v in videos:
        dest = make_path(v["board"], "video", v["meeting_date"], v["extra"], args.output_dir,
                          ext=".mp4", counter=v["counter"])
        label = os.path.basename(dest)
        if os.path.exists(dest):
            print(f"  skip (exists)  {label}")
            skipped += 1
            continue
        print(f"  [{v['meeting_date']}] {v['board']} — video (granicus {v['ref']})")
        print(f"  downloading    {label}")
        if download_granicus_video(v["ref"], dest):
            downloaded += 1
            log_lines.append(f"{datetime.datetime.now().isoformat()}  OK       {dest}")
        else:
            failed += 1
            log_lines.append(f"{datetime.datetime.now().isoformat()}  FAILED   granicus {v['ref']}")

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
#    python3 scripts/download-boston-agendas.py --dry-run
#
# 2. Just School Committee:
#    python3 scripts/download-boston-agendas.py --board "School Committee"
#
# 3. PDFs only (no video):
#    python3 scripts/download-boston-agendas.py --no-video
#
# 4. Video only:
#    python3 scripts/download-boston-agendas.py --video-only
#
# 5. Change the lookback window:
#    python3 scripts/download-boston-agendas.py --days 14
#
# 6. Run on a schedule (cron — evening):
#    0 19 * * 1-5 cd /path/to/repo && python3 scripts/download-boston-agendas.py
#
# COVERAGE: Boston School Committee (Board of Education) is a native
# Legistar body, downloaded the same way as City Council — no separate
# scrape needed. Parks and Recreation Commission (Boston's Parks & Rec
# public board, distinct from the Parks and Recreation city department) is
# covered via the separate Public Notices system since it's not in
# Legistar at all — see script header for the full breakdown.
