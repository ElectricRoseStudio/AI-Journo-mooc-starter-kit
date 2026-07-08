#!/usr/bin/env python3
# download-oxford-agendas.py
# Download municipal meeting agendas, minutes, and YouTube recordings from Oxford CT
# for meetings whose date falls within the past N days (and up to 7 days ahead).
#
# USAGE:
#   python3 scripts/download-oxford-agendas.py [options]
#
# REQUIREMENTS:
#   - Python 3.6+  (no third-party packages needed)
#   - yt-dlp installed (for video downloads only): pip install yt-dlp
#   - Internet connection
#
# WHAT IT DOES:
#   1. Fetches the Oxford CT Agenda Center hub page to discover all board
#      category IDs (up to 36 boards)
#   2. Calls the AgendaCenter Search endpoint with those IDs and a date range
#      — this returns all matching meeting rows in a single HTML response
#   3. Parses rows for Agenda and Minutes ViewFile URLs
#   4. Downloads PDFs to beat-archive/oxford-agendas/YYYY-MM/
#   5. Fetches YouTube RSS feeds for two board channels and filters by date
#   6. Downloads recordings with yt-dlp
#   7. Appends a download log to beat-archive/oxford-agendas/download-log.txt
#
# SITE STRUCTURE:
#   AgendaCenter (CivicPlus CivicEngage):
#     Hub:    https://www.oxford-ct.gov/agendacenter
#     Search: GET /AgendaCenter/Search/
#               ?term=&CIDs={cat1,cat2,...}&startDate=M/D/YYYY
#               &endDate=M/D/YYYY&dateRange=custom&dateSelector=0
#     Agenda: /AgendaCenter/ViewFile/Agenda/_{MMDDYYYY}-{meetingID}
#     Minutes:/AgendaCenter/ViewFile/Minutes/_{MMDDYYYY}-{meetingID}
#
#   YouTube recordings — two official channels:
#     Planning & Zoning Commission:
#       Channel ID : UCu9jwntiynuBbB-uvhMjCug
#       All uploads are P&Z meetings or public hearings.
#     Board of Selectmen:
#       Channel ID : UC6RebHWUhp3s-iGJZqmL77Q
#       Uploads include some test videos ("My Broadcast", "Test ...") that are
#       included when they fall within the date window; users can discard them.
#
#   NOTE: Each RSS feed returns a maximum of 15 videos. Run this script at least
#   weekly to ensure no recordings are missed.
#
# BOARDS (up to 36 categories):
#   Affordable Housing Plan Committee, Agriculture Advisory Committee,
#   Animal Control Shelter, Assessment Appeals Board, Board of Finance,
#   Board of Selectmen, Charter Revision 2026, Community Support Committee,
#   Conservation Commission-Inland Wetlands Agency, Cultural Arts Commission,
#   Economic Development, Economic Development Commission, Elderly Commission,
#   Fire Department Chiefs, Flood Victims Memorial Advisory Committee,
#   Great Oak Middle School Evaluation and Improvement Committee,
#   Health & Safety Committee, Housing Authority, Library Board of Directors,
#   Main Street Project Committee, Open Space Trail Management Committee,
#   Oxford Addictive Substances Information Services (OASIS),
#   Oxford Ambulance Board of Directors, Oxford School Building Committee,
#   Parks and Recreation Commission, Plan of Conservation & Development,
#   Planning & Zoning Department, Planning and Zoning Commission, Senior Center,
#   Tetlak Park Fields, Town Meeting, Town wide Rubbish Removal Study Committee,
#   Townwide Rubbish Removal, Water Pollution Control Authority,
#   Zoning Board of Appeals

import argparse
import datetime
import html as html_module
import os
import re
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET

YT_DLP_NODE = "node:/home/richkirby/.local/bin/yt-dlp-node"  # yt-dlp needs Node 22+; symlink kept current by scripts/update-yt-dlp-node.sh

# --- Configuration ---
BASE_URL = "https://www.oxford-ct.gov"
HUB_URL = f"{BASE_URL}/agendacenter"
SEARCH_URL = f"{BASE_URL}/AgendaCenter/Search/"

# Official Oxford CT YouTube channels
YT_CHANNELS = [
    {
        "id": "UCu9jwntiynuBbB-uvhMjCug",
        "name": "Planning & Zoning Commission",
    },
    {
        "id": "UC6RebHWUhp3s-iGJZqmL77Q",
        "name": "Board of Selectmen",
    },
]

OUTPUT_DIR = "beat-archive/oxford-agendas"
DAYS_BACK = 4
DAYS_AHEAD = 7
DELAY_SECONDS = 0.5

UA = "Mozilla/5.0 (X11; Linux x86_64; rv:120.0) Gecko/20100101 Firefox/120.0"

# Parses _MMDDYYYY-meetingID from ViewFile paths
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


def download_file(url, dest_path):
    """Download url to dest_path. Returns True on success."""
    req = urllib.request.Request(
        url,
        headers={"User-Agent": UA, "Accept": "application/pdf, */*"},
    )
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            with open(dest_path, "wb") as f:
                f.write(r.read())
        return True
    except Exception as e:
        print(f"  WARNING: {e}", file=sys.stderr)
        return False


# --- PDF parsing (CivicEngage) ---

def parse_category_ids(hub_html):
    """Extract all board category IDs from the AgendaCenter hub page."""
    return list(dict.fromkeys(re.findall(r'id="cat(\d+)"', hub_html)))


def parse_meetings(search_html):
    """
    Parse meeting rows from the AgendaCenter Search results page.

    Returns a list of dicts:
      {board, meeting_date, meeting_id, agenda_url, minutes_url}
    """
    # Build board name map from id="cat{id}" headings
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

            agenda_m = re.search(
                r'href="(/AgendaCenter/ViewFile/Agenda/(_\d{8}-\d+))"',
                row_html,
            )
            if not agenda_m:
                continue
            agenda_path = agenda_m.group(1)
            date_id_str = agenda_m.group(2)

            dm = _DATE_ID_RE.match(date_id_str)
            if not dm:
                continue
            mm, dd, yyyy, meeting_id = dm.groups()
            try:
                meeting_date = datetime.date(int(yyyy), int(mm), int(dd))
            except ValueError:
                continue

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


# --- YouTube helpers ---

def fetch_yt_videos_in_window(channel_id, channel_name, cutoff, future_limit):
    """
    Fetch recent videos from a YouTube RSS feed and filter to the date window.
    RSS feeds return the latest ~15 videos. Run this script at least weekly.
    """
    rss_url = f"https://www.youtube.com/feeds/videos.xml?channel_id={channel_id}"
    raw = fetch_html(rss_url)
    if not raw:
        print(f"  WARNING: Could not fetch YouTube RSS for {channel_name}.", file=sys.stderr)
        return []
    try:
        root = ET.fromstring(raw)
    except ET.ParseError as e:
        print(f"  WARNING: Could not parse YouTube RSS for {channel_name}: {e}", file=sys.stderr)
        return []

    ns = {
        "atom": "http://www.w3.org/2005/Atom",
        "yt": "http://www.youtube.com/xml/schemas/2015",
    }
    videos = []
    for entry in root.findall("atom:entry", ns):
        video_id = entry.findtext("yt:videoId", namespaces=ns)
        title_el = entry.find("atom:title", ns)
        title = title_el.text if title_el is not None else ""
        published_el = entry.find("atom:published", ns)
        published_str = published_el.text if published_el is not None else ""
        if not (video_id and title and published_str):
            continue
        try:
            pub_date = datetime.date.fromisoformat(published_str[:10])
        except ValueError:
            continue
        if cutoff <= pub_date <= future_limit:
            videos.append({
                "video_id": video_id,
                "title": title,
                "published": pub_date,
                "channel_name": channel_name,
            })
    return videos


def is_in_yt_archive(archive_path, video_id):
    """Return True if video_id is already in the yt-dlp download archive."""
    if not os.path.exists(archive_path):
        return False
    needle = f"youtube {video_id}"
    with open(archive_path) as f:
        return any(needle in line for line in f)


def download_yt_video(video_id, title, channel_name, pub_date, output_dir, archive_path):
    """
    Download a YouTube video with yt-dlp.
    Returns 'downloaded', 'skipped', or 'failed'.
    """
    month_dir = os.path.join(output_dir, pub_date.strftime("%Y-%m"))
    os.makedirs(month_dir, exist_ok=True)
    date_str = pub_date.strftime("%Y-%m-%d")
    channel_slug = slugify(channel_name, max_len=30)
    title_slug = slugify(title, max_len=40)
    outtmpl = os.path.join(
        month_dir, f"{date_str}-{channel_slug}-{title_slug}-{video_id}.%(ext)s"
    )
    url = f"https://www.youtube.com/watch?v={video_id}"

    cmd = [
        "yt-dlp", "--js-runtimes", YT_DLP_NODE,
        "--no-playlist",
        "--merge-output-format", "mp4",
        "--download-archive", archive_path,
        "-o", outtmpl,
        "-q", "--no-warnings",
        url,
    ]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=3600)
        if result.returncode == 0:
            return "downloaded"
        return "failed"
    except subprocess.TimeoutExpired:
        print(f"  WARNING: yt-dlp timed out for {video_id}", file=sys.stderr)
        return "failed"
    except FileNotFoundError:
        print(
            "  ERROR: yt-dlp not found. Install with: pip install yt-dlp",
            file=sys.stderr,
        )
        return "failed"


# --- Utilities ---

def slugify(text, max_len=55):
    text = text.lower().strip()
    text = re.sub(r"[/\\&]", "-", text)
    text = re.sub(r"[^\w\s-]", "", text)
    text = re.sub(r"[\s_]+", "-", text)
    text = re.sub(r"-{2,}", "-", text)
    return text.strip("-")[:max_len]


def make_dest_path(board, doc_type, meeting_date, meeting_id, output_dir):
    date_str = meeting_date.strftime("%Y-%m-%d")
    month_dir = os.path.join(output_dir, meeting_date.strftime("%Y-%m"))
    os.makedirs(month_dir, exist_ok=True)
    board_slug = slugify(board, max_len=45)
    fname = f"{date_str}-{board_slug}-{meeting_id}-{doc_type}.pdf"
    return os.path.join(month_dir, fname)


# --- Main ---

def main():
    parser = argparse.ArgumentParser(
        description=(
            "Download Oxford CT municipal agendas, minutes, and meeting recordings "
            "for meetings within the past N days."
        )
    )
    parser.add_argument(
        "--days", type=int, default=DAYS_BACK, metavar="N",
        help=f"Look back N days by meeting/publish date (default: {DAYS_BACK})",
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
        help="Only include boards/video titles containing NAME (case-insensitive)",
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
        "--docs-only", action="store_true",
        help="Download PDFs only, skip video recordings",
    )
    parser.add_argument(
        "--videos-only", action="store_true",
        help="Download video recordings only, skip PDFs",
    )
    args = parser.parse_args()

    now = datetime.datetime.now()
    if (now.weekday() == 5 and now.hour >= 18) or (now.weekday() == 6 and now.hour < 12):  # Saturday night, Sunday morning
        print("Skipping — no downloads on Saturday nights or Sunday mornings.")
        sys.exit(0)

    today = datetime.date.today()
    cutoff = today - datetime.timedelta(days=args.days)
    future_limit = today + datetime.timedelta(days=args.ahead)

    # CivicEngage Search uses M/D/YYYY (no zero-padding on Linux)
    start_str = cutoff.strftime("%-m/%-d/%Y")
    end_str = future_limit.strftime("%-m/%-d/%Y")

    print(f"Date window : {cutoff} to {future_limit}")
    print(f"Hub page    : {HUB_URL}")
    if not args.dry_run:
        print(f"Output dir  : {args.output_dir}")
    print()

    all_docs = []
    yt_videos = []

    # --- Step 1: PDFs from AgendaCenter ---
    if not args.videos_only:
        print("Fetching hub page to discover board categories...")
        hub_html = fetch_html(HUB_URL)
        if not hub_html:
            print("ERROR: Could not fetch the hub page.", file=sys.stderr)
            sys.exit(1)
        cat_ids = parse_category_ids(hub_html)
        if not cat_ids:
            print(
                "ERROR: No category IDs found — page structure may have changed.",
                file=sys.stderr,
            )
            sys.exit(1)
        print(f"  Found {len(cat_ids)} board category/categories.")

        print("Searching for meetings in date window...")
        search_params = {
            "term": "",
            "CIDs": ",".join(cat_ids),
            "startDate": start_str,
            "endDate": end_str,
            "dateRange": "custom",
            "dateSelector": "0",
        }
        search_html = fetch_html(SEARCH_URL, search_params)
        if not search_html:
            print("ERROR: Could not fetch search results.", file=sys.stderr)
            sys.exit(1)
        meetings = parse_meetings(search_html)
        print(f"  Found {len(meetings)} meeting(s) with documents in date window.")
        print()

        if args.board:
            filter_str = args.board.lower()
            meetings = [m for m in meetings if filter_str in m["board"].lower()]
            print(f"  Filtered to {len(meetings)} meeting(s) matching '{args.board}'.")
            print()

        for mtg in meetings:
            if not args.no_agendas and mtg["agenda_url"]:
                all_docs.append({**mtg, "doc_type": "agenda", "url": mtg["agenda_url"]})
            if not args.no_minutes and mtg["minutes_url"]:
                all_docs.append({**mtg, "doc_type": "minutes", "url": mtg["minutes_url"]})
        all_docs.sort(key=lambda x: (x["meeting_date"], x["board"]), reverse=True)

    # --- Step 2: Videos from YouTube ---
    if not args.docs_only:
        board_filter = args.board.lower() if args.board else None
        print("Fetching YouTube RSS feeds for meeting recordings...")
        for channel in YT_CHANNELS:
            if board_filter and board_filter not in channel["name"].lower():
                continue
            videos = fetch_yt_videos_in_window(
                channel["id"], channel["name"], cutoff, future_limit
            )
            # Apply board filter to video titles too
            if board_filter:
                videos = [v for v in videos if board_filter in v["title"].lower()
                          or board_filter in v["channel_name"].lower()]
            print(f"  {channel['name']}: {len(videos)} recording(s) in window.")
            if len(videos) >= 15:
                print(
                    "    NOTE: RSS returned 15 videos (max) — older recordings may be missed.\n"
                    "          Run this script at least weekly to stay current."
                )
            yt_videos.extend(videos)
        print()

    if not all_docs and not yt_videos:
        print("No documents or recordings found in the date window.")
        return

    # --- Dry-run listing ---
    if args.dry_run:
        if all_docs:
            print(f"{'Board':<40} {'Date':<12} {'Meeting ID':<10} Type")
            print("-" * 75)
            for d in all_docs:
                print(
                    f"{d['board'][:39]:<40} "
                    f"{d['meeting_date']!s:<12} "
                    f"{d['meeting_id']:<10} "
                    f"{d['doc_type']}"
                )
            print()
        if yt_videos:
            print(f"{'Channel':<35} {'Published':<12} Video Title")
            print("-" * 85)
            for v in sorted(yt_videos, key=lambda x: x["published"], reverse=True):
                print(
                    f"{v['channel_name'][:34]:<35} "
                    f"{v['published']!s:<12} "
                    f"{v['title']}"
                )
            print()
        total = len(all_docs) + len(yt_videos)
        print(f"{total} item(s) matched. Re-run without --dry-run to download.")
        return

    # --- Download PDFs ---
    os.makedirs(args.output_dir, exist_ok=True)
    log_path = os.path.join(args.output_dir, "download-log.txt")
    log_lines = []
    downloaded = skipped = failed = 0

    if all_docs:
        print(f"Downloading {len(all_docs)} document(s)...")
        for d in all_docs:
            dest = make_dest_path(
                d["board"], d["doc_type"], d["meeting_date"],
                d["meeting_id"], args.output_dir,
            )
            label = os.path.basename(dest)

            if os.path.exists(dest):
                print(f"  skip (exists)  {label}")
                skipped += 1
                continue

            print(f"  [{d['meeting_date']}] {d['board'][:45]} — {d['doc_type']}")
            print(f"  downloading    {label}")
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
        print()

    # --- Download YouTube videos ---
    if yt_videos:
        archive_path = os.path.join(args.output_dir, "yt-archive.txt")
        print(f"Downloading {len(yt_videos)} YouTube recording(s)...")
        for v in sorted(yt_videos, key=lambda x: x["published"], reverse=True):
            vid = v["video_id"]
            print(f"  [{v['published']}] {v['channel_name']} — {v['title']}")

            if is_in_yt_archive(archive_path, vid):
                print(f"  skip (archive) {vid}")
                skipped += 1
                continue

            status = download_yt_video(
                vid, v["title"], v["channel_name"],
                v["published"], args.output_dir, archive_path,
            )
            if status == "downloaded":
                downloaded += 1
                log_lines.append(
                    f"{datetime.datetime.now().isoformat()}  OK       yt:{vid}  {v['title']}"
                )
            else:
                failed += 1
                log_lines.append(
                    f"{datetime.datetime.now().isoformat()}  FAILED   yt:{vid}  {v['title']}"
                )
        print()

    if log_lines:
        with open(log_path, "a") as f:
            f.write("\n".join(log_lines) + "\n")

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
#    python3 scripts/download-oxford-agendas.py --dry-run
#
# 2. Narrow to one board:
#    python3 scripts/download-oxford-agendas.py --board "Board of Selectmen"
#
# 3. PDFs only (no video downloads):
#    python3 scripts/download-oxford-agendas.py --docs-only
#
# 4. Videos only:
#    python3 scripts/download-oxford-agendas.py --videos-only
#
# 5. Agendas only (skip minutes):
#    python3 scripts/download-oxford-agendas.py --no-minutes
#
# 6. Change the lookback window:
#    python3 scripts/download-oxford-agendas.py --days 14
#
# 7. Run on a schedule (cron — 7 AM daily):
#    0 7 * * * cd /path/to/repo && python3 scripts/download-oxford-agendas.py
#
# NOTES:
#   - Oxford uses CivicPlus CivicEngage AgendaCenter (same platform as
#     East Windsor, Ellington, etc.). Category IDs are discovered at runtime
#     from the hub page and are stable unless the town reconfigures.
#   - Two boards post YouTube recordings. The P&Z channel is consistently
#     titled; the BOS channel occasionally uploads test videos ("My Broadcast",
#     "Test ...") which appear in results if published within the date window.
#   - Both RSS feeds return a maximum of 15 videos. Running this script daily
#     or weekly ensures recordings are not missed.
#   - yt-dlp writes a download archive (yt-archive.txt) so videos are not
#     re-downloaded on subsequent runs.
