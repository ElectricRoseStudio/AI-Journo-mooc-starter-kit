#!/usr/bin/env python3
# download-trumbull-agendas.py
# Download municipal meeting agendas and minutes from Trumbull CT for meetings
# whose date falls within the past N days (and up to 7 days ahead, to catch
# agendas posted early for upcoming meetings).
#
# USAGE:
#   python3 scripts/download-trumbull-agendas.py [options]
#
# REQUIREMENTS:
#   - Python 3.6+  (no third-party packages needed)
#   - Internet connection
#
# WHAT IT DOES:
#   1. Fetches the Trumbull CT Agenda Center hub page to discover all board
#      category IDs (46 boards)
#   2. Calls the AgendaCenter Search endpoint with those IDs and a date range
#      — this returns all matching rows in a single inline HTML response
#   3. Parses meeting rows for agenda and minutes ViewFile URLs
#   4. Downloads PDFs to beat-archive/trumbull-agendas/YYYY-MM/
#   5. Queries the Cablecast VOD API for Government Meeting recordings
#      in the date window; saves each as a .url watch-page shortcut
#      (or downloads the MP4 with --download-video)
#   6. Appends a download log to beat-archive/trumbull-agendas/download-log.txt
#
# SITE STRUCTURE (CivicPlus CivicEngage — PDFs):
#   Hub:     https://www.trumbull-ct.gov/agendacenter
#   Search:  GET /AgendaCenter/Search/
#              ?term=&CIDs={cat1,cat2,...}&startDate=MM%2FDD%2FYYYY
#              &endDate=MM%2FDD%2FYYYY&dateRange=custom&dateSelector=range
#   Agenda:  /AgendaCenter/ViewFile/Agenda/_{MMDDYYYY}-{meetingID}
#   Minutes: /AgendaCenter/ViewFile/Minutes/_{MMDDYYYY}-{meetingID}
#
#   ViewFile URLs serve PDFs directly (Content-Type: application/pdf).
#   The meeting date is encoded as MMDDYYYY in both the anchor ID and
#   the ViewFile URL path.
#
# SITE STRUCTURE (Cablecast VOD API — recordings):
#   Portal:  https://reflect-trumbulltv.cablecast.tv/CablecastPublicSite/?channel=1
#   Gallery: /internetchannel/gallery/10?channel=1  (Government Meetings, 2515 items)
#   Watch:   /internetchannel/show/{showId}?channel=1
#
#   REST API (public, no auth required):
#     GET /cablecastapi/v1/shows?after=YYYY-MM-DD&before=YYYY-MM-DD&offset=N
#       Returns shows in date range (pageSize=50; paginate with offset).
#       meta.count is always the total unfiltered count — ignore it for pagination;
#       stop when a page returns fewer than 50 results.
#     Fields used: id, title, eventDate, category, vods[]
#     category 5 = "Government Meeting" (filter client-side)
#
#     GET /cablecastapi/v1/vods/{vodId}
#       vod.url = direct MP4 download URL (1-2 GB per meeting)
#       Used only when --download-video is set.
#
#   Default behaviour: save the watch-page URL as a .url shortcut.
#   Use --download-video to download the full MP4 instead (files are ~1-2 GB each).

import argparse
import datetime
import html as html_module
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

# --- Configuration ---
BASE_URL = "https://www.trumbull-ct.gov"
HUB_URL = f"{BASE_URL}/agendacenter"
SEARCH_URL = f"{BASE_URL}/AgendaCenter/Search/"
CABLECAST_BASE = "https://reflect-trumbulltv.cablecast.tv"
CABLECAST_WATCH = f"{CABLECAST_BASE}/internetchannel/show/{{show_id}}?channel=1"
OUTPUT_DIR = "beat-archive/trumbull-agendas"
DAYS_BACK = 4
DAYS_AHEAD = 7
DELAY_SECONDS = 0.8

UA = "Mozilla/5.0"

# Regex to parse _MMDDYYYY-meetingID from ViewFile paths
_DATE_ID_RE = re.compile(r'_(\d{2})(\d{2})(\d{4})-(\d+)$')


# --- HTTP helpers ---

def fetch_html(url, params=None):
    """GET url (with optional query params dict) and return decoded HTML, or None."""
    full_url = url
    if params:
        full_url += "?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(
        full_url,
        headers={"User-Agent": UA, "Accept": "text/html,*/*"},
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            raw = r.read()
            charset = r.headers.get_content_charset() or "utf-8"
            return raw.decode(charset, errors="replace")
    except urllib.error.HTTPError as e:
        print(f"  HTTP {e.code} — {full_url}", file=sys.stderr)
        return None
    except Exception as e:
        print(f"  ERROR fetching {full_url}: {e}", file=sys.stderr)
        return None


def fetch_json(url):
    """GET url and return parsed JSON, or None on error."""
    req = urllib.request.Request(
        url, headers={"User-Agent": UA, "Accept": "application/json"}
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return json.loads(r.read().decode("utf-8", errors="replace"))
    except urllib.error.HTTPError as e:
        print(f"  HTTP {e.code} — {url}", file=sys.stderr)
        return None
    except Exception as e:
        print(f"  ERROR fetching {url}: {e}", file=sys.stderr)
        return None


def download_file(url, dest_path, timeout=120):
    """Download url to dest_path. Returns True on success."""
    req = urllib.request.Request(
        url,
        headers={"User-Agent": UA, "Accept": "*/*"},
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            with open(dest_path, "wb") as f:
                f.write(r.read())
        return True
    except Exception as e:
        print(f"  WARNING: {e}", file=sys.stderr)
        return False


def save_url_shortcut(url, dest_path):
    """Save a URL as a Windows Internet Shortcut (.url) file. Returns True."""
    with open(dest_path, "w") as f:
        f.write(f"[InternetShortcut]\nURL={url}\n")
    return True


# --- CivicEngage parsing ---

def parse_category_ids(hub_html):
    """
    Extract all board category IDs from the hub page.
    Returns a list of ID strings like ['2', '3', '4', ...].
    """
    return list(dict.fromkeys(re.findall(r'id="cat(\d+)"', hub_html)))


def parse_meetings(search_html):
    """
    Parse meeting rows from the Search results page.

    Returns a list of dicts:
      {board, meeting_date, meeting_id, agenda_url, minutes_url}

    Board names come from <h2> tags inside id="cat{N}" divs.
    Agenda/minutes URLs come from ViewFile href attributes in catAgendaRow rows.
    meeting_date is a datetime.date; minutes_url may be None.
    """
    # Map category panel ID → board name
    board_names = {}
    for m in re.finditer(
        r'id="cat(\d+)"[^>]*>.*?<h2[^>]*>(.*?)</h2>', search_html, re.DOTALL
    ):
        cat_id = m.group(1)
        name = html_module.unescape(re.sub(r'<[^>]+>', '', m.group(2)).strip())
        board_names[cat_id] = name

    meetings = []

    for pan_m in re.finditer(
        r'<div\s+id="category-panel-(\d+)"[^>]*>(.*?)</div>\s*</span>',
        search_html, re.DOTALL,
    ):
        cat_id = pan_m.group(1)
        panel_html = pan_m.group(2)
        board = board_names.get(cat_id, f"cat{cat_id}")

        for row_m in re.finditer(
            r'<tr[^>]+class="catAgendaRow"[^>]*>(.*?)</tr>',
            panel_html, re.DOTALL,
        ):
            row_html = row_m.group(1)

            # Agenda URL — first ViewFile/Agenda link in the row
            agenda_m = re.search(
                r'href="(/AgendaCenter/ViewFile/Agenda/(_\d{8}-\d+))"',
                row_html,
            )
            if not agenda_m:
                continue
            agenda_path = agenda_m.group(1)
            date_id_str = agenda_m.group(2)  # e.g. _04092026-6066

            dm = _DATE_ID_RE.match(date_id_str)
            if not dm:
                continue
            mm, dd, yyyy, meeting_id = dm.groups()
            try:
                meeting_date = datetime.date(int(yyyy), int(mm), int(dd))
            except ValueError:
                continue

            # Minutes URL — link inside <td class="minutes">
            minutes_path = None
            minutes_td = re.search(
                r'<td[^>]+class="minutes"[^>]*>(.*?)</td>', row_html, re.DOTALL
            )
            if minutes_td and 'ViewFile/Minutes' in minutes_td.group(1):
                min_m = re.search(
                    r'href="(/AgendaCenter/ViewFile/Minutes/[^"]+)"',
                    minutes_td.group(1),
                )
                if min_m:
                    minutes_path = min_m.group(1)

            meetings.append({
                "board": board,
                "meeting_date": meeting_date,
                "meeting_id": meeting_id,
                "agenda_url": BASE_URL + agenda_path,
                "minutes_url": BASE_URL + minutes_path if minutes_path else None,
            })

    return meetings


# --- Cablecast helpers ---

def cablecast_fetch_shows(cutoff, future_limit):
    """
    Return all Cablecast shows with category=5 (Government Meeting) whose
    eventDate falls within [cutoff, future_limit].  Paginates automatically.

    Note: the API's after/before filters work only on the first page; subsequent
    pages return unfiltered results sorted by eventDate descending.  We apply
    client-side date filtering and stop pagination as soon as we see a show
    with eventDate earlier than cutoff.
    """
    shows = []
    offset = 0
    page_size = 50
    while True:
        params = urllib.parse.urlencode({
            "after": cutoff.isoformat(),
            "before": future_limit.isoformat(),
            "offset": offset,
        })
        data = fetch_json(f"{CABLECAST_BASE}/cablecastapi/v1/shows?{params}")
        if not data:
            break
        batch = data.get("shows", [])
        if not batch:
            break
        done = False
        for s in batch:
            event_date_str = (s.get("eventDate") or "")[:10]
            try:
                event_date = datetime.date.fromisoformat(event_date_str)
            except ValueError:
                continue
            # Shows are sorted newest-first; once we pass cutoff we're done
            if event_date < cutoff:
                done = True
                break
            if event_date > future_limit:
                continue
            if s.get("category") == 5:
                shows.append(s)
        if done or len(batch) < page_size:
            break
        offset += page_size
    return shows


def cablecast_fetch_vod_url(vod_id):
    """Return the direct MP4 download URL for a VOD, or None on error."""
    data = fetch_json(f"{CABLECAST_BASE}/cablecastapi/v1/vods/{vod_id}")
    if not data:
        return None
    vod = data.get("vod") or {}
    return vod.get("url")


# --- Utilities ---

def slugify(text, max_len=55):
    text = text.lower().strip()
    text = re.sub(r"[/\\&]", "-", text)
    text = re.sub(r"[^\w\s-]", "", text)
    text = re.sub(r"[\s_]+", "-", text)
    text = re.sub(r"-{2,}", "-", text)
    return text.strip("-")[:max_len]


def make_pdf_dest(board, doc_type, meeting_date, meeting_id, output_dir):
    date_str = meeting_date.strftime("%Y-%m-%d")
    month_dir = os.path.join(output_dir, meeting_date.strftime("%Y-%m"))
    os.makedirs(month_dir, exist_ok=True)
    board_slug = slugify(board, max_len=45)
    fname = f"{date_str}-{board_slug}-{meeting_id}-{doc_type}.pdf"
    return os.path.join(month_dir, fname)


def make_video_dest(show_id, title, meeting_date, output_dir, ext=".url"):
    date_str = meeting_date.strftime("%Y-%m-%d")
    month_dir = os.path.join(output_dir, meeting_date.strftime("%Y-%m"))
    os.makedirs(month_dir, exist_ok=True)
    title_slug = slugify(title, max_len=50)
    fname = f"{date_str}-{title_slug}-{show_id}{ext}"
    return os.path.join(month_dir, fname)


# --- Main ---

def main():
    parser = argparse.ArgumentParser(
        description=(
            "Download Trumbull CT municipal agendas, minutes, and meeting "
            "recordings for meetings within the past N days."
        )
    )
    parser.add_argument(
        "--days", type=int, default=DAYS_BACK, metavar="N",
        help=f"Look back N days by meeting date (default: {DAYS_BACK})",
    )
    parser.add_argument(
        "--ahead", type=int, default=DAYS_AHEAD, metavar="N",
        help=f"Also include meetings up to N days ahead (default: {DAYS_AHEAD})",
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
        help="Only include boards/shows whose name contains NAME (case-insensitive)",
    )
    parser.add_argument(
        "--no-minutes", action="store_true",
        help="Skip minutes, download agendas only",
    )
    parser.add_argument(
        "--no-agendas", action="store_true",
        help="Skip agendas, download minutes only",
    )
    parser.add_argument(
        "--no-video", action="store_true",
        help="Skip Cablecast meeting recordings entirely",
    )
    parser.add_argument(
        "--download-video", action="store_true",
        help=(
            "Download MP4 video files instead of saving watch-page .url shortcuts. "
            "Files are typically 1-2 GB each — use with caution."
        ),
    )
    args = parser.parse_args()

    today = datetime.date.today()
    cutoff = today - datetime.timedelta(days=args.days)
    future_limit = today + datetime.timedelta(days=args.ahead)

    # CivicEngage Search uses MM/DD/YYYY
    start_str = cutoff.strftime("%-m/%-d/%Y")
    end_str = future_limit.strftime("%-m/%-d/%Y")

    print(f"Date window : {cutoff} to {future_limit}")
    print(f"Hub page    : {HUB_URL}")
    if not args.dry_run:
        print(f"Output dir  : {args.output_dir}")
    print()

    # --- Step 1: get category IDs from hub ---
    print("Fetching hub page to discover board categories...")
    hub_html = fetch_html(HUB_URL)
    if not hub_html:
        print("ERROR: Could not fetch the hub page.", file=sys.stderr)
        sys.exit(1)

    cat_ids = parse_category_ids(hub_html)
    if not cat_ids:
        print("ERROR: No category IDs found — page structure may have changed.",
              file=sys.stderr)
        sys.exit(1)

    print(f"  Found {len(cat_ids)} board category/categories.")

    # --- Step 2: search for meetings in the date window ---
    print("Searching for meetings in date window...")
    search_params = {
        "term": "",
        "CIDs": ",".join(cat_ids),
        "startDate": start_str,
        "endDate": end_str,
        "dateRange": "custom",
        "dateSelector": "range",
    }
    search_html = fetch_html(SEARCH_URL, search_params)
    if not search_html:
        print("ERROR: Could not fetch search results.", file=sys.stderr)
        sys.exit(1)

    meetings = parse_meetings(search_html)
    print(f"  Found {len(meetings)} meeting(s) with documents in date window.")

    # Apply board filter to AgendaCenter meetings
    if args.board:
        filter_str = args.board.lower()
        meetings = [m for m in meetings if filter_str in m["board"].lower()]

    # Build list of PDF docs
    docs = []
    for mtg in meetings:
        if not args.no_agendas and mtg["agenda_url"]:
            docs.append({**mtg, "doc_type": "agenda", "url": mtg["agenda_url"]})
        if not args.no_minutes and mtg["minutes_url"]:
            docs.append({**mtg, "doc_type": "minutes", "url": mtg["minutes_url"]})

    docs.sort(key=lambda x: (x["meeting_date"], x["board"]), reverse=True)

    # --- Step 2b: Cablecast recordings ---
    recordings = []
    if not args.no_video:
        print("Fetching Cablecast Government Meeting recordings...")
        shows = cablecast_fetch_shows(cutoff, future_limit)
        print(f"  Found {len(shows)} recording(s) in date window.")

        board_filter = args.board.lower() if args.board else None
        for s in shows:
            title = s.get("title", "").strip()
            if board_filter and board_filter not in title.lower():
                continue
            event_date_str = (s.get("eventDate") or "")[:10]
            try:
                meeting_date = datetime.date.fromisoformat(event_date_str)
            except ValueError:
                continue
            vod_ids = s.get("vods") or []
            recordings.append({
                "show_id": s["id"],
                "title": title,
                "meeting_date": meeting_date,
                "vod_id": vod_ids[0] if vod_ids else None,
                "watch_url": CABLECAST_WATCH.format(show_id=s["id"]),
            })

        recordings.sort(key=lambda x: x["meeting_date"], reverse=True)

    print()

    if not docs and not recordings:
        print("Nothing found in date window.")
        return

    if args.dry_run:
        if docs:
            print(f"{'Board':<48} {'Date':<12} {'ID':<8} Type")
            print("-" * 80)
            for d in docs:
                print(
                    f"{d['board'][:47]:<48} "
                    f"{d['meeting_date']!s:<12} "
                    f"{d['meeting_id']:<8} "
                    f"{d['doc_type']}"
                )
            print(f"\n{len(docs)} PDF document(s).")

        if recordings:
            print()
            fmt = "mp4 download" if args.download_video else ".url shortcut"
            print(f"{'Title':<58} {'Date':<12} {'Show ID'}")
            print("-" * 82)
            for r in recordings:
                print(
                    f"{r['title'][:57]:<58} "
                    f"{r['meeting_date']!s:<12} "
                    f"{r['show_id']}"
                )
            print(f"\n{len(recordings)} recording(s) — will save as {fmt}.")

        print("\nRe-run without --dry-run to download.")
        return

    # --- Step 3: download PDFs ---
    os.makedirs(args.output_dir, exist_ok=True)
    log_path = os.path.join(args.output_dir, "download-log.txt")
    log_lines = []
    downloaded = skipped = failed = 0

    for d in docs:
        dest = make_pdf_dest(
            d["board"], d["doc_type"], d["meeting_date"],
            d["meeting_id"], args.output_dir,
        )
        label = os.path.basename(dest)

        if os.path.exists(dest):
            print(f"  skip (exists)  {label}")
            skipped += 1
            continue

        print(f"  [{d['meeting_date']}] {d['board'][:50]} — {d['doc_type']}")
        print(f"  saving         {label}")

        if download_file(d["url"], dest):
            downloaded += 1
            log_lines.append(
                f"{datetime.datetime.now().isoformat()}  OK       {dest}"
            )
        else:
            failed += 1
            log_lines.append(
                f"{datetime.datetime.now().isoformat()}  FAILED   {d['url']}"
            )
            if os.path.exists(dest):
                os.remove(dest)

        time.sleep(DELAY_SECONDS)

    # --- Step 4: save/download recordings ---
    vid_saved = vid_skipped = vid_failed = 0

    for r in recordings:
        ext = ".mp4" if args.download_video else ".url"
        dest = make_video_dest(
            r["show_id"], r["title"], r["meeting_date"], args.output_dir, ext=ext
        )
        label = os.path.basename(dest)

        if os.path.exists(dest):
            print(f"  skip (exists)  {label}")
            vid_skipped += 1
            continue

        print(f"  [{r['meeting_date']}] {r['title'][:55]}")
        print(f"  saving         {label}")

        if args.download_video:
            if not r["vod_id"]:
                print(f"  WARNING: no VOD ID for show {r['show_id']}", file=sys.stderr)
                vid_failed += 1
                continue
            mp4_url = cablecast_fetch_vod_url(r["vod_id"])
            if not mp4_url:
                print(f"  WARNING: could not fetch VOD URL for vod {r['vod_id']}",
                      file=sys.stderr)
                vid_failed += 1
                continue
            ok = download_file(mp4_url, dest, timeout=3600)
        else:
            ok = save_url_shortcut(r["watch_url"], dest)

        if ok:
            vid_saved += 1
            log_lines.append(
                f"{datetime.datetime.now().isoformat()}  OK       {dest}"
            )
        else:
            vid_failed += 1
            log_lines.append(
                f"{datetime.datetime.now().isoformat()}  FAILED   {r['watch_url']}"
            )
            if os.path.exists(dest):
                os.remove(dest)

        time.sleep(DELAY_SECONDS)

    if log_lines:
        with open(log_path, "a") as f:
            f.write("\n".join(log_lines) + "\n")

    print()
    print(f"Done — PDFs: downloaded={downloaded}  skipped={skipped}  failed={failed}")
    if recordings:
        print(f"      Video: saved={vid_saved}  skipped={vid_skipped}  failed={vid_failed}")
    if downloaded + skipped + vid_saved + vid_skipped:
        print(f"Files in: {args.output_dir}")
    if log_lines:
        print(f"Log:      {log_path}")


if __name__ == "__main__":
    main()


# --- Tips ---
#
# 1. Preview without downloading:
#    python3 scripts/download-trumbull-agendas.py --dry-run
#
# 2. Narrow to one board:
#    python3 scripts/download-trumbull-agendas.py --board "Planning"
#
# 3. Agendas only (skip minutes):
#    python3 scripts/download-trumbull-agendas.py --no-minutes
#
# 4. Skip recording shortcuts (PDFs only):
#    python3 scripts/download-trumbull-agendas.py --no-video
#
# 5. Download actual MP4 video files (1-2 GB each — use with caution):
#    python3 scripts/download-trumbull-agendas.py --download-video
#
# 6. Change the lookback window:
#    python3 scripts/download-trumbull-agendas.py --days 7
#
# 7. Save files somewhere else:
#    python3 scripts/download-trumbull-agendas.py --output-dir ~/Downloads/trumbull
#
# 8. Run on a schedule (cron — 7 AM daily):
#    0 7 * * * cd /path/to/repo && python3 scripts/download-trumbull-agendas.py
#
# 9. Process downloaded PDFs with Claude afterward:
#    python3 scripts/download-trumbull-agendas.py && bash scripts/batch-process.sh beat-archive/trumbull-agendas/
#
# NOTES:
#   - Trumbull CT uses CivicPlus CivicEngage for agendas/minutes. All board rows
#     are embedded inline in the Search results page — no AJAX calls needed.
#   - The Search endpoint accepts a comma-separated list of category IDs and
#     a date range. Category IDs are discovered dynamically from the hub page
#     on each run, so new boards are picked up automatically.
#   - Meeting dates are encoded as MMDDYYYY in ViewFile URL paths, e.g.:
#       /AgendaCenter/ViewFile/Agenda/_04092026-6066 → April 9, 2026
#   - ViewFile URLs serve PDFs directly with no authentication required.
#   - Government meeting recordings are hosted on Cablecast at
#     https://reflect-trumbulltv.cablecast.tv (operated by Trumbull Community
#     Television). The REST API at /cablecastapi/v1/shows is public.
#     Category 5 = "Government Meeting" (64+ meetings per 30-day window).
#   - Video files via --download-video are typically 1-2 GB each (full meetings).
#     The default .url shortcut opens the Cablecast watch page in a browser.
#   - The --ahead flag (default: 7 days) captures agendas for upcoming meetings
#     that have already been posted. Run daily to stay current.
