#!/usr/bin/env python3
# download-tolland-agendas.py
# Download municipal meeting agendas and minutes from Tolland CT for meetings
# whose date falls within the past N days (and up to 7 days ahead, to catch
# agendas posted early for upcoming meetings), plus YouTube meeting recordings.
#
# USAGE:
#   python3 scripts/download-tolland-agendas.py [options]
#
# REQUIREMENTS:
#   - Python 3.6+  (no third-party packages needed)
#   - yt-dlp installed (for video downloads only): pip install yt-dlp
#   - Internet connection
#
# WHAT IT DOES:
#   1. Fetches all Agendas and Minutes board categories from the General Code
#      eCode360 API (custId TO1208)
#   2. For each category, retrieves the current and/or prior year document list
#   3. Parses meeting dates from document titles (format: YYYY-MM-DD prefix)
#   4. Downloads matching PDFs to beat-archive/tolland-agendas/YYYY-MM/
#   5. Fetches the Community Voice Channel YouTube RSS feed and filters for
#      Tolland-related meeting recordings
#   6. Downloads matching videos with yt-dlp
#   7. Appends a log to beat-archive/tolland-agendas/download-log.txt
#
# SITE STRUCTURE:
#   Documents (General Code eCode360, custId TO1208):
#     Types:        GET https://ecode360.com/api/location/TO1208/pub-doc/types
#                   → typeId 120 = Agendas, typeId 1422 = Minutes
#     Categories:   GET https://ecode360.com/api/location/TO1208/pub-doc/type/{typeId}/categories
#     Docs by year: GET https://ecode360.com/api/location/TO1208/pub-doc/category/{catId}/year/{year}/children
#                   Returns [{type:"document", key:"{docKey}", title:"2026-04-28 Regular Meeting Agenda"}]
#     Download:     GET https://ecode360.com/api/TO1208/pub-doc/{docKey}/download
#
#   Video recordings (Community Voice Channel, YouTube):
#     Channel ID: UC7IKRS0lXdkbT4FMX2Dp0lQ (cvcct.org — covers multiple CT towns)
#     RSS: https://www.youtube.com/feeds/videos.xml?channel_id=UC7IKRS0lXdkbT4FMX2Dp0lQ
#     NOTE: This channel covers Bolton, Vernon, Tolland, and others. Only Town
#     Council meetings are typically recorded. Titles are filtered for "Tolland".
#     The RSS feed returns the latest ~15 videos across all towns combined, so
#     run this script at least weekly to avoid gaps.
#
# AGENDA BOARDS (38 categories):
#   Agriculture Commission, Birch Grove Primary School Building Committee,
#   Board of Education, Charter Revision Commission, Conservation Commission,
#   Design Advisory Board, Economic Development Commission, Ethics Commission,
#   Historic District Commission, Housing Authority, Inland Wetlands Commission,
#   Land Acquisition Advisory Committee, Non-Profit Housing Corporation,
#   Planning and Zoning Commission (PZC), Recreation Advisory Board,
#   Sustainable Connecticut Committee, Technology Advisory Board,
#   Tolland Public Library Advisory Board, Tolland Water Commission,
#   Town Council, Veterans Recognition Commission, WPCA,
#   Zoning Board of Appeals (ZBA), and more.

import argparse
import datetime
import json
import os
import re
import subprocess

YT_DLP_NODE = "node:/home/richkirby/.local/bin/yt-dlp-node"  # yt-dlp needs Node 22+; symlink kept current by scripts/update-yt-dlp-node.sh
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET

# --- Configuration ---
ECODE_BASE = "https://ecode360.com"
CUST_ID = "TO1208"
TYPE_AGENDAS = 120
TYPE_MINUTES = 1422

YT_CHANNEL_ID = "UC7IKRS0lXdkbT4FMX2Dp0lQ"  # Community Voice Channel (cvcct.org)
YT_RSS_URL = f"https://www.youtube.com/feeds/videos.xml?channel_id={YT_CHANNEL_ID}"
YT_TITLE_FILTER = "tolland"       # case-insensitive must-include for multi-town channel
YT_TITLE_EXCLUDE = "tolland county"  # exclude "Tolland County" references (not the town)

OUTPUT_DIR = "beat-archive/tolland-agendas"
DAYS_BACK = 4
DAYS_AHEAD = 7
PAGE_DELAY = 0.5
DOWNLOAD_DELAY = 0.8

UA = "Mozilla/5.0 (X11; Linux x86_64; rv:120.0) Gecko/20100101 Firefox/120.0"

# Matches YYYY-MM-DD at the start of document titles
_DATE_RE = re.compile(r"^(\d{4})-(\d{2})-(\d{2})\b")


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
    """Download url to dest_path, streaming in chunks. Returns True on success."""
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


# --- Date parsing ---

def parse_date_from_title(title):
    """
    Extract a meeting date from a Tolland eCode360 document title.

    Observed formats:
      "2026-05-12 Regular Meeting Agenda"   → 2026-05-12
      "2026-04-28 - Special Meeting Agenda" → 2026-04-28
      "2026-01-12 Regular Meeting - Amended"→ 2026-01-12

    Returns a datetime.date or None if no date found.
    """
    m = _DATE_RE.match(title.strip())
    if not m:
        return None
    try:
        return datetime.date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
    except ValueError:
        return None


# --- General Code eCode360 document API ---

def fetch_categories(type_id):
    """Return all board/committee categories for a given document type."""
    url = f"{ECODE_BASE}/api/location/{CUST_ID}/pub-doc/type/{type_id}/categories"
    return fetch_json(url) or []


def fetch_docs_for_year(cat_key, year):
    """Return all documents in a category for a given year."""
    url = (
        f"{ECODE_BASE}/api/location/{CUST_ID}/pub-doc/category/{cat_key}"
        f"/year/{year}/children"
    )
    return fetch_json(url) or []


def make_download_url(doc_key):
    return f"{ECODE_BASE}/api/{CUST_ID}/pub-doc/{doc_key}/download"


def fetch_documents_in_window(cutoff, future_limit, board_filter=None, skip_minutes=False, skip_agendas=False):
    """
    Fetch all agenda and minutes documents whose title date falls within
    [cutoff, future_limit]. Returns a list of dicts:
      {board, doc_type, meeting_date, doc_key, title, url}
    """
    today = datetime.date.today()
    years = sorted({cutoff.year, today.year, future_limit.year})

    results = []

    for doc_type_name, type_id in [("agenda", TYPE_AGENDAS), ("minutes", TYPE_MINUTES)]:
        if skip_agendas and doc_type_name == "agenda":
            continue
        if skip_minutes and doc_type_name == "minutes":
            continue

        cats = fetch_categories(type_id)
        if not cats:
            print(f"  WARNING: No {doc_type_name} categories returned.", file=sys.stderr)
            continue

        for cat in cats:
            cat_key = cat["key"]
            board = cat["title"]

            if board_filter and board_filter.lower() not in board.lower():
                continue

            for year in years:
                docs = fetch_docs_for_year(cat_key, year)
                time.sleep(PAGE_DELAY)

                for doc in docs:
                    title = doc.get("title", "")
                    doc_key = doc.get("key", "")
                    meeting_date = parse_date_from_title(title)
                    if meeting_date is None:
                        continue
                    if not (cutoff <= meeting_date <= future_limit):
                        continue
                    results.append({
                        "board": board,
                        "doc_type": doc_type_name,
                        "meeting_date": meeting_date,
                        "doc_key": doc_key,
                        "title": title,
                        "url": make_download_url(doc_key),
                    })

    results.sort(key=lambda x: (x["meeting_date"], x["board"]), reverse=True)
    return results


# --- YouTube helpers ---

def fetch_yt_videos_in_window(cutoff, future_limit):
    """
    Fetch recent videos from Community Voice Channel RSS and filter to
    Tolland-related titles within the date window.
    The RSS feed returns the latest ~15 videos across all covered towns.
    """
    raw = fetch_html(YT_RSS_URL)
    if not raw:
        print("  WARNING: Could not fetch YouTube RSS feed.", file=sys.stderr)
        return []
    try:
        root = ET.fromstring(raw)
    except ET.ParseError as e:
        print(f"  WARNING: Could not parse YouTube RSS: {e}", file=sys.stderr)
        return []

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
        title_lower = title.lower()
        if YT_TITLE_FILTER not in title_lower:
            continue
        if YT_TITLE_EXCLUDE in title_lower:
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
    """Download a YouTube video with yt-dlp. Returns 'downloaded', 'skipped', or 'failed'."""
    month_dir = os.path.join(output_dir, pub_date.strftime("%Y-%m"))
    os.makedirs(month_dir, exist_ok=True)
    date_str = pub_date.strftime("%Y-%m-%d")
    title_slug = slugify(title)
    outtmpl = os.path.join(month_dir, f"{date_str}-{title_slug}-{video_id}.%(ext)s")
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


def make_dest_path(board, doc_type, meeting_date, doc_key, output_dir):
    date_str = meeting_date.strftime("%Y-%m-%d")
    month_dir = os.path.join(output_dir, meeting_date.strftime("%Y-%m"))
    os.makedirs(month_dir, exist_ok=True)
    board_slug = slugify(board, max_len=45)
    fname = f"{date_str}-{board_slug}-{doc_key}-{doc_type}.pdf"
    return os.path.join(month_dir, fname)


# --- Main ---

def main():
    parser = argparse.ArgumentParser(
        description=(
            "Download Tolland CT municipal agendas, minutes, and meeting recordings "
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

    print(f"Date window : {cutoff} to {future_limit}")
    print(f"eCode360    : {ECODE_BASE}/api/location/{CUST_ID}/")
    if not args.dry_run:
        print(f"Output dir  : {args.output_dir}")
    print()

    all_docs = []
    yt_videos = []
    total_yt_entries = 0

    # --- Step 1: PDFs from eCode360 ---
    if not args.videos_only:
        print("Fetching eCode360 board categories and documents...")
        all_docs = fetch_documents_in_window(
            cutoff, future_limit,
            board_filter=args.board if args.board else None,
            skip_minutes=args.no_minutes,
            skip_agendas=args.no_agendas,
        )
        print(f"  Found {len(all_docs)} document(s) in date window.")
        print()

    # --- Step 2: Videos from YouTube ---
    if not args.docs_only:
        print("Fetching Community Voice Channel YouTube RSS for Tolland recordings...")
        yt_videos, total_yt_entries = fetch_yt_videos_in_window(cutoff, future_limit)
        if args.board:
            filter_str = args.board.lower()
            yt_videos = [v for v in yt_videos if filter_str in v["title"].lower()]
        print(f"  Found {len(yt_videos)} Tolland recording(s) in window "
              f"({total_yt_entries} total entries in RSS feed).")
        if total_yt_entries >= 15:
            print(
                "  NOTE: RSS feed is at the 15-video cap — older videos from any town are not\n"
                "        visible. Run this script at least weekly to avoid gaps in Tolland coverage."
            )
        print()

    if not all_docs and not yt_videos:
        print("No documents or recordings found in the date window.")
        return

    # --- Dry-run listing ---
    if args.dry_run:
        if all_docs:
            print(f"{'Board':<48} {'Date':<12} {'Type':<8} Title")
            print("-" * 90)
            for d in all_docs:
                print(
                    f"{d['board'][:47]:<48} "
                    f"{d['meeting_date']!s:<12} "
                    f"{d['doc_type']:<8} "
                    f"{d['title']}"
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
                d["doc_key"], args.output_dir,
            )
            label = os.path.basename(dest)

            if os.path.exists(dest):
                print(f"  skip (exists)  {label}")
                skipped += 1
                continue

            print(f"  [{d['meeting_date']}] {d['board'][:50]} — {d['doc_type']}")
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
#    python3 scripts/download-tolland-agendas.py --dry-run
#
# 2. Narrow to one board:
#    python3 scripts/download-tolland-agendas.py --board "Town Council"
#
# 3. PDFs only (no video downloads):
#    python3 scripts/download-tolland-agendas.py --docs-only
#
# 4. Videos only:
#    python3 scripts/download-tolland-agendas.py --videos-only
#
# 5. Agendas only (skip minutes):
#    python3 scripts/download-tolland-agendas.py --no-minutes
#
# 6. Change the lookback window:
#    python3 scripts/download-tolland-agendas.py --days 7
#
# 7. Save files somewhere else:
#    python3 scripts/download-tolland-agendas.py --output-dir ~/Downloads/tolland
#
# 8. Run on a schedule (cron — 7 AM daily):
#    0 7 * * * cd /path/to/repo && python3 scripts/download-tolland-agendas.py
#
# NOTES:
#   - Tolland CT uses General Code eCode360 (custId TO1208). Documents are
#     retrieved per board category per year via the JSON API. Category lists
#     are fetched dynamically on each run so new boards are picked up
#     automatically.
#   - Document titles use ISO date prefix (YYYY-MM-DD) for all meeting records.
#   - Video recordings are produced by the Community Voice Channel (cvcct.org),
#     which covers Bolton, Vernon, Tolland, and other nearby towns. Only Town
#     Council meetings are typically recorded. Titles are filtered by "Tolland".
#   - The YouTube RSS feed returns the latest 15 videos across ALL covered towns,
#     so Tolland-specific content may be sparse in any given window. Run this
#     script at least weekly.
#   - yt-dlp writes a download archive (yt-archive.txt) so videos are not
#     re-downloaded on subsequent runs.
