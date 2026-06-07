#!/usr/bin/env python3
# download-glastonbury-agendas.py
# Download municipal meeting agendas, minutes, and video recordings from
# Glastonbury CT for meetings whose date falls within the past N days
# (and up to 7 days ahead, to catch agendas posted early).
#
# USAGE:
#   python3 scripts/download-glastonbury-agendas.py [options]
#
# REQUIREMENTS:
#   - Python 3.6+  (no third-party packages needed)
#   - Internet connection
#
# WHAT IT DOES:
#   1. Discovers all board/committee agenda pages from the main website sidebar
#   2. Fetches each board's past and upcoming meeting listings (with pagination)
#   3. Downloads agenda and minutes PDFs (Vision Internet CMS)
#   4. Queries the Cablecast VOD API for video recordings
#   5. Downloads video recordings as MP4 files
#   6. Appends a download log to beat-archive/glastonbury-agendas/download-log.txt
#
# SITE STRUCTURE:
#   Documents (Vision Internet CMS):
#     Hub:      https://www.glastonburyct.gov/i-want-to/find/meeting-minutes-agendas
#     Board:    https://www.glastonburyct.gov/our-community/.../[board]/agendas-and-minutes
#     Past:     append /-toggle-allpast    (20 meetings per page)
#     Upcoming: append /-toggle-allupcoming
#     Page N:   append /-npage-N           (max page found in HTML links)
#     Doc:      https://www.glastonburyct.gov/home/showpublisheddocument/{id}
#
#   Video (Cablecast):
#     Shows:    GET https://vod.glastonbury-ct.gov/cablecastapi/v1/shows?site=1&offset=N
#               Returns up to 50 shows per page, sorted newest-first
#               Total ~1,019 shows as of May 2026
#     VOD:      GET https://vod.glastonbury-ct.gov/cablecastapi/v1/vods/{vod_id}
#     MP4 URL:  vod["url"]  (hosted on reflect-glastonburyct.cablecast.tv)
#
# NOTE: Video files are typically 500 MB–3 GB each. The script skips files
# that already exist, so you can re-run safely. Use --docs-only to skip video.
#
# NOTE: The Vision Internet site returns HTTP 403 to some automated user-agents.
# A Firefox-like UA is required (already set in this script).
#
# BOARDS (28 as of 2026):
#   Affordable Housing Committee, ASDRC, Board of Assessment Appeals,
#   Board of Finance, Commission on Aging, Commission on Racial Justice & Equity,
#   Community Beautification Committee, Conservation Commission,
#   Council/TP&Z Working Group, Economic Development Commission,
#   Ethics Commission, Fair Rent Commission, Fire Commission,
#   Great Pond Stewardship, Historic District Commission, Human Relations Commission,
#   Insurance Advisory Committee, Planning & Zoning Update Steering Committee,
#   Public Buildings Commission, Recreation Commission,
#   Road Safety Action Plan Steering Committee, Town Council,
#   Town Council Policy & Ordinance Review Subcommittee,
#   Town Plan and Zoning Commission, Water Pollution Control Authority,
#   Welles Turner Library Board, Youth and Family Services Commission,
#   Zoning Board of Appeals

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

BASE_URL = "https://www.glastonburyct.gov"
CABLECAST_URL = "https://vod.glastonbury-ct.gov"
CABLECAST_REFLECT_URL = "https://reflect-glastonburyct.cablecast.tv"
OUTPUT_DIR = "beat-archive/glastonbury-agendas"
DAYS_BACK = 4
DAYS_AHEAD = 7
PAGE_DELAY = 0.8
DOWNLOAD_DELAY = 0.8

# Board discovery: fetch the sidebar from any board page (all pages share the same nav)
BOARD_DISCOVERY_URL = (
    f"{BASE_URL}/our-community/about-us/town-government/"
    "town-leadership-council/town-council-agendas-and-minutes"
)

UA_DOC = (
    "Mozilla/5.0 (X11; Linux x86_64; rv:120.0) Gecko/20100101 Firefox/120.0"
)
UA_VIDEO = "Glastonbury-CT-Agendas-Downloader/1.0 (journalism research)"

# Fallback board list if discovery fails (URLs verified 2026-05-08)
FALLBACK_BOARDS = [
    ("Town Council",
     f"{BASE_URL}/our-community/about-us/town-government/town-leadership-council/"
     "town-council-agendas-and-minutes"),
    ("Affordable Housing Committee",
     f"{BASE_URL}/our-community/about-us/town-government/boards-commissions-committees/"
     "board-of-assessment-appeals/affordable-housing-committee-minutes-agendas"),
    ("ASDRC",
     f"{BASE_URL}/our-community/about-us/town-government/boards-commissions-committees/"
     "board-of-finance/asdrc-finance-minutes-and-agendas"),
    ("Board of Assessment Appeals",
     f"{BASE_URL}/our-community/about-us/town-government/boards-commissions-committees/"
     "board-of-assessment-appeals/minutes-agendas"),
    ("Board of Finance",
     f"{BASE_URL}/our-community/about-us/town-government/boards-commissions-committees/"
     "board-of-finance/board-of-finance-minutes-and-agendas"),
    ("Commission on Aging",
     f"{BASE_URL}/our-community/about-us/town-government/boards-commissions-committees/"
     "commission-on-aging/commission-on-aging-minutes-agendas"),
    ("Commission on Racial Justice and Equity",
     f"{BASE_URL}/our-community/about-us/town-government/boards-commissions-committees/"
     "board-of-finance/commission-on-racial-justice-and-equity-minutes-and-agendas"),
    ("Community Beautification Committee",
     f"{BASE_URL}/our-community/about-us/town-government/boards-commissions-committees/"
     "community-beautification-committee/community-beautification-committee-minutes-agendas"),
    ("Conservation Commission",
     f"{BASE_URL}/our-community/about-us/town-government/boards-commissions-committees/"
     "conservation-wetlands/conservation-commission-minutes-agendas"),
    ("Council/TP&Z Working Group",
     f"{BASE_URL}/our-community/about-us/town-government/boards-commissions-committees/"
     "board-of-finance/council-tp-z-building-zone-regulations-working-group-minutes-agendas"),
    ("Economic Development Commission",
     f"{BASE_URL}/our-community/about-us/town-government/boards-commissions-committees/"
     "economic-development-commission/economic-development-commission-minutes-agendas"),
    ("Ethics Commission",
     f"{BASE_URL}/our-community/about-us/town-government/boards-commissions-committees/"
     "ethics-commission/ethics-commission-minutes-agendas"),
    ("Fair Rent Commission",
     f"{BASE_URL}/our-community/about-us/town-government/boards-commissions-committees/"
     "fair-rent-commission/fair-rent-commission-minutes-agendas"),
    ("Fire Commission",
     f"{BASE_URL}/our-community/about-us/town-government/boards-commissions-committees/"
     "fire-commission/fire-commission-minutes-agendas"),
    ("Great Pond Stewardship Committee",
     f"{BASE_URL}/our-community/about-us/town-government/boards-commissions-committees/"
     "great-pond-preserve-stewardship-committee/great-pond-stewardship-minutes-agendas"),
    ("Historic District Commission",
     f"{BASE_URL}/our-community/about-us/town-government/boards-commissions-committees/"
     "historic-district-commission/historic-district-commission-minutes-agendas"),
    ("Human Relations Commission",
     f"{BASE_URL}/our-community/about-us/town-government/boards-commissions-committees/"
     "human-relations-commission/human-relations-commission-minutes-agendas"),
    ("Insurance Advisory Committee",
     f"{BASE_URL}/our-community/about-us/town-government/boards-commissions-committees/"
     "insurance-advisory-committee/insurance-advisory-committee-minutes-agendas"),
    ("Planning & Zoning Update Steering Committee",
     f"{BASE_URL}/our-community/about-us/town-government/boards-commissions-committees/"
     "conservation-wetlands/planning-zoning-update-steering-committee-minutes-agendas"),
    ("Public Buildings Commission",
     f"{BASE_URL}/our-community/about-us/town-government/boards-commissions-committees/"
     "public-buildings-commission/public-buildings-commission-minutes-agendas"),
    ("Recreation Commission",
     f"{BASE_URL}/our-community/about-us/town-government/boards-commissions-committees/"
     "recreation-commission/recreation-commission-minutes-agendas"),
    ("Road Safety Action Plan Steering Committee",
     f"{BASE_URL}/our-community/about-us/town-government/boards-commissions-committees/"
     "recreation-commission/road-safety-action-plan-steering-committee-minutes-and-agendas"),
    ("Town Council Policy & Ordinance Review Subcommittee",
     f"{BASE_URL}/our-community/about-us/town-government/boards-commissions-committees/"
     "council-tp-z-building-zone-regulations-working-group/"
     "town-council-policy-ordinance-review-subcommittee-minutes-agendas"),
    ("Town Plan and Zoning Commission",
     f"{BASE_URL}/our-community/about-us/town-government/boards-commissions-committees/"
     "town-plan-zoning-commission/planning-and-zoning-agendas-and-minutes"),
    ("Water Pollution Control Authority",
     f"{BASE_URL}/our-community/about-us/town-government/boards-commissions-committees/"
     "water-pollution-control-authority/water-pollution-control-authority-minutes-and-agendas"),
    ("Welles Turner Library Board",
     f"{BASE_URL}/our-community/about-us/town-government/boards-commissions-committees/"
     "welles-turner-library-board/welles-turner-library-board-minutes-agendas"),
    ("Youth and Family Services Commission",
     f"{BASE_URL}/our-community/about-us/town-government/boards-commissions-committees/"
     "youth-and-family-services-commission/youth-and-family-services-commission-meetings-agendas"),
    ("Zoning Board of Appeals",
     f"{BASE_URL}/our-community/about-us/town-government/boards-commissions-committees/"
     "zoning-board-of-appeals/zoning-board-of-appeals-minutes-agendas"),
]


# --- HTTP helpers ---

def fetch_html(url):
    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": UA_DOC,
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.5",
        },
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


def fetch_json(url):
    req = urllib.request.Request(
        url,
        headers={"User-Agent": UA_VIDEO, "Accept": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return json.loads(r.read())
    except urllib.error.HTTPError as e:
        print(f"  HTTP {e.code} — {url}", file=sys.stderr)
        return None
    except Exception as e:
        print(f"  ERROR fetching {url}: {e}", file=sys.stderr)
        return None


def download_file(url, dest_path):
    """Download url to dest_path. Returns True on success."""
    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": UA_DOC,
            "Accept": "application/pdf,video/mp4,*/*",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=120) as r:
            with open(dest_path, "wb") as f:
                while True:
                    chunk = r.read(65536)
                    if not chunk:
                        break
                    f.write(chunk)
        return True
    except Exception as e:
        print(f"  WARNING: {e}", file=sys.stderr)
        return False


# --- Board discovery ---

def discover_boards():
    """
    Fetch the sidebar nav from any board page to get all 28 board URLs.
    Uses aria-label attributes for proper board names.
    Falls back to FALLBACK_BOARDS on failure.
    Returns list of (name, url) tuples.
    """
    import html as _html
    html_text = fetch_html(BOARD_DISCOVERY_URL)
    if not html_text:
        print("  Board discovery failed — using fallback list.", file=sys.stderr)
        return list(FALLBACK_BOARDS)

    seen = set()
    boards = []

    # Match sidebar links: href='...' target='_self' aria-label='Board Name'
    pattern = re.compile(
        r"href='(https://www\.glastonburyct\.gov/our-community[^']+)'"
        r"\s+target='_self'\s+aria-label='([^']+)'",
        re.IGNORECASE,
    )
    for m in pattern.finditer(html_text):
        href = m.group(1)
        label = _html.unescape(m.group(2))
        href_clean = re.sub(r"/-toggle-[^/']+", "", href).rstrip("/")
        if (
            ("agendas" in href_clean or "minutes" in href_clean)
            and href_clean not in seen
        ):
            seen.add(href_clean)
            boards.append((label, href_clean))

    if not boards:
        print("  Board discovery returned no results — using fallback list.", file=sys.stderr)
        return list(FALLBACK_BOARDS)

    return boards


# --- Document parsing ---

# Matches a single event row (tr) in the meetings table
_TR_RE = re.compile(r"<tr[^>]*>(.*?)</tr>", re.DOTALL)

# Matches the meeting date in the event_datetime cell: MM/DD/YYYY
_DATE_RE = re.compile(r"(\d{2})/(\d{2})/(\d{4})")

# Matches showpublisheddocument links with label text
_DOC_LINK_RE = re.compile(
    r"<span[^>]*class=[\"']agenda-minutes-label[\"'][^>]*>(.*?)</span>"
    r".*?"
    r"href=[\"'](/home/showpublisheddocument/\d+(?:/\d+)?)[\"']"
    r"[^>]*>(.*?)</a>",
    re.DOTALL,
)

# Pagination: find highest /-npage-N in page HTML
_NPAGE_RE = re.compile(r"/-npage-(\d+)")


def parse_max_page(html):
    pages = [int(m.group(1)) for m in _NPAGE_RE.finditer(html)]
    return max(pages) if pages else 1


def parse_event_rows(html, board_name):
    """
    Extract meeting events from a board page. Returns a list of dicts:
      {board, meeting_date, doc_type, doc_url, doc_label}
    """
    items = []
    for m in _TR_RE.finditer(html):
        row = m.group(1)
        if "event_title" not in row and "event_datetime" not in row:
            continue

        date_match = _DATE_RE.search(row)
        if not date_match:
            continue
        mm, dd, yyyy = int(date_match.group(1)), int(date_match.group(2)), int(date_match.group(3))
        try:
            meeting_date = datetime.date(yyyy, mm, dd)
        except ValueError:
            continue

        for dm in _DOC_LINK_RE.finditer(row):
            label_text = re.sub(r"<[^>]+>", "", dm.group(1)).strip().lower().rstrip(":")
            doc_url = dm.group(2)
            doc_title = re.sub(r"<[^>]+>", "", dm.group(3)).strip()

            if label_text == "agenda":
                doc_type = "agenda"
            elif label_text == "minutes":
                doc_type = "minutes"
            else:
                continue

            items.append({
                "board": board_name,
                "meeting_date": meeting_date,
                "doc_type": doc_type,
                "doc_url": doc_url,
                "doc_label": doc_title,
            })

    return items


def fetch_board_docs(board_name, board_url, cutoff, future_limit):
    """
    Fetch all documents in [cutoff, future_limit] from a board page.
    Fetches both allpast and allupcoming to catch early-posted agendas.
    Returns a list of document dicts.
    """
    all_docs = []

    for toggle in ("allpast", "allupcoming"):
        toggle_url = board_url + f"/-toggle-{toggle}"
        page = 1
        max_page = 1

        while page <= max_page:
            if page == 1:
                url = toggle_url
            else:
                url = toggle_url + f"/-npage-{page}"

            html = fetch_html(url)
            if not html:
                break

            if page == 1:
                max_page = parse_max_page(html)

            rows = parse_event_rows(html, board_name)
            in_window = [
                d for d in rows
                if cutoff <= d["meeting_date"] <= future_limit
            ]
            all_docs.extend(in_window)

            # For allpast (newest-first), stop early if all dates on page are before cutoff
            if toggle == "allpast" and rows:
                oldest_on_page = min(d["meeting_date"] for d in rows)
                if oldest_on_page < cutoff:
                    break

            page += 1
            if page <= max_page:
                time.sleep(PAGE_DELAY)

    return all_docs


# --- Video (Cablecast) ---

def fetch_shows_in_window(cutoff, future_limit):
    """
    Page through the Cablecast shows API and return all shows whose
    eventDate falls in [cutoff, future_limit]. Stops paging once
    the oldest show on a page is before cutoff.
    """
    shows = []
    offset = 0
    total = None

    while True:
        url = f"{CABLECAST_URL}/cablecastapi/v1/shows?site=1&offset={offset}"
        data = fetch_json(url)
        if not data or "shows" not in data:
            break

        if total is None:
            total = data.get("meta", {}).get("count", 0)

        page_shows = data["shows"]
        if not page_shows:
            break

        oldest_date = None
        for show in page_shows:
            raw_date = show.get("eventDate", "")
            try:
                event_date = datetime.date.fromisoformat(raw_date[:10])
            except (ValueError, TypeError):
                continue

            if cutoff <= event_date <= future_limit and show.get("title") and show.get("vods"):
                shows.append({
                    "id": show["id"],
                    "title": show["title"],
                    "event_date": event_date,
                    "vod_ids": show["vods"],
                })

            if oldest_date is None or event_date < oldest_date:
                oldest_date = event_date

        # Shows are sorted newest-first; stop once we're past the lookback window
        if oldest_date and oldest_date < cutoff:
            break

        offset += len(page_shows)
        if offset >= (total or 0):
            break

        time.sleep(PAGE_DELAY)

    return shows


def get_vod_url(vod_id):
    """Return the direct MP4 download URL for a Cablecast VOD ID, or None."""
    url = f"{CABLECAST_URL}/cablecastapi/v1/vods/{vod_id}"
    data = fetch_json(url)
    if not data:
        return None
    # Response may be {"vod": {...}} or the vod object directly
    vod = data.get("vod", data) if isinstance(data, dict) else None
    if not vod:
        return None
    vod_url = vod.get("url")
    if vod_url and vod.get("vodState") == "complete" and not vod.get("disabled"):
        return vod_url
    return None


# --- File naming ---

def slugify(text, max_len=50):
    text = text.lower().strip()
    text = re.sub(r"[/\\&]", "-", text)
    text = re.sub(r"\s+-\s+", "-", text)
    text = re.sub(r"[^\w\s-]", "", text)
    text = re.sub(r"[\s_]+", "-", text)
    text = re.sub(r"-{2,}", "-", text)
    return text.strip("-")[:max_len]


def make_doc_path(board, doc_type, meeting_date, output_dir, counter=0):
    date_str = meeting_date.strftime("%Y-%m-%d")
    month_dir = os.path.join(output_dir, meeting_date.strftime("%Y-%m"))
    os.makedirs(month_dir, exist_ok=True)
    board_slug = slugify(board)
    suffix = f"-{counter}" if counter > 0 else ""
    return os.path.join(month_dir, f"{date_str}-{board_slug}-{doc_type}{suffix}.pdf")


def make_video_path(show_title, event_date, output_dir, counter=0):
    date_str = event_date.strftime("%Y-%m-%d")
    month_dir = os.path.join(output_dir, event_date.strftime("%Y-%m"))
    os.makedirs(month_dir, exist_ok=True)
    title_slug = slugify(show_title)
    suffix = f"-{counter}" if counter > 0 else ""
    return os.path.join(month_dir, f"{date_str}-{title_slug}{suffix}.mp4")


# --- Main ---

def main():
    parser = argparse.ArgumentParser(
        description=(
            "Download Glastonbury CT municipal agendas, minutes, and video recordings "
            "for meetings within the past N days."
        )
    )
    parser.add_argument(
        "--days", type=int, default=DAYS_BACK, metavar="N",
        help=f"Look back N days by meeting date (default: {DAYS_BACK})",
    )
    parser.add_argument(
        "--ahead", type=int, default=DAYS_AHEAD, metavar="N",
        help=f"Include upcoming meetings up to N days ahead (default: {DAYS_AHEAD})",
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
        help="Only fetch boards whose name contains NAME (case-insensitive)",
    )
    parser.add_argument(
        "--docs-only", action="store_true",
        help="Download PDFs only, skip video recordings",
    )
    parser.add_argument(
        "--videos-only", action="store_true",
        help="Download video recordings only, skip PDFs",
    )
    parser.add_argument(
        "--no-minutes", action="store_true",
        help="Skip minutes, download agendas only",
    )
    parser.add_argument(
        "--no-agendas", action="store_true",
        help="Skip agendas, download minutes only",
    )
    args = parser.parse_args()

    today = datetime.date.today()
    cutoff = today - datetime.timedelta(days=args.days)
    future_limit = today + datetime.timedelta(days=args.ahead)

    print(f"Date window : {cutoff} to {future_limit}")
    if not args.dry_run:
        print(f"Output dir  : {args.output_dir}")
    print()

    log_lines = []
    downloaded = skipped = failed = 0
    all_docs: list = []
    shows: list = []

    # ------------------------------------------------------------------ #
    # Part 1: PDFs (Vision Internet CMS)                                  #
    # ------------------------------------------------------------------ #

    if not args.videos_only:
        print("Discovering board pages...")
        boards = discover_boards()

        if args.board:
            filter_str = args.board.lower()
            boards = [(n, u) for n, u in boards if filter_str in n.lower()]
            print(f"  Filtered to {len(boards)} board(s) matching '{args.board}'.")
        else:
            print(f"  Found {len(boards)} board/committee page(s).")
        print()

        print(f"Fetching board meeting listings ({len(boards)} boards)...")
        for i, (board_name, board_url) in enumerate(boards, 1):
            print(f"  [{i:>2}/{len(boards)}] {board_name}...", end=" ", flush=True)
            docs = fetch_board_docs(board_name, board_url, cutoff, future_limit)
            in_window = [
                d for d in docs
                if (not args.no_agendas or d["doc_type"] != "agenda")
                and (not args.no_minutes or d["doc_type"] != "minutes")
            ]
            print(f"{len(in_window)} doc(s)")
            all_docs.extend(in_window)
            time.sleep(PAGE_DELAY)

        all_docs.sort(key=lambda x: (x["meeting_date"], x["board"]), reverse=True)

        print()
        print(
            f"Found {len(all_docs)} PDF document(s) across "
            f"{len({d['board'] for d in all_docs})} board(s)."
        )
        print()

        if args.dry_run and all_docs:
            print(f"{'Board':<40} {'Date':<12} Type   Document label")
            print("-" * 90)
            for d in all_docs:
                print(
                    f"{d['board'][:39]:<40} {d['meeting_date']!s:<12} "
                    f"{d['doc_type']:<7} {d['doc_label'][:40]}"
                )

        elif all_docs:
            os.makedirs(args.output_dir, exist_ok=True)
            fname_counters: dict = {}

            for d in all_docs:
                key = (slugify(d["board"]), d["meeting_date"], d["doc_type"])
                fname_counters[key] = fname_counters.get(key, 0) + 1
                counter = fname_counters[key] - 1

                dest = make_doc_path(
                    d["board"], d["doc_type"], d["meeting_date"],
                    args.output_dir, counter,
                )
                label = os.path.basename(dest)

                if os.path.exists(dest):
                    print(f"  skip (exists)  {label}")
                    skipped += 1
                    continue

                doc_url = f"{BASE_URL}{d['doc_url']}" if d["doc_url"].startswith("/") else d["doc_url"]
                print(f"  [{d['meeting_date']}] {d['board']} — {d['doc_type']}")
                print(f"  downloading    {label}")

                if download_file(doc_url, dest):
                    downloaded += 1
                    log_lines.append(
                        f"{datetime.datetime.now().isoformat()}  OK       {dest}"
                    )
                else:
                    failed += 1
                    log_lines.append(
                        f"{datetime.datetime.now().isoformat()}  FAILED   {doc_url}"
                    )
                    if os.path.exists(dest):
                        os.remove(dest)

                time.sleep(DOWNLOAD_DELAY)

    # ------------------------------------------------------------------ #
    # Part 2: Video recordings (Cablecast)                                #
    # ------------------------------------------------------------------ #

    if not args.docs_only:
        print()
        print("Querying Cablecast VOD API for recordings...")
        shows = fetch_shows_in_window(cutoff, future_limit)

        if args.board:
            filter_str = args.board.lower()
            shows = [s for s in shows if filter_str in s["title"].lower()]

        shows.sort(key=lambda s: s["event_date"], reverse=True)
        print(f"  Found {len(shows)} recording(s) in window.")
        print()

        if args.dry_run and shows:
            print(f"{'Date':<12} {'Title':<50} VOD IDs")
            print("-" * 75)
            for s in shows:
                print(f"{s['event_date']!s:<12} {s['title'][:49]:<50} {s['vod_ids']}")

        elif shows:
            os.makedirs(args.output_dir, exist_ok=True)
            title_counters: dict = {}

            for show in shows:
                for vod_id in show["vod_ids"]:
                    print(
                        f"  [{show['event_date']}] {show['title']} — VOD {vod_id}",
                        end=" ",
                        flush=True,
                    )

                    key = (slugify(show["title"]), show["event_date"])
                    title_counters[key] = title_counters.get(key, 0) + 1
                    counter = title_counters[key] - 1

                    dest = make_video_path(
                        show["title"], show["event_date"], args.output_dir, counter
                    )
                    label = os.path.basename(dest)

                    if os.path.exists(dest):
                        print(f"skip (exists): {label}")
                        skipped += 1
                        continue

                    vod_url = get_vod_url(vod_id)
                    if not vod_url:
                        print("no URL (processing or disabled)")
                        failed += 1
                        log_lines.append(
                            f"{datetime.datetime.now().isoformat()}  NO_URL   vod_id={vod_id} show={show['title']}"
                        )
                        continue

                    print(f"downloading → {label}")
                    if download_file(vod_url, dest):
                        size_mb = os.path.getsize(dest) / (1024 * 1024)
                        downloaded += 1
                        log_lines.append(
                            f"{datetime.datetime.now().isoformat()}  OK       {dest}  ({size_mb:.0f} MB)"
                        )
                    else:
                        failed += 1
                        log_lines.append(
                            f"{datetime.datetime.now().isoformat()}  FAILED   {vod_url}"
                        )
                        if os.path.exists(dest):
                            os.remove(dest)

                    time.sleep(DOWNLOAD_DELAY)

    # ------------------------------------------------------------------ #
    # Summary and log                                                     #
    # ------------------------------------------------------------------ #

    if args.dry_run:
        total_found = (len(all_docs) if not args.videos_only else 0) + (len(shows) if not args.docs_only else 0)
        print(f"\n{total_found} item(s) matched. Re-run without --dry-run to download.")
        return

    if log_lines:
        log_path = os.path.join(args.output_dir, "download-log.txt")
        with open(log_path, "a") as f:
            f.write("\n".join(log_lines) + "\n")

    print()
    print(f"Done — downloaded: {downloaded}  skipped: {skipped}  failed: {failed}")
    if downloaded + skipped:
        print(f"Files in: {args.output_dir}")
    if log_lines:
        print(f"Log:      {os.path.join(args.output_dir, 'download-log.txt')}")


if __name__ == "__main__":
    main()


# --- Tips ---
#
# 1. Preview without downloading:
#    python3 scripts/download-glastonbury-agendas.py --dry-run
#
# 2. PDFs only (skip the large video files):
#    python3 scripts/download-glastonbury-agendas.py --docs-only
#
# 3. Videos only:
#    python3 scripts/download-glastonbury-agendas.py --videos-only
#
# 4. Filter to a single board:
#    python3 scripts/download-glastonbury-agendas.py --board "Town Council"
#    python3 scripts/download-glastonbury-agendas.py --board "Zoning"
#
# 5. Change the lookback window:
#    python3 scripts/download-glastonbury-agendas.py --days 7
#
# 6. Save files somewhere else:
#    python3 scripts/download-glastonbury-agendas.py --output-dir ~/Downloads/glastonbury
#
# 7. Run on a schedule (cron — 8 AM daily):
#    0 8 * * * cd /path/to/repo && python3 scripts/download-glastonbury-agendas.py --docs-only
#
# 8. Process downloaded PDFs with Claude afterward:
#    python3 scripts/download-glastonbury-agendas.py --docs-only && \
#      bash scripts/batch-process.sh beat-archive/glastonbury-agendas/
#
# NOTE: Video files are typically 500 MB–3 GB each. The script skips files
# that already exist (by filename), so you can safely re-run after interruptions.
#
# NOTE: The Vision Internet site (www.glastonburyct.gov) uses Akamai edge caching
# and blocks some automated user-agents. The Firefox UA string used here works
# reliably. If you see a sustained run of HTTP 403 responses, wait a few minutes
# before retrying.
