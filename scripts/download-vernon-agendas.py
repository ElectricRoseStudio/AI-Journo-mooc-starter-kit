#!/usr/bin/env python3
# download-vernon-agendas.py
# Download municipal meeting agendas and minutes from Vernon CT for meetings
# whose date falls within the past N days (and up to 7 days ahead, to catch
# agendas posted early for upcoming meetings), plus YouTube meeting recordings.
#
# USAGE:
#   python3 scripts/download-vernon-agendas.py [options]
#
# REQUIREMENTS:
#   - Python 3.6+  (no third-party packages needed)
#   - yt-dlp installed (for video downloads only): pip install yt-dlp
#   - Internet connection
#
# WHAT IT DOES:
#   1. Traverses the Thrillshare CMS document tree for Vernon CT
#      (three content areas: Town Council, Boards & Commissions, Budget Hearings)
#   2. Finds year-appropriate folders within each area and fetches document listings
#   3. Parses meeting dates from document filenames (format: M-D-YYYY)
#   4. Downloads matching PDFs to beat-archive/vernon-agendas/YYYY-MM/
#   5. Fetches Vernon CT's own YouTube channel RSS for meeting recordings
#   6. Downloads matching videos with yt-dlp
#   7. Appends a log to beat-archive/vernon-agendas/download-log.txt
#
# SITE STRUCTURE:
#   Documents (Thrillshare CMS, org 26255, section 443064):
#     API: https://thrillshare-cmsv2.services.thrillshare.com/api/v2/s/443064/documents
#          ?folder_id={id}&page_no={n}
#     Response: {"documents":[{file_name, url, ...}], "items":[folders+docs], "meta":{...}}
#     Folder hierarchy:
#       Town Council Agendas and Minutes (22110334)
#         └── "{YYYY} Town Council Agendas and Minutes"  → PDF docs
#       Boards & Commissions (22110439)
#         └── [Board Name]
#               └── "{YYYY}"  → PDF docs
#       Budget Hearings and Meetings (22109631)
#         └── "{YYYY}-{YYYY+1} Budget"
#               └── Budget Meeting Agendas and Minutes  → PDF docs
#     Date in filename: "M-D-YYYY Board Description [Type]" (month/day not zero-padded)
#
#   Video recordings (Town of Vernon CT YouTube, channel UC7QdlGkfv2VAnPuD8i4dR0w):
#     RSS: https://www.youtube.com/feeds/videos.xml?channel_id=UC7QdlGkfv2VAnPuD8i4dR0w
#     Only Town Council meetings are typically recorded. Titles filtered for
#     "meeting" to exclude non-government content.
#     The RSS feed returns the latest ~15 videos; run at least weekly to stay current.
#
# NOTE: The vernon-ct.gov website uses a JavaScript challenge (Thrillshare CMS)
#   that blocks curl and some HTTP clients. Python's urllib bypasses it cleanly.
#   PDF downloads use the Thrillshare CDN (files-backend.assets.thrillshare.com)
#   which is directly accessible.

import argparse
import datetime
import json
import os
import re
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET

# --- Configuration ---
THRILLSHARE_API = (
    "https://thrillshare-cmsv2.services.thrillshare.com"
    "/api/v2/s/443064/documents"
)

# Root folder IDs for each document content area (discovered from site structure)
TC_ROOT   = 22110334   # Town Council Agendas and Minutes
BOARDS_ROOT = 22110439 # Boards & Commissions
BUDGET_ROOT = 22109631 # Budget Hearings and Meetings

YT_CHANNEL_ID = "UC7QdlGkfv2VAnPuD8i4dR0w"  # Town of Vernon CT
YT_RSS_URL = f"https://www.youtube.com/feeds/videos.xml?channel_id={YT_CHANNEL_ID}"
YT_TITLE_FILTER = "meeting"  # filter to government meetings only

OUTPUT_DIR = "beat-archive/vernon-agendas"
DAYS_BACK = 4
DAYS_AHEAD = 7
API_DELAY = 0.15
DOWNLOAD_DELAY = 0.8

UA = "Mozilla/5.0 (X11; Linux x86_64; rv:120.0) Gecko/20100101 Firefox/120.0"

# Matches M-D-YYYY (with or without zero-padding) anywhere in a string
_DATE_RE = re.compile(r"\b(\d{1,2})-(\d{1,2})-(\d{4})\b")


# --- HTTP helpers ---

def fetch_json(url):
    req = urllib.request.Request(
        url,
        headers={"User-Agent": UA, "Accept": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return json.loads(r.read())
    except urllib.error.HTTPError as e:
        print(f"  HTTP {e.code} — {url}", file=sys.stderr)
        return None
    except Exception as e:
        print(f"  ERROR {url}: {e}", file=sys.stderr)
        return None


def fetch_html(url):
    req = urllib.request.Request(
        url,
        headers={"User-Agent": UA, "Accept": "text/html,*/*"},
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            raw = r.read()
            charset = r.headers.get_content_charset() or "utf-8"
            return raw.decode(charset, errors="replace")
    except Exception as e:
        print(f"  ERROR fetching {url}: {e}", file=sys.stderr)
        return None


def download_file(url, dest_path):
    """Download url to dest_path. Returns True on success."""
    req = urllib.request.Request(
        url,
        headers={"User-Agent": UA, "Accept": "application/pdf,*/*"},
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


# --- Date helpers ---

def parse_date_from_filename(name):
    """
    Extract meeting date from a Thrillshare document filename.
    Most boards use 'M-D-YYYY ...' (date at start, no zero-padding).
    Returns datetime.date or None.
    """
    m = _DATE_RE.search(name)
    if not m:
        return None
    month, day, year = int(m.group(1)), int(m.group(2)), int(m.group(3))
    try:
        return datetime.date(year, month, day)
    except ValueError:
        return None


def extract_year_from_folder_name(name):
    """
    Return the first 4-digit year found at the start of a folder name, or None.
    Handles: '2026', '2026 Town Council Agendas and Minutes',
             '2026-2027 Budget' → returns 2026
    """
    m = re.match(r"^(\d{4})", name.strip())
    return int(m.group(1)) if m else None


def folder_is_budget_meeting(name):
    """True if this subfolder name indicates budget meeting agendas."""
    return "budget meeting" in name.lower() and (
        "agenda" in name.lower() or "minute" in name.lower()
    )


# --- Thrillshare document fetching ---

def fetch_folder_page(folder_id, page_no=1):
    """Fetch one page of a folder's contents. Returns (folders_list, docs_list, has_more)."""
    url = f"{THRILLSHARE_API}?folder_id={folder_id}&page_no={page_no}"
    data = fetch_json(url)
    if not data:
        return [], [], False

    items = data.get("items", [])
    docs = data.get("documents", [])
    meta = data.get("meta", {})
    links = meta.get("links", {})

    # Separate folder items from doc items
    folders = [i for i in items if "folder_name" in i]

    # Pagination: has_more if "next" link differs from "last"
    has_more = bool(links.get("next") and links.get("next") != links.get("last"))
    # Also check: if total_entries > page_no * 20
    total = meta.get("total_entries") or 0
    if total > page_no * 20:
        has_more = True

    return folders, docs, has_more


def fetch_all_docs_in_folder(folder_id):
    """Fetch all paginated documents in a leaf folder."""
    all_docs = []
    page = 1
    while True:
        folders, docs, has_more = fetch_folder_page(folder_id, page)
        all_docs.extend(docs)
        if not has_more or not docs:
            break
        page += 1
        time.sleep(API_DELAY)
    return all_docs


def collect_docs_from_tc(target_years):
    """
    Collect docs from Town Council Agendas and Minutes folder.
    Structure: TC_ROOT → year subfolders → docs
    """
    results = []
    folders, _, _ = fetch_folder_page(TC_ROOT)
    for f in folders:
        year = extract_year_from_folder_name(f["folder_name"])
        if year and year in target_years:
            docs = fetch_all_docs_in_folder(f["id"])
            for d in docs:
                results.append({**d, "_board": "Town Council"})
            time.sleep(API_DELAY)
    return results


def collect_docs_from_boards(target_years, board_filter=None):
    """
    Collect docs from all boards in Boards & Commissions.
    Structure: BOARDS_ROOT → board subfolders → year subfolders → docs
    """
    results = []
    board_folders, _, _ = fetch_folder_page(BOARDS_ROOT)
    time.sleep(API_DELAY)

    total_boards = len(board_folders)
    for idx, bf in enumerate(board_folders, 1):
        board_name = bf["folder_name"]
        if board_filter and board_filter.lower() not in board_name.lower():
            continue

        # Skip utility folders that don't contain meeting docs
        skip_names = {"documentation", "resume", "notice", "links", "pollinate"}
        if any(s in board_name.lower() for s in skip_names):
            continue

        print(f"  [{idx:2d}/{total_boards}] {board_name[:55]}...", end="", flush=True)

        # Fetch year subfolders for this board
        year_folders, _, _ = fetch_folder_page(bf["id"])
        time.sleep(API_DELAY)

        board_count = 0
        for yf in year_folders:
            year = extract_year_from_folder_name(yf["folder_name"])
            if year and year in target_years:
                docs = fetch_all_docs_in_folder(yf["id"])
                for d in docs:
                    results.append({**d, "_board": board_name})
                board_count += len(docs)
                time.sleep(API_DELAY)

        print(f" {board_count} doc(s)")

    return results


def collect_docs_from_budget(target_years, board_filter=None):
    """
    Collect budget meeting agendas and minutes.
    Structure: BUDGET_ROOT → "{YYYY}-{YYYY+1} Budget" folders
                           → "Budget Meeting Agendas and Minutes" subfolder
                           → docs
    """
    results = []
    if board_filter and "budget" not in board_filter.lower():
        return results

    budget_folders, _, _ = fetch_folder_page(BUDGET_ROOT)
    time.sleep(API_DELAY)

    for bf in budget_folders:
        year = extract_year_from_folder_name(bf["folder_name"])
        if not year:
            continue
        # Budget folders span two years (e.g. 2026-2027 covers 2026)
        # Include if the folder year or folder year+1 is in target_years
        budget_years = {year, year + 1}
        if not budget_years.intersection(target_years):
            continue

        # Look for "Budget Meeting Agendas and Minutes" subfolder
        sub_folders, budget_docs, _ = fetch_folder_page(bf["id"])
        time.sleep(API_DELAY)

        # Direct docs in the budget year folder
        for d in budget_docs:
            results.append({**d, "_board": "Budget Hearings"})

        for sf in sub_folders:
            if folder_is_budget_meeting(sf["folder_name"]):
                docs = fetch_all_docs_in_folder(sf["id"])
                for d in docs:
                    results.append({**d, "_board": "Budget Hearings"})
                time.sleep(API_DELAY)

    return results


def fetch_all_documents(target_years, board_filter=None,
                        skip_agendas=False, skip_minutes=False):
    """Collect all documents from all content areas, filtered by target_years."""
    all_raw = []
    all_raw += collect_docs_from_tc(target_years)
    all_raw += collect_docs_from_boards(target_years, board_filter)
    all_raw += collect_docs_from_budget(target_years, board_filter)
    return all_raw


# --- Document filtering and naming ---

def classify_doc(file_name):
    """Return 'agenda', 'minutes', or 'other' based on filename keywords."""
    lower = file_name.lower()
    if "minute" in lower or "action" in lower:
        return "minutes"
    if "agenda" in lower or "cancel" in lower or "packet" in lower:
        return "agenda"
    return "other"


def build_doc_record(raw_doc, cutoff, future_limit, board_filter=None,
                     skip_agendas=False, skip_minutes=False):
    """
    Parse a raw Thrillshare doc dict into a structured record.
    Returns a dict or None if the doc should be excluded.
    """
    file_name = raw_doc.get("file_name", "")
    url = raw_doc.get("url", "")
    board = raw_doc.get("_board", "Unknown")

    if board_filter and board_filter.lower() not in board.lower() \
            and board_filter.lower() not in file_name.lower():
        return None

    meeting_date = parse_date_from_filename(file_name)
    if meeting_date is None:
        return None
    if not (cutoff <= meeting_date <= future_limit):
        return None

    doc_type = classify_doc(file_name)
    if skip_agendas and doc_type == "agenda":
        return None
    if skip_minutes and doc_type == "minutes":
        return None

    return {
        "board": board,
        "file_name": file_name,
        "doc_type": doc_type,
        "meeting_date": meeting_date,
        "url": url,
    }


# --- YouTube helpers ---

def fetch_yt_videos_in_window(cutoff, future_limit):
    """
    Fetch Vernon CT's YouTube channel RSS and filter to meeting videos
    in the date window.
    """
    raw = fetch_html(YT_RSS_URL)
    if not raw:
        print("  WARNING: Could not fetch YouTube RSS feed.", file=sys.stderr)
        return [], 0
    try:
        root = ET.fromstring(raw)
    except ET.ParseError as e:
        print(f"  WARNING: Could not parse YouTube RSS: {e}", file=sys.stderr)
        return [], 0

    ns = {
        "atom": "http://www.w3.org/2005/Atom",
        "yt": "http://www.youtube.com/xml/schemas/2015",
    }
    all_entries = root.findall("atom:entry", ns)
    videos = []
    for entry in all_entries:
        video_id = entry.findtext("yt:videoId", namespaces=ns)
        title_el = entry.find("atom:title", ns)
        title = title_el.text if title_el is not None else ""
        published_el = entry.find("atom:published", ns)
        published_str = published_el.text if published_el is not None else ""
        if not (video_id and title and published_str):
            continue
        if YT_TITLE_FILTER not in title.lower():
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
            })
    return videos, len(all_entries)


def is_in_yt_archive(archive_path, video_id):
    if not os.path.exists(archive_path):
        return False
    needle = f"youtube {video_id}"
    with open(archive_path) as f:
        return any(needle in line for line in f)


def download_yt_video(video_id, title, pub_date, output_dir, archive_path):
    """Download a YouTube video with yt-dlp. Returns 'downloaded' or 'failed'."""
    month_dir = os.path.join(output_dir, pub_date.strftime("%Y-%m"))
    os.makedirs(month_dir, exist_ok=True)
    date_str = pub_date.strftime("%Y-%m-%d")
    title_slug = slugify(title)
    outtmpl = os.path.join(month_dir, f"{date_str}-{title_slug}-{video_id}.%(ext)s")
    url = f"https://www.youtube.com/watch?v={video_id}"

    cmd = [
        "yt-dlp",
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


def make_dest_path(board, doc_type, meeting_date, file_name, output_dir):
    date_str = meeting_date.strftime("%Y-%m-%d")
    month_dir = os.path.join(output_dir, meeting_date.strftime("%Y-%m"))
    os.makedirs(month_dir, exist_ok=True)
    board_slug = slugify(board, max_len=40)
    name_slug = slugify(file_name, max_len=50)
    fname = f"{date_str}-{board_slug}-{name_slug}.pdf"
    return os.path.join(month_dir, fname)


# --- Main ---

def main():
    parser = argparse.ArgumentParser(
        description=(
            "Download Vernon CT municipal agendas, minutes, and meeting recordings "
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

    today = datetime.date.today()
    cutoff = today - datetime.timedelta(days=args.days)
    future_limit = today + datetime.timedelta(days=args.ahead)
    target_years = set(range(cutoff.year, future_limit.year + 1))

    print(f"Date window : {cutoff} to {future_limit}")
    print(f"CMS         : Thrillshare, org 26255, section 443064")
    if not args.dry_run:
        print(f"Output dir  : {args.output_dir}")
    print()

    all_docs = []
    yt_videos = []
    total_yt_entries = 0

    # --- Step 1: PDFs from Thrillshare ---
    if not args.videos_only:
        print("Scanning Thrillshare document tree (Town Council, Boards & Commissions, Budget)...")
        raw_docs = fetch_all_documents(
            target_years,
            board_filter=args.board if args.board else None,
        )

        for raw in raw_docs:
            rec = build_doc_record(
                raw, cutoff, future_limit,
                board_filter=args.board,
                skip_agendas=args.no_agendas,
                skip_minutes=args.no_minutes,
            )
            if rec:
                all_docs.append(rec)

        all_docs.sort(key=lambda x: (x["meeting_date"], x["board"]), reverse=True)
        print(f"  Found {len(all_docs)} document(s) in date window.")
        print()

    # --- Step 2: Videos from YouTube ---
    if not args.docs_only:
        print("Fetching Vernon CT YouTube channel RSS for meeting recordings...")
        yt_videos, total_yt_entries = fetch_yt_videos_in_window(cutoff, future_limit)
        if args.board:
            filter_str = args.board.lower()
            yt_videos = [v for v in yt_videos if filter_str in v["title"].lower()]
        print(f"  Found {len(yt_videos)} recording(s) in window "
              f"({total_yt_entries} total entries in RSS feed).")
        if total_yt_entries >= 15:
            print(
                "  NOTE: RSS feed is at the 15-video cap — older recordings may be missed.\n"
                "        Run this script at least weekly to stay current."
            )
        print()

    if not all_docs and not yt_videos:
        print("No documents or recordings found in the date window.")
        return

    # --- Dry-run listing ---
    if args.dry_run:
        if all_docs:
            print(f"{'Board':<40} {'Date':<12} {'Type':<8} Filename")
            print("-" * 90)
            for d in all_docs:
                print(
                    f"{d['board'][:39]:<40} "
                    f"{d['meeting_date']!s:<12} "
                    f"{d['doc_type']:<8} "
                    f"{d['file_name']}"
                )
            print()
        if yt_videos:
            print(f"{'Published':<12} Video ID      Title")
            print("-" * 72)
            for v in yt_videos:
                print(f"{v['published']!s:<12} {v['video_id']:<14} {v['title']}")
            print()
        total = len(all_docs) + len(yt_videos)
        print(f"{total} item(s) matched. Re-run without --dry-run to download.")
        return

    # --- Step 3: Download PDFs ---
    os.makedirs(args.output_dir, exist_ok=True)
    log_path = os.path.join(args.output_dir, "download-log.txt")
    log_lines = []
    downloaded = skipped = failed = 0

    if all_docs:
        for d in all_docs:
            dest = make_dest_path(
                d["board"], d["doc_type"], d["meeting_date"],
                d["file_name"], args.output_dir,
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
                    f"{datetime.datetime.now().isoformat()}  FAILED   {d['url'][:80]}"
                )
                if os.path.exists(dest):
                    os.remove(dest)

            time.sleep(DOWNLOAD_DELAY)

        print()

    # --- Step 4: Download YouTube videos ---
    if yt_videos:
        archive_path = os.path.join(args.output_dir, "yt-archive.txt")
        print(f"Downloading {len(yt_videos)} YouTube recording(s)...")

        for v in yt_videos:
            vid = v["video_id"]
            print(f"  [{v['published']}] {v['title']}")

            if is_in_yt_archive(archive_path, vid):
                print(f"  skip (archive) {vid}")
                skipped += 1
                continue

            print(f"  downloading    {vid}")
            status = download_yt_video(
                vid, v["title"], v["published"], args.output_dir, archive_path
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
#    python3 scripts/download-vernon-agendas.py --dry-run
#
# 2. Narrow to one board:
#    python3 scripts/download-vernon-agendas.py --board "Planning and Zoning"
#
# 3. PDFs only (no video downloads):
#    python3 scripts/download-vernon-agendas.py --docs-only
#
# 4. Videos only:
#    python3 scripts/download-vernon-agendas.py --videos-only
#
# 5. Agendas only (skip minutes):
#    python3 scripts/download-vernon-agendas.py --no-minutes
#
# 6. Change the lookback window:
#    python3 scripts/download-vernon-agendas.py --days 7
#
# 7. Save files somewhere else:
#    python3 scripts/download-vernon-agendas.py --output-dir ~/Downloads/vernon
#
# 8. Run on a schedule (cron — 7 AM daily):
#    0 7 * * * cd /path/to/repo && python3 scripts/download-vernon-agendas.py
#
# NOTES:
#   - Vernon CT uses Thrillshare CMS (thrillshare.com), org 26255. Documents are
#     organized in a folder hierarchy: content area → board → year → docs.
#     The script traverses three content areas: Town Council Agendas and Minutes,
#     Boards & Commissions (45 boards), and Budget Hearings and Meetings.
#   - Meeting dates are embedded in filenames as M-D-YYYY (no zero-padding),
#     e.g., "5-5-2026 Town Council Agenda".
#   - Videos are recorded by the Town of Vernon itself (channel UC7QdlGkfv2VAnPuD8i4dR0w)
#     and currently cover Town Council meetings only. Titles are filtered for
#     "meeting" to exclude school and community event videos.
#   - yt-dlp writes a download archive (yt-archive.txt) so videos are not
#     re-downloaded on subsequent runs.
#   - The --ahead flag (default: 7 days) captures agendas for upcoming meetings
#     that have already been posted.
#   - Boards & Commissions includes: Arts Commission, Board of Ethics,
#     Cemetery Commission, Conservation Commission, Design Review Commission,
#     Economic Development Commission, Energy Improvement District Board,
#     Hockanum River Linear Park Committee, Homestead Revitalization Committee,
#     Human Services Advisory Commission, Hydropower Commission,
#     Inland Wetlands Commission, Local Historic Properties Commission,
#     Municipal Flood and Erosion Control Board, North Central District Health
#     Department Board, Open Space Task Force, Pension Board, Planning and Zoning
#     Commission, Permanent Municipal Building Committee, Risk Management
#     Subcommittee, School Readiness Council, Senior Citizens Advisory Board,
#     Vernon Area Cable Advisory Council, Vernon Housing Authority, Vernon Rocks
#     Coalition, Vernon Traffic Authority, WPCA, Youth Services Bureau Advisory
#     Board, Zoning Board of Appeals, and others.
