#!/usr/bin/env python3
# download-meriden-agendas.py
# Download municipal meeting agendas, minutes, and video recordings from
# Meriden CT for meetings within the past N days (and up to 7 days ahead,
# to catch agendas posted early for upcoming meetings).
#
# USAGE:
#   python3 scripts/download-meriden-agendas.py [options]
#
# REQUIREMENTS:
#   - Python 3.6+  (no third-party packages needed for docs)
#   - yt-dlp       (required for --include-video; pip install yt-dlp)
#   - Internet connection
#
# WHAT IT DOES:
#   1. Fetches the full board/subfolder tree from the Documents-On-Demand API
#   2. For each board's Agendas and Minutes subfolders, fetches the document list
#   3. Parses the meeting date from each document's title
#   4. Downloads documents whose meeting date falls within the date window
#      to beat-archive/meriden-agendas/YYYY-MM/
#   5. Downloads CHAMP DS meeting video recordings via yt-dlp (--include-video)
#   6. Appends a download log to beat-archive/meriden-agendas/download-log.txt
#
# SITE STRUCTURE:
#   Meriden CT uses two systems:
#
#   Documents-on-Demand (https://meridencityct.documents-on-demand.com/)
#     Public REST API — no authentication needed:
#       GET /meta/rootfolder         → all boards + subfolder keys
#       GET /meta/docfolder?containerId={key} → year tree with documents
#       GET /document/{key}/{filename}.PDF → PDF download
#     Meeting dates are embedded in titles: "City Council Agenda April 06, 2026-Packet"
#
#   CHAMP DS video (https://play.champds.com/meridenct/archive/1):
#     REST API (host: playapi.champds.com/meridenct):
#       GET /archiveGroupDate/1/LOCAL/{start}/{end}/ → event list
#       GET /event/{CustomerEventID} → MediaInfo.VOD2 path (HLS token)
#     HLS stream: https://securestream11.champds.com{VOD2}
#     Downloaded via yt-dlp with Referer header.

import argparse
import datetime
import glob
import json
import os
import re
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

YT_DLP_NODE = "node:/home/richkirby/.nvm/versions/node/v20.20.2/bin/node"  # yt-dlp needs Node 20+; system node is 18

# --- Configuration ---
API_BASE = "https://meridencityct.documents-on-demand.com"
CHAMP_API = "https://playapi.champds.com/meridenct"
CHAMP_PLAY = "https://play.champds.com/meridenct"
CHAMP_STREAM = "https://securestream11.champds.com"
CHAMP_ARCHIVE_GROUP = 1
OUTPUT_DIR = "beat-archive/meriden-agendas"
DAYS_BACK = 4
DAYS_AHEAD = 7   # capture agendas posted early for upcoming meetings
DELAY_SECONDS = 1

# Subfolder title prefixes to download (lowercased, case-insensitive startswith match)
DEFAULT_TYPES = {"agenda", "minutes"}

UA = "Meriden-Agendas-Downloader/1.0 (journalism research)"

MONTHS = {
    "January": 1, "February": 2, "March": 3, "April": 4,
    "May": 5, "June": 6, "July": 7, "August": 8,
    "September": 9, "October": 10, "November": 11, "December": 12,
}
DATE_RE = re.compile(
    r"(January|February|March|April|May|June|July|August"
    r"|September|October|November|December)"
    r"\s+(\d{1,2}),?\s+(\d{4})"
)


# --- HTTP helpers ---

def fetch_json(url, referer=None):
    """GET url and return parsed JSON, or None on error."""
    headers = {"User-Agent": UA, "Accept": "application/json",
               "X-Requested-With": "XMLHttpRequest"}
    if referer:
        headers["Referer"] = referer
    req = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            raw = r.read()
        import gzip as _gz
        if raw[:2] == b"\x1f\x8b":
            raw = _gz.decompress(raw)
        return json.loads(raw.decode("utf-8", errors="replace"))
    except urllib.error.URLError as e:
        print(f"  ERROR fetching {url}: {e}", file=sys.stderr)
        return None


def download_file(doc_key, file_name, file_type, dest_path):
    """Download a Documents-On-Demand file to dest_path. Returns True on success."""
    encoded = urllib.parse.quote(file_name, safe="")
    url = f"{API_BASE}/document/{doc_key}/{encoded}.{file_type}"
    req = urllib.request.Request(url, headers={
        "User-Agent": UA, "Referer": API_BASE + "/"
    })
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            with open(dest_path, "wb") as f:
                f.write(r.read())
        return True
    except Exception as e:
        print(f"  WARNING: {e}", file=sys.stderr)
        return False


def download_video(hls_url, dest_template):
    """
    Download a CHAMP DS HLS recording via yt-dlp.
    dest_template must end in .%(ext)s.
    Returns True on success.
    """
    cmd = [
        "yt-dlp", "--js-runtimes", YT_DLP_NODE,
        "--no-playlist",
        "-f", "bestvideo+bestaudio/best",
        "--merge-output-format", "mp4",
        "--add-header", f"Referer:{CHAMP_PLAY}/archive/1",
        "-o", dest_template,
        "--no-overwrites",
        "--quiet",
        "--no-warnings",
        hls_url,
    ]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=7200)
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
        print(f"  WARNING: yt-dlp timed out for {hls_url}", file=sys.stderr)
        return False
    except Exception as e:
        print(f"  WARNING: yt-dlp error: {e}", file=sys.stderr)
        return False


def video_already_exists(dest_template):
    """Return True if any file matching dest_template (with %(ext)s) already exists."""
    base = dest_template.replace(".%(ext)s", "")
    return bool(glob.glob(base + ".*"))


# --- CHAMP DS helpers ---

def get_champ_events(cutoff, future_limit):
    """Return list of recorded CHAMP events (EventMediaClassID==2) in date range."""
    start = cutoff.strftime("%Y-%m-%dT00:00:00")
    end = future_limit.strftime("%Y-%m-%dT23:59:59")
    url = f"{CHAMP_API}/archiveGroupDate/{CHAMP_ARCHIVE_GROUP}/LOCAL/{start}/{end}/"
    events = fetch_json(url, referer=f"{CHAMP_PLAY}/archive/1")
    if not events or not isinstance(events, list):
        return []
    return [e for e in events if e.get("EventMediaClassID") == 2]


def get_champ_vod2(event_id):
    """
    Fetch event detail and return (hls_url, title).
    Returns (None, None) on failure.
    VOD2 is an HLS path returned by the event API, hosted on securestream11.
    """
    data = fetch_json(f"{CHAMP_API}/event/{event_id}",
                      referer=f"{CHAMP_PLAY}/archive/1")
    if not data:
        return None, None
    title = (data.get("Event") or {}).get("EventTitle", "")
    vod2_path = (data.get("MediaInfo") or {}).get("VOD2")
    if not vod2_path:
        return None, None

    # ServiceTypeID=8 holds the stream host; fall back to CHAMP_STREAM constant
    stream_host = CHAMP_STREAM
    svcs = data.get("ServicesAndMachineInfo") or {}
    svc8 = svcs.get("8")
    if svc8 and isinstance(svc8, dict):
        base = svc8.get("URLBase", "")
        if base.startswith("https://"):
            stream_host = base

    return stream_host.rstrip("/") + vod2_path, title


# --- Utilities ---

def slugify(text, max_len=60):
    text = text.lower().strip()
    text = re.sub(r"[/\\]", "-", text)
    text = re.sub(r"\s+-\s+", "-", text)
    text = re.sub(r"[^\w\s-]", "", text)
    text = re.sub(r"[\s_]+", "-", text)
    text = re.sub(r"-{2,}", "-", text)
    return text.strip("-")[:max_len]


def parse_date_from_title(title):
    """Extract a meeting date from a document title. Returns a date or None."""
    m = DATE_RE.search(title)
    if not m:
        return None
    try:
        return datetime.date(int(m.group(3)), MONTHS[m.group(1)], int(m.group(2)))
    except ValueError:
        return None


def extract_variant(title):
    """Return the part of the title after '{Month} {DD}, {YYYY}-', or ''."""
    m = DATE_RE.search(title)
    if not m:
        return ""
    rest = title[m.end():]
    return re.sub(r"^[-\s]+", "", rest).strip()


def make_dest_path(board_name, subfolder_type, meeting_date, file_name, output_dir):
    date_prefix = meeting_date.strftime("%Y-%m-%d")
    month_dir = meeting_date.strftime("%Y-%m")
    board_slug = slugify(board_name, max_len=35)
    type_slug = slugify(subfolder_type, max_len=10)
    variant = extract_variant(file_name)
    doc_slug = slugify(variant, max_len=40) if variant else slugify(file_name, max_len=40)
    month_path = os.path.join(output_dir, month_dir)
    os.makedirs(month_path, exist_ok=True)
    fname = f"{date_prefix}-{board_slug}-{type_slug}"
    if doc_slug:
        fname += f"-{doc_slug}"
    fname += ".pdf"
    return os.path.join(month_path, fname)


def make_video_dest_path(event_title, meeting_date, output_dir):
    """Return yt-dlp output template (ends in .%(ext)s) for a video download."""
    date_prefix = meeting_date.strftime("%Y-%m-%d")
    month_dir = meeting_date.strftime("%Y-%m")
    title_slug = slugify(event_title, max_len=50)
    month_path = os.path.join(output_dir, month_dir)
    os.makedirs(month_path, exist_ok=True)
    return os.path.join(month_path, f"{date_prefix}-{title_slug}-video.%(ext)s")


def walk_docs(nodes, results):
    """Recursively collect document nodes from a docfolder response tree."""
    for node in nodes:
        if node.get("folder") is False and node.get("fileType"):
            results.append(node)
        children = node.get("children") or []
        if children:
            walk_docs(children, results)


# --- Main ---

def main():
    parser = argparse.ArgumentParser(
        description=(
            "Download Meriden CT municipal agendas and minutes via the "
            "Documents-On-Demand public API."
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
        help="Also download Packets subfolders (can be large)",
    )
    parser.add_argument(
        "--include-video", action="store_true",
        help="Also download CHAMP DS meeting video recordings via yt-dlp",
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
        doc_types.add("packet")

    include_video = args.include_video and not args.docs_only

    print(f"Date window : {cutoff} to {future_limit}")
    print(f"Portal      : {API_BASE}")
    if not args.dry_run:
        print(f"Output dir  : {args.output_dir}")
    if include_video:
        print("Video       : enabled (yt-dlp / CHAMP DS HLS)")
    print()

    # --- Step 1: fetch the folder tree ---
    print("Fetching folder tree from Documents-On-Demand API...")
    tree = fetch_json(f"{API_BASE}/meta/rootfolder")
    if not tree:
        print("ERROR: Could not fetch folder tree.", file=sys.stderr)
        sys.exit(1)

    # Root is a list with one top-level "City of Meriden" node
    root = tree[0]
    boards = root.get("children", [])
    print(f"Found {len(boards)} board(s).\n")

    if args.board:
        filter_name = args.board.lower()
        boards = [b for b in boards if filter_name in b.get("title", "").lower()]
        print(f"Filtered to {len(boards)} board(s) matching '{args.board}'.\n")

    # --- Step 2: collect matching documents ---
    matches = []

    for board in boards:
        board_name = board.get("title", "Unknown")
        subfolders = board.get("children", [])

        for subfolder in subfolders:
            sf_title = subfolder.get("title", "")
            sf_lower = sf_title.lower()

            # Match by prefix: "Agendas" and "Agenda" both start with "agenda"
            if not any(sf_lower.startswith(t) for t in doc_types):
                continue

            sf_key = subfolder.get("key")
            if not sf_key:
                continue

            url = f"{API_BASE}/meta/docfolder?containerId={sf_key}"
            folder_data = fetch_json(url)
            if not folder_data:
                continue

            docs = []
            walk_docs(folder_data, docs)

            for doc in docs:
                title = doc.get("title", "")
                meeting_date = parse_date_from_title(title)
                if not meeting_date:
                    continue
                if meeting_date < cutoff or meeting_date > future_limit:
                    continue

                matches.append({
                    "board": board_name,
                    "subfolder": sf_title,
                    "meeting_date": meeting_date,
                    "doc_key": doc.get("key"),
                    "file_name": doc.get("fileName") or title,
                    "file_type": doc.get("fileType", "PDF"),
                    "title": title,
                })

            time.sleep(0.2)

    matches.sort(key=lambda x: (x["meeting_date"], x["board"]), reverse=True)

    # --- Collect video events ---
    video_matches = []
    if include_video or args.dry_run:
        print("Fetching CHAMP DS video events...")
        champ_events = get_champ_events(cutoff, future_limit)
        for e in champ_events:
            raw_dt = e.get("EventDateTimeLocal") or e.get("EventDateTimeUTC") or ""
            try:
                event_date = datetime.datetime.strptime(raw_dt[:10], "%Y-%m-%d").date()
            except ValueError:
                continue
            video_matches.append({
                "title": e.get("EventTitle", ""),
                "date": event_date,
                "customer_event_id": e.get("CustomerEventID"),
            })
        video_matches.sort(key=lambda x: x["date"], reverse=True)
        print(f"Found {len(video_matches)} video recording(s).\n")

    print(
        f"Found {len(matches)} document(s) across "
        f"{len({m['board'] for m in matches})} board(s)."
    )
    print()

    if not matches and not video_matches:
        return

    if args.dry_run:
        print(f"{'Board':<38} {'Date':<12} {'Type':<9} Title")
        print("-" * 85)
        for m in matches:
            print(
                f"{m['board'][:37]:<38} {m['meeting_date']!s:<12} "
                f"{m['subfolder'][:8]:<9} {m['title'][:40]}"
            )
        if video_matches:
            print()
            print(f"{'Video Title':<60} {'Date':<12}")
            print("-" * 72)
            for v in video_matches:
                print(f"{v['title'][:59]:<60} {v['date']!s}")
        total = len(matches) + len(video_matches)
        print(f"\n{total} item(s). Re-run without --dry-run to download.")
        return

    # --- Step 3: download ---
    os.makedirs(args.output_dir, exist_ok=True)
    log_path = os.path.join(args.output_dir, "download-log.txt")
    log_lines = []
    downloaded = skipped = failed = 0

    for m in matches:
        dest = make_dest_path(
            m["board"], m["subfolder"], m["meeting_date"],
            m["file_name"], args.output_dir,
        )
        label = os.path.basename(dest)

        if os.path.exists(dest):
            print(f"  skip (exists)  {label}")
            skipped += 1
            continue

        print(f"  [{m['meeting_date']}] {m['board']} — {m['subfolder']}")
        print(f"  downloading    {label}")

        if download_file(m["doc_key"], m["file_name"], m["file_type"], dest):
            downloaded += 1
            log_lines.append(
                f"{datetime.datetime.now().isoformat()}  OK       {dest}"
            )
        else:
            failed += 1
            log_lines.append(
                f"{datetime.datetime.now().isoformat()}  FAILED   "
                f"{m['doc_key']}/{m['file_name']}.{m['file_type']}"
            )
            if os.path.exists(dest):
                os.remove(dest)

        time.sleep(DELAY_SECONDS)

    if include_video:
        for v in video_matches:
            dest_template = make_video_dest_path(
                v["title"], v["date"], args.output_dir
            )

            if video_already_exists(dest_template):
                skipped += 1
                continue

            event_id = v["customer_event_id"]
            if event_id is None:
                print(f"  WARNING: no CustomerEventID for '{v['title']}'",
                      file=sys.stderr)
                continue

            hls_url, _ = get_champ_vod2(event_id)
            if not hls_url:
                failed += 1
                log_lines.append(
                    f"{datetime.datetime.now().isoformat()}  FAILED   "
                    f"CHAMP event {event_id} — no VOD2 URL"
                )
                continue

            label = os.path.basename(dest_template)
            print(f"  [{v['date']}] {v['title'][:50]}")
            print(f"  downloading    {label}  (HLS via yt-dlp)")

            if download_video(hls_url, dest_template):
                downloaded += 1
                log_lines.append(
                    f"{datetime.datetime.now().isoformat()}  OK       {dest_template}"
                )
            else:
                failed += 1
                log_lines.append(
                    f"{datetime.datetime.now().isoformat()}  FAILED   {hls_url}"
                )

            time.sleep(DELAY_SECONDS)

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
#    python3 scripts/download-meriden-agendas.py --dry-run
#
# 2. Download docs + CHAMP video recordings for the past 30 days:
#    python3 scripts/download-meriden-agendas.py --include-video
#
# 3. Narrow to one board:
#    python3 scripts/download-meriden-agendas.py --board "City Council"
#
# 4. Change the lookback window:
#    python3 scripts/download-meriden-agendas.py --days 7
#
# 5. Also include full agenda packets (large files):
#    python3 scripts/download-meriden-agendas.py --include-packets
#
# 6. Documents only (no video even if flag is passed):
#    python3 scripts/download-meriden-agendas.py --docs-only
#
# 7. Save files somewhere else:
#    python3 scripts/download-meriden-agendas.py --output-dir ~/Downloads/meriden
#
# 8. Run on a schedule (cron — 8 AM daily):
#    0 8 * * * cd /path/to/repo && python3 scripts/download-meriden-agendas.py
#
# NOTE: The --ahead flag (default: 7 days) captures agendas for upcoming meetings
# that have already been published. Run daily to stay current.
#
# NOTE: Meeting dates are parsed from document titles (e.g., "City Council
# Agenda April 06, 2026-Packet"). Documents without a recognizable date in the
# title are skipped. This affects a small number of non-standard documents.
#
# NOTE: The Documents-On-Demand API is public and requires no authentication.
# No browser or Playwright needed — all endpoints accept plain HTTP GET requests.
#
# NOTE: CHAMP DS videos are HLS streams fetched fresh from the event API on each
# run. The HLS token in the VOD2 URL is time-limited; the script always fetches a
# fresh URL before downloading. Files that already exist are skipped.
