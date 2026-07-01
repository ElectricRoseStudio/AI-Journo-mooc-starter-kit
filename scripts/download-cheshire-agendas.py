#!/usr/bin/env python3
# download-cheshire-agendas.py
# Download municipal meeting agendas, minutes, and video recordings from
# Cheshire CT for meetings within the past N days (and up to 7 days ahead,
# to catch agendas posted early for upcoming meetings).
#
# USAGE:
#   python3 scripts/download-cheshire-agendas.py [options]
#
# REQUIREMENTS:
#   - Python 3.6+  (no third-party packages needed for docs)
#   - yt-dlp       (required for --include-video; pip install yt-dlp)
#   - Internet connection
#
# WHAT IT DOES:
#   1. Queries the CivicClerk REST API for events in the date window
#   2. For each event that has published files (Agenda, Minutes, etc.),
#      downloads the selected document types
#   3. Optionally fetches the Cheshire Channel 14 YouTube channel video list
#      via yt-dlp, parses meeting dates from video titles, and downloads
#      videos whose dates fall within the window (--include-video flag)
#   4. Saves files to beat-archive/cheshire-agendas/YYYY-MM/
#   5. Appends a download log to beat-archive/cheshire-agendas/download-log.txt
#
# SITE STRUCTURE:
#   Documents — CivicClerk platform (cheshirect.portal.civicclerk.com):
#     The public OData REST API requires no authentication.
#     Key endpoints (all under https://cheshirect.api.civicclerk.com/v1/):
#       GET /Events?$filter=startDateTime+ge+DATE&$orderby=startDateTime+desc
#           Returns events with publishedFiles arrays.
#           File types seen: "Agenda", "Minutes", "Agenda Packet", "Notice"
#       GET /Meetings/GetMeetingFileStream(fileId=N,plainText=false)
#           Streams the PDF for a given fileId.
#     Note: Cheshire's CivicClerk instance has no video/media attached to
#     any events — the media fields (hasMedia, youtubeVideoId, etc.) are
#     all empty across the full event history.
#
#   Videos — Cheshire Channel 14 (YouTube @CheshireChannel14):
#     ~80 videos going back to 2018. Only select boards are recorded:
#     Town Council, Planning and Zoning, Next Generation School Building
#     Committee, WPCA, BOE, and occasional others.
#     YouTube upload dates are not exposed in the yt-dlp flat-playlist
#     response; meeting dates are parsed from video titles instead.
#
#     Title date formats observed:
#       M-D-YY          e.g. "Town Council 3-23-26"
#       M/D/YY          e.g. "WPCA 3/26/21"
#       M/D/YYYY        e.g. "Town Council 5/13/20"
#       M-D-YYYY        e.g. "Town Council 3-20-18"
#       Month D, YYYY   e.g. "Planning and Zoning February 24, 2025"

import argparse
import datetime
import glob
import json
import os
import re
import subprocess
import sys
import time

YT_DLP_NODE = "node:/home/richkirby/.nvm/versions/node/v20.20.2/bin/node"  # yt-dlp needs Node 20+; system node is 18
import urllib.error
import urllib.parse
import urllib.request

# --- Configuration ---
API_BASE = "https://cheshirect.api.civicclerk.com/v1"
PORTAL_URL = "https://cheshirect.portal.civicclerk.com"
YOUTUBE_CHANNEL = "https://www.youtube.com/@CheshireChannel14/videos"
OUTPUT_DIR = "beat-archive/cheshire-agendas"
DAYS_BACK = 4
DAYS_AHEAD = 7   # capture agendas posted early for upcoming meetings
DELAY_SECONDS = 1

# Document types to download by default (case-insensitive prefix match)
DEFAULT_TYPES = {"agenda", "minutes"}

UA = "Cheshire-Agendas-Downloader/1.0 (journalism research)"

_MONTH_NAMES = {
    'january': 1, 'february': 2, 'march': 3, 'april': 4,
    'may': 5, 'june': 6, 'july': 7, 'august': 8,
    'september': 9, 'october': 10, 'november': 11, 'december': 12,
}


# --- HTTP helpers ---

def fetch_json(url):
    """GET url and return parsed JSON, or None on error."""
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return json.loads(r.read().decode("utf-8"))
    except urllib.error.URLError as e:
        print(f"  ERROR fetching {url}: {e}", file=sys.stderr)
        return None


def download_file(file_id, dest_path):
    """Download a CivicClerk file by fileId to dest_path. Returns True on success."""
    url = f"{API_BASE}/Meetings/GetMeetingFileStream(fileId={file_id},plainText=false)"
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            with open(dest_path, "wb") as f:
                f.write(r.read())
        return True
    except Exception as e:
        print(f"  WARNING: {e}", file=sys.stderr)
        return False


def download_video(youtube_url, dest_template):
    """
    Download a YouTube video using yt-dlp.
    dest_template must end in .%(ext)s — yt-dlp fills in the extension.
    Returns True on success.
    """
    cmd = [
        "yt-dlp", "--js-runtimes", YT_DLP_NODE,
        "--no-playlist",
        "-f", "bestvideo[ext=mp4]+bestaudio[ext=m4a]/best[ext=mp4]/best",
        "--merge-output-format", "mp4",
        "-o", dest_template,
        "--no-overwrites",
        "--quiet",
        "--no-warnings",
        youtube_url,
    ]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=3600)
        if result.returncode != 0 and result.stderr:
            print(f"  WARNING: yt-dlp: {result.stderr.strip()}", file=sys.stderr)
        return result.returncode == 0
    except FileNotFoundError:
        print(
            "  ERROR: yt-dlp not found. Install it with: pip install yt-dlp",
            file=sys.stderr,
        )
        return False
    except subprocess.TimeoutExpired:
        print(f"  WARNING: yt-dlp timed out for {youtube_url}", file=sys.stderr)
        return False
    except Exception as e:
        print(f"  WARNING: yt-dlp error: {e}", file=sys.stderr)
        return False


def video_already_exists(dest_template):
    """Return True if any file matching dest_template (with %(ext)s) already exists."""
    base = dest_template.replace(".%(ext)s", "")
    return bool(glob.glob(base + ".*"))


# --- Video title date parsing ---

def parse_title_date(title):
    """
    Extract the meeting date from a Channel 14 YouTube video title.

    Handles formats seen in the channel:
      M-D-YY / M-D-YYYY    e.g. "Town Council 3-23-26", "Town Council 3-20-18"
      M/D/YY / M/D/YYYY    e.g. "WPCA 3/26/21", "Town Council 5/13/20"
      Month D, YYYY        e.g. "Planning and Zoning February 24, 2025"

    Two-digit years are always interpreted as 2000+YY (all videos post-2000).
    Returns a datetime.date or None.
    """
    # Numeric: M[-/]D[-/]YY or M[-/]D[-/]YYYY
    m = re.search(r'\b(\d{1,2})[/-](\d{1,2})[/-](\d{2,4})\b', title)
    if m:
        month, day, year = int(m.group(1)), int(m.group(2)), int(m.group(3))
        if year < 100:
            year += 2000
        try:
            return datetime.date(year, month, day)
        except ValueError:
            pass

    # Named month: "Month D, YYYY" or "Month D YYYY"
    m = re.search(
        r'\b(January|February|March|April|May|June|July|August|'
        r'September|October|November|December)\s+(\d{1,2}),?\s+(\d{4})\b',
        title, re.IGNORECASE,
    )
    if m:
        month = _MONTH_NAMES[m.group(1).lower()]
        day, year = int(m.group(2)), int(m.group(3))
        try:
            return datetime.date(year, month, day)
        except ValueError:
            pass

    return None


# --- Channel video enumeration ---

def fetch_channel_videos():
    """
    Use yt-dlp --flat-playlist to list all videos on @CheshireChannel14.
    Returns a list of {video_id, title, youtube_url}.
    """
    cmd = [
        "yt-dlp", "--js-runtimes", YT_DLP_NODE,
        "--flat-playlist",
        "--no-warnings",
        "--print", "%(id)s\t%(title)s",
        YOUTUBE_CHANNEL,
    ]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
        if result.returncode != 0:
            print(
                f"  WARNING: yt-dlp channel list failed: {result.stderr.strip()}",
                file=sys.stderr,
            )
            return []
        videos = []
        for line in result.stdout.splitlines():
            parts = line.split("\t", 1)
            if len(parts) == 2:
                vid_id, title = parts
                videos.append({
                    "video_id": vid_id.strip(),
                    "title": title.strip(),
                    "youtube_url": f"https://www.youtube.com/watch?v={vid_id.strip()}",
                })
        return videos
    except FileNotFoundError:
        print(
            "  ERROR: yt-dlp not found. Install it with: pip install yt-dlp",
            file=sys.stderr,
        )
        return []
    except subprocess.TimeoutExpired:
        print("  WARNING: yt-dlp timed out fetching channel list", file=sys.stderr)
        return []


# --- Utilities ---

def slugify(text):
    text = text.lower().strip()
    text = re.sub(r"[/\\]", "-", text)
    text = re.sub(r"\s+-\s+", "-", text)
    text = re.sub(r"[^\w\s-]", "", text)
    text = re.sub(r"[\s_]+", "-", text)
    text = re.sub(r"-{2,}", "-", text)
    return text.strip("-")[:60]


def make_dest_path(board_name, doc_type, meeting_date, output_dir, suffix=""):
    date_prefix = meeting_date.strftime("%Y-%m-%d")
    month_dir = meeting_date.strftime("%Y-%m")
    board_slug = slugify(board_name)
    type_slug = slugify(doc_type)
    month_path = os.path.join(output_dir, month_dir)
    os.makedirs(month_path, exist_ok=True)
    fname = f"{date_prefix}-{board_slug}-{type_slug}{suffix}.pdf"
    return os.path.join(month_path, fname)


def make_video_dest_path(title, meeting_date, output_dir):
    """Return a yt-dlp output template (ends in .%(ext)s) for a video download."""
    date_prefix = meeting_date.strftime("%Y-%m-%d")
    month_dir = meeting_date.strftime("%Y-%m")
    title_slug = slugify(title)
    month_path = os.path.join(output_dir, month_dir)
    os.makedirs(month_path, exist_ok=True)
    fname = f"{date_prefix}-{title_slug}.%(ext)s"
    return os.path.join(month_path, fname)


def parse_date(iso_str):
    """Return a date object from an ISO 8601 string, or None."""
    try:
        return datetime.date.fromisoformat(iso_str[:10])
    except (ValueError, TypeError):
        return None


# --- Main ---

def main():
    parser = argparse.ArgumentParser(
        description=(
            "Download Cheshire CT municipal agendas, minutes, and video recordings "
            "for meetings within the past N days."
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
        help="Only process boards whose name contains NAME (case-insensitive)",
    )
    parser.add_argument(
        "--include-packets", action="store_true",
        help="Also download Agenda Packet files (can be large)",
    )
    parser.add_argument(
        "--include-video", action="store_true",
        help="Also download YouTube video recordings via yt-dlp (can be large)",
    )
    parser.add_argument(
        "--docs-only", action="store_true",
        help="Download only PDFs; skip video even if --include-video is set",
    )
    args = parser.parse_args()

    now = datetime.datetime.now()
    if (now.weekday() == 5 and now.hour >= 18) or (now.weekday() == 6 and now.hour < 12):  # Saturday night, Sunday morning
        print("Skipping — no downloads on Saturday nights or Sunday mornings.")
        sys.exit(0)

    today = datetime.date.today()
    cutoff = today - datetime.timedelta(days=args.days)
    future_limit = today + datetime.timedelta(days=args.ahead)

    doc_types = set(DEFAULT_TYPES)
    if args.include_packets:
        doc_types.add("agenda packet")

    include_video = args.include_video and not args.docs_only

    print(f"Date window : {cutoff} to {future_limit}")
    print(f"Portal      : {PORTAL_URL}")
    if not args.dry_run:
        print(f"Output dir  : {args.output_dir}")
    if include_video:
        print(f"Video       : {YOUTUBE_CHANNEL}")
    print()

    # ------------------------------------------------------------------ #
    # Part 1: PDFs from CivicClerk API                                    #
    # ------------------------------------------------------------------ #

    print("Fetching events from CivicClerk API...")

    filter_str = (
        f"startDateTime ge {cutoff}T00:00:00Z "
        f"and startDateTime lt {future_limit}T23:59:59Z"
    )
    url = (
        f"{API_BASE}/Events"
        f"?$filter={urllib.parse.quote(filter_str)}"
        f"&$orderby=startDateTime+desc,+eventName+desc"
    )

    # Paginate through all results (API returns ~15 per page)
    all_events = []
    next_url = url
    while next_url:
        data = fetch_json(next_url)
        if data is None:
            print("ERROR: Could not fetch events from API.", file=sys.stderr)
            sys.exit(1)
        all_events.extend(data.get("value", []))
        next_url = data.get("@odata.nextLink")

    print(f"Found {len(all_events)} event(s) in window.\n")

    if args.board:
        filter_name = args.board.lower()
        all_events = [
            e for e in all_events
            if filter_name in e.get("categoryName", "").lower()
            or filter_name in e.get("eventName", "").lower()
        ]
        print(f"Filtered to {len(all_events)} event(s) matching '{args.board}'.\n")

    # Collect downloadable documents
    matches = []

    for event in all_events:
        meeting_date = parse_date(event.get("eventDate", ""))
        if not meeting_date:
            continue

        board = event.get("categoryName") or event.get("eventName", "Unknown")
        published_files = event.get("publishedFiles") or []

        for pf in published_files:
            doc_type = (pf.get("type") or "").strip()
            if doc_type.lower() not in doc_types:
                continue
            file_id = pf.get("fileId")
            if not file_id:
                continue
            publish_on = parse_date(pf.get("publishOn", ""))

            matches.append({
                "meeting_date": meeting_date,
                "board": board,
                "doc_type": doc_type,
                "file_id": file_id,
                "publish_on": publish_on,
                "event_id": event.get("id"),
            })

    matches.sort(key=lambda x: (x["meeting_date"], x["board"]), reverse=True)

    # Detect duplicate (board, doc_type, meeting_date) combos and assign suffixes
    seen_keys: dict = {}
    for m in matches:
        key = (m["board"], m["doc_type"], m["meeting_date"])
        seen_keys[key] = seen_keys.get(key, 0) + 1
    key_counter: dict = {}
    for m in matches:
        key = (m["board"], m["doc_type"], m["meeting_date"])
        if seen_keys[key] > 1:
            key_counter[key] = key_counter.get(key, 0) + 1
            m["suffix"] = f"-{key_counter[key]}"
        else:
            m["suffix"] = ""

    print(
        f"Found {len(matches)} document(s) (Agenda/Minutes) across "
        f"{len({m['event_id'] for m in matches})} event(s)."
    )

    # ------------------------------------------------------------------ #
    # Part 2: Videos from Channel 14 YouTube                              #
    # ------------------------------------------------------------------ #

    video_matches = []
    if include_video or (args.dry_run and args.include_video):
        print("\nFetching Channel 14 video list from YouTube...")
        all_videos = fetch_channel_videos()
        print(f"  Found {len(all_videos)} total video(s) on channel.")

        for v in all_videos:
            vdate = parse_title_date(v["title"])
            if vdate is None:
                continue
            if vdate < cutoff or vdate > future_limit:
                continue
            if args.board and args.board.lower() not in v["title"].lower():
                continue
            video_matches.append({
                "title": v["title"],
                "date": vdate,
                "video_id": v["video_id"],
                "youtube_url": v["youtube_url"],
            })

        print(f"  {len(video_matches)} video(s) fall within the date window.")

    print()

    if not matches and not video_matches:
        return

    if args.dry_run:
        if matches:
            print(f"{'Board':<42} {'Date':<12} {'Published':<12} Type")
            print("-" * 80)
            for m in matches:
                pub = str(m["publish_on"]) if m["publish_on"] else "unknown"
                print(
                    f"{m['board'][:41]:<42} {m['meeting_date']!s:<12} "
                    f"{pub:<12} {m['doc_type']}"
                )
        if video_matches:
            print(f"\n{'Title':<55} {'Date':<12}")
            print("-" * 70)
            for v in sorted(video_matches, key=lambda x: x["date"], reverse=True):
                print(f"{v['title'][:54]:<55} {v['date']!s}")
        total = len(matches) + len(video_matches)
        print(f"\n{total} item(s). Re-run without --dry-run to download.")
        return

    # ------------------------------------------------------------------ #
    # Download PDFs                                                        #
    # ------------------------------------------------------------------ #

    os.makedirs(args.output_dir, exist_ok=True)
    log_path = os.path.join(args.output_dir, "download-log.txt")
    log_lines = []
    downloaded = skipped = failed = 0

    for m in matches:
        dest = make_dest_path(
            m["board"], m["doc_type"], m["meeting_date"], args.output_dir,
            suffix=m.get("suffix", ""),
        )
        label = os.path.basename(dest)

        if os.path.exists(dest):
            print(f"  skip (exists)  {label}")
            skipped += 1
            continue

        print(f"  [{m['meeting_date']}] {m['board']} — {m['doc_type']}")
        print(f"  downloading    {label}")

        if download_file(m["file_id"], dest):
            downloaded += 1
            log_lines.append(
                f"{datetime.datetime.now().isoformat()}  OK       {dest}"
            )
        else:
            failed += 1
            file_url = f"{API_BASE}/Meetings/GetMeetingFileStream(fileId={m['file_id']},plainText=false)"
            log_lines.append(
                f"{datetime.datetime.now().isoformat()}  FAILED   {file_url}"
            )
            if os.path.exists(dest):
                os.remove(dest)

        time.sleep(DELAY_SECONDS)

    # ------------------------------------------------------------------ #
    # Download videos                                                      #
    # ------------------------------------------------------------------ #

    if include_video:
        for v in sorted(video_matches, key=lambda x: x["date"], reverse=True):
            dest = make_video_dest_path(v["title"], v["date"], args.output_dir)
            label = os.path.basename(dest)

            if video_already_exists(dest):
                print(f"  skip (exists)  {label}")
                skipped += 1
                continue

            print(f"  [{v['date']}] {v['title']}")
            print(f"  downloading    {label}")
            print(f"  source         {v['youtube_url']}")

            if download_video(v["youtube_url"], dest):
                downloaded += 1
                log_lines.append(
                    f"{datetime.datetime.now().isoformat()}  OK       {dest}"
                )
            else:
                failed += 1
                log_lines.append(
                    f"{datetime.datetime.now().isoformat()}  FAILED   {v['youtube_url']}"
                )

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
#    python3 scripts/download-cheshire-agendas.py --dry-run
#
# 2. Download docs + video recordings for the past 30 days:
#    python3 scripts/download-cheshire-agendas.py --include-video
#
# 3. Narrow to one board:
#    python3 scripts/download-cheshire-agendas.py --board "Town Council"
#
# 4. Change the lookback window:
#    python3 scripts/download-cheshire-agendas.py --days 7
#
# 5. Also include full agenda packets (large files):
#    python3 scripts/download-cheshire-agendas.py --include-packets
#
# 6. Documents only (no video even if flag is passed):
#    python3 scripts/download-cheshire-agendas.py --docs-only
#
# 7. Save files somewhere else:
#    python3 scripts/download-cheshire-agendas.py --output-dir ~/Downloads/cheshire
#
# 8. Run on a schedule (cron — 8 AM daily):
#    0 8 * * * cd /path/to/repo && python3 scripts/download-cheshire-agendas.py
#
# 9. Run daily with video included:
#    0 8 * * * cd /path/to/repo && python3 scripts/download-cheshire-agendas.py --include-video
#
# 10. Process downloaded PDFs with Claude afterward:
#    python3 scripts/download-cheshire-agendas.py && bash scripts/batch-process.sh beat-archive/cheshire-agendas/
#
# NOTE: The --ahead flag (default: 7 days) captures agendas for upcoming meetings
# that have already been published. Run daily to stay current.
#
# NOTE: The CivicClerk API is public and requires no authentication for
# published documents. No browser or Playwright needed. The API uses OData
# skiptoken pagination — this script follows @odata.nextLink automatically.
#
# NOTE: Cheshire's CivicClerk instance has no media/video fields populated
# across its entire event history. All video is on YouTube @CheshireChannel14.
# Only select boards are recorded: Town Council, Planning and Zoning,
# Next Generation School Building Committee, WPCA, BOE, and occasional others.
# Meetings are not recorded every session — coverage is irregular.
#
# NOTE: YouTube upload dates are not exposed by yt-dlp's flat-playlist mode
# for this channel. The script parses meeting dates directly from video titles,
# which use formats like "Town Council 3-23-26" or "Planning and Zoning
# February 24, 2025". Titles without a parseable date are skipped.
#
# NOTE: Use --include-video to download YouTube video recordings via yt-dlp.
# Videos range from ~200 MB to ~1 GB. Files that already exist on disk
# are skipped, so re-runs are safe.
