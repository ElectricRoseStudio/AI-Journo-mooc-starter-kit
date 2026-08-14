#!/usr/bin/env python3
# download-ledyard-agendas.py
# Download municipal meeting agendas, minutes, and video recordings from
# the Ledyard, CT Agenda Center (Legistar) for meetings within the past
# N days (and up to M days ahead).
#
# USAGE:
#   python3 scripts/download-ledyard-agendas.py [options]
#
# REQUIREMENTS:
#   - Python 3.6+  (no third-party packages needed for docs)
#   - yt-dlp       (for video: pip install yt-dlp; handles HLS natively)
#   - Internet connection
#
# WHAT IT DOES:
#   Documents (default or --docs-only):
#     1. Queries the public Legistar Web API for events whose date falls
#        within DAYS_BACK days ago through DAYS_AHEAD days ahead
#     2. Downloads each event's agenda/minutes PDF (already full URLs on
#        Legistar's own CDN — no auth, no HTML scraping needed) to
#        beat-archive/ledyard-agendas/YYYY-MM/
#
#   Video (--include-video or --video-only; off by default):
#     3. For each event with a published video (EventMedia set), resolves
#        the underlying Granicus HLS stream URL and downloads it with
#        yt-dlp (Granicus itself isn't a yt-dlp-supported site, but its
#        raw .m3u8 stream URL is — yt-dlp handles generic HLS natively)
#
# SITE STRUCTURE — Legistar (Granicus), NOT CivicPlus:
#   Legistar's own AgendaCenter-equivalent hub the town links as
#   "Agenda Center" is https://ledyardct.legistar.com/Calendar.aspx, but
#   this script bypasses that HTML page entirely in favor of Legistar's
#   public JSON REST API, which is far more reliable to parse:
#
#   API:        https://webapi.legistar.com/v1/ledyardct/Events
#                 ?$filter=EventDate ge datetime'YYYY-MM-DD' and
#                          EventDate le datetime'YYYY-MM-DD'
#               Returns EventBodyName (board), EventDate, EventAgendaFile
#               and EventMinutesFile (direct PDF URLs, null if not yet
#               posted), and EventMedia (an integer clip ID, null if no
#               video).
#
#   Video:      EventMedia -> https://ledyardct.granicus.com/player/clip/
#               {EventMedia}?view_id=1&redirect=true (a Granicus player
#               page). That page embeds a JS line
#               `video_url="https://archive-stream.granicus.com/...m3u8"`
#               pointing at the actual HLS stream — this script regexes
#               that out and hands the .m3u8 straight to yt-dlp, which
#               resolves it as a native HLS format directly (confirmed:
#               1920x1080, working download). No Video.aspx redirect hop
#               or "G" site GUID is needed; the clip ID alone is enough.
#
# NOTE: EventAgendaStatusName / EventMinutesStatusName can be "Canceled",
#   "Draft", or "Final" — this script doesn't filter on status, it just
#   downloads whatever file URL is present (a canceled meeting usually
#   has no agenda file at all, so it's naturally skipped).

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

# --- Configuration ---
API_BASE = "https://webapi.legistar.com/v1/ledyardct/Events"
GRANICUS_CLIP_URL = "https://ledyardct.granicus.com/player/clip/{media_id}?view_id=1&redirect=true"
OUTPUT_DIR = "beat-archive/ledyard-agendas"
DAYS_BACK = 4
DAYS_AHEAD = 7
DELAY_SECONDS = 0.5

UA = "Ledyard-CT-Agendas-Downloader/1.0 (journalism research)"

_VIDEO_URL_RE = re.compile(r'video_url\s*=\s*"([^"]+\.m3u8)"')


# --- HTTP helpers ---

def fetch_json(url):
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return json.loads(r.read().decode("utf-8", errors="replace"))
    except Exception as e:
        print(f"  ERROR fetching {url}: {e}", file=sys.stderr)
        return None


def fetch_html(url):
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            charset = r.headers.get_content_charset() or "utf-8"
            return r.read().decode(charset, errors="replace")
    except Exception as e:
        print(f"  WARNING: {e}", file=sys.stderr)
        return None


def download_pdf(url, dest_path):
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "application/pdf,*/*"})
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            with open(dest_path, "wb") as f:
                f.write(r.read())
        return True
    except Exception as e:
        print(f"  WARNING: {e}", file=sys.stderr)
        return False


# --- (1) Documents ---

def collect_events(cutoff, future_limit):
    params = {
        "$filter": (
            f"EventDate ge datetime'{cutoff.isoformat()}' and "
            f"EventDate le datetime'{future_limit.isoformat()}'"
        )
    }
    url = f"{API_BASE}?{urllib.parse.urlencode(params)}"
    data = fetch_json(url)
    return data or []


def collect_docs(events, board_filter, no_minutes, no_agendas):
    docs = []
    for e in events:
        board = (e.get("EventBodyName") or "Unknown Board").strip()
        if board_filter and board_filter not in board.lower():
            continue
        try:
            meeting_date = datetime.date.fromisoformat(e["EventDate"][:10])
        except (KeyError, ValueError):
            continue
        if e.get("EventAgendaFile") and not no_agendas:
            docs.append({"board": board, "meeting_date": meeting_date,
                        "doc_type": "agenda", "href": e["EventAgendaFile"]})
        if e.get("EventMinutesFile") and not no_minutes:
            docs.append({"board": board, "meeting_date": meeting_date,
                        "doc_type": "minutes", "href": e["EventMinutesFile"]})
    return docs


# --- (2) Video ---

def collect_videos(events, board_filter):
    videos = []
    for e in events:
        media_id = e.get("EventMedia")
        if not media_id:
            continue
        board = (e.get("EventBodyName") or "Unknown Board").strip()
        if board_filter and board_filter not in board.lower():
            continue
        try:
            meeting_date = datetime.date.fromisoformat(e["EventDate"][:10])
        except (KeyError, ValueError):
            continue
        videos.append({"board": board, "meeting_date": meeting_date, "media_id": media_id})
    return videos


def resolve_stream_url(media_id):
    """Fetch the Granicus clip player page and pull out its .m3u8 stream URL."""
    html = fetch_html(GRANICUS_CLIP_URL.format(media_id=media_id))
    if not html:
        return None
    m = _VIDEO_URL_RE.search(html)
    return m.group(1) if m else None


def download_video(media_id, dest_path):
    stream_url = resolve_stream_url(media_id)
    if not stream_url:
        print(f"  WARNING: could not resolve stream URL for clip {media_id}", file=sys.stderr)
        return False
    cmd = [
        "yt-dlp", "--no-warnings", "--quiet",
        "--merge-output-format", "mp4",
        "-o", dest_path,
        stream_url,
    ]
    try:
        result = subprocess.run(cmd, timeout=1800)
        return result.returncode == 0
    except FileNotFoundError:
        print("  ERROR: yt-dlp not found. Install with: pip install yt-dlp", file=sys.stderr)
        return False
    except subprocess.TimeoutExpired:
        print(f"  WARNING: yt-dlp timed out on clip {media_id}", file=sys.stderr)
        return False


# --- File naming ---

def slugify(text, max_len=55):
    text = str(text).lower().strip()
    text = re.sub(r"[/\\&]", "-", text)
    text = re.sub(r"[^\w\s-]", "", text)
    text = re.sub(r"[\s_]+", "-", text)
    text = re.sub(r"-{2,}", "-", text)
    return text.strip("-")[:max_len]


def make_path(board, doc_type, meeting_date, output_dir, ext, counter=0):
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
            "Download Ledyard CT municipal agendas, minutes, and video "
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
    parser.add_argument("--include-video", action="store_true", help="Also download published video recordings")
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

    print("Querying Legistar events...")
    events = collect_events(cutoff, future_limit)
    print(f"  {len(events)} event(s) in window.\n")

    docs = collect_docs(events, board_filter, args.no_minutes, args.no_agendas) if do_docs else []
    videos = collect_videos(events, board_filter) if do_video else []

    if do_docs:
        print(f"Documents   : {len(docs)} found\n")
    if do_video:
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
            print(f"{'Board':<40} {'Date':<12} Video (clip id)")
            print("-" * 65)
            for v in videos:
                print(f"{v['board'][:39]:<40} {v['meeting_date']!s:<12} {v['media_id']}")
            print()
        print(f"{total} item(s). Re-run without --dry-run to download.")
        return

    os.makedirs(args.output_dir, exist_ok=True)
    log_path = os.path.join(args.output_dir, "download-log.txt")
    log_lines = []
    downloaded = skipped = failed = 0

    for d in docs:
        dest = make_path(d["board"], d["doc_type"], d["meeting_date"], args.output_dir, ".pdf", counter=d["counter"])
        label = os.path.basename(dest)
        if os.path.exists(dest):
            print(f"  skip (exists)  {label}")
            skipped += 1
            continue
        print(f"  [{d['meeting_date']}] {d['board']} — {d['doc_type']}")
        print(f"  downloading    {label}")
        if download_pdf(d["href"], dest):
            downloaded += 1
            log_lines.append(f"{datetime.datetime.now().isoformat()}  OK       {dest}")
        else:
            failed += 1
            log_lines.append(f"{datetime.datetime.now().isoformat()}  FAILED   {d['href']}")
            if os.path.exists(dest):
                os.remove(dest)
        time.sleep(DELAY_SECONDS)

    for v in videos:
        dest = make_path(v["board"], "video", v["meeting_date"], args.output_dir, ".mp4", counter=v["counter"])
        label = os.path.basename(dest)
        if os.path.exists(dest):
            print(f"  skip (exists)  {label}")
            skipped += 1
            continue
        print(f"  [{v['meeting_date']}] {v['board']} — video (clip {v['media_id']})")
        print(f"  downloading    {label}")
        if download_video(v["media_id"], dest):
            downloaded += 1
            log_lines.append(f"{datetime.datetime.now().isoformat()}  OK       {dest}")
        else:
            failed += 1
            log_lines.append(f"{datetime.datetime.now().isoformat()}  FAILED   video clip {v['media_id']}")

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
#    python3 scripts/download-ledyard-agendas.py --dry-run
#
# 2. Just Town Council:
#    python3 scripts/download-ledyard-agendas.py --board "Town Council"
#
# 3. Change the lookback window:
#    python3 scripts/download-ledyard-agendas.py --days 14
#
# 4. Also download published video (real, active coverage — most meetings have it):
#    python3 scripts/download-ledyard-agendas.py --include-video
#
# 5. Run on a schedule (cron — evening):
#    0 21 * * 1-5 cd /path/to/repo && python3 scripts/download-ledyard-agendas.py --include-video
