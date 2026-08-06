#!/usr/bin/env python3
# download-melrose-agendas.py
# Download municipal meeting agendas, minutes, and video recordings from
# Melrose MA for meetings whose date falls within the past N days (and up
# to 7 days ahead).
#
# USAGE:
#   python3 scripts/download-melrose-agendas.py [options]
#
# REQUIREMENTS:
#   - Python 3.6+  (no third-party packages needed for docs)
#   - yt-dlp with curl_cffi installed, for video (see VIDEO NOTE below):
#       pip install --user --break-system-packages "curl_cffi>=0.10,<0.16"
#     (installed once on this machine on 2026-08-06; a fresh machine needs
#     this before video downloads will work — without it, yt-dlp's Vimeo
#     extractor fails outright, see VIDEO NOTE)
#   - Internet connection
#
# WHAT IT DOES:
#   1. Calls the CivicClerk OData API to fetch all events in the date
#      window (documents AND video, unlike Enfield/Medford — see below)
#   2. Downloads Agenda/Agenda Packet/Minutes/Notice PDFs via the
#      Meetings/GetMeetingFileStream endpoint
#   3. Separately crawls MMTV's Vimeo archive (vimeo.com/mmtv3) for
#      recordings within the date window, parsed from video titles
#   4. Appends a download log to beat-archive/melrose-agendas/download-log.txt
#
# SITE STRUCTURE:
#   CMS: CivicPlus front end, but the actual "Agendas & Minutes" page
#        (https://www.cityofmelrose.org/129/Agendas-Minutes) just embeds a
#        CivicClerk widget (tenant "MELROSEMA") — same underlying platform
#        as Enfield CT and Medford MA's City Council. Unlike Medford, this
#        ONE CivicClerk instance covers every board, including School
#        Committee and Park Commission — no separate scrape needed for
#        either (confirmed via a full year of events: both categories are
#        native CivicClerk categories with normal publishedFiles).
#
#   Public portal: https://melrosema.portal.civicclerk.com
#   OData API:     https://melrosema.api.civicclerk.com/v1
#     GET /Events?$filter=eventDate ge {ISO}Z and eventDate le {ISO}Z
#         &$orderby=eventDate asc
#   Document download:
#     GET /v1/Meetings/GetMeetingFileStream(fileId={id},plainText=false)
#
#   Checked a full year of Melrose's CivicClerk event data directly: every
#   event's mediaStreamPath/youtubeVideoId field was empty — Melrose does
#   NOT post recordings through CivicClerk itself (unlike Enfield, which
#   does). Video is only on MMTV's Vimeo account.
#
# VIDEO NOTE — MMTV (Melrose Massachusetts Television, the town's PEG
# access nonprofit) posts recordings to vimeo.com/mmtv3, newest first, one
# video per meeting, no separate per-board playlists. Two things had to be
# worked out to make this scriptable:
#
#   1. yt-dlp's Vimeo extractor needs curl_cffi for a browser-impersonation
#      handshake or Vimeo's API 401s immediately. This yt-dlp build
#      (2026.07.04) only supports curl_cffi 0.5.10 or 0.10.x-0.15.x —
#      curl_cffi 0.16 (the current release as of 2026-08) raises
#      ImportError and silently falls back to no impersonation at all, an
#      easy trap. Installed the pinned range with --user
#      --break-system-packages after confirming with the user (2026-08-06)
#      — this is a Debian-managed Python and the repo's .venv/ turned out
#      to be an incomplete/unused environment (no pip inside it).
#   2. Even with impersonation working, yt-dlp's normal vimeo.com/{id}
#      extraction path still 401s — it depends on a viewer JWT fetch that
#      fails for this account/video combination. The fix: request the
#      video via its embed URL instead, exactly as Vimeo's own oEmbed API
#      returns it:
#        https://player.vimeo.com/video/{id}?app_id=122963
#      This is what confirmed the video is genuinely public (oEmbed
#      succeeds unauthenticated) and is the URL form this script always
#      downloads through — never the bare vimeo.com/{id} form.
#
#   Video titles are plain text with no structured metadata, e.g.
#   "Melrose School Committee July 28th 2026" or "Appropriations and
#   Oversight June 11th 2026 Budget Hearing" (trailing words after the
#   date, when present, are kept as a suffix). Board names parsed from
#   titles are cosmetic only — they are NOT cross-matched against
#   CivicClerk's categoryName strings (the two vocabularies don't agree,
#   e.g. "Appropriations and Oversight" here vs. "Appropriations &
#   Oversight Committee" in the API), so video files are named and filed
#   independently of the documents pipeline.

import argparse
import datetime
import json
import os
import re
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

# --- Configuration ---
CIVICCLERK_API = "https://melrosema.api.civicclerk.com/v1"
VIMEO_PROFILE_URL = "https://vimeo.com/mmtv3"
VIMEO_APP_ID = "122963"  # confirmed via https://vimeo.com/api/oembed.json?url=...
OUTPUT_DIR = "beat-archive/melrose-agendas"
DAYS_BACK = 4
DAYS_AHEAD = 7
API_DELAY = 0.25

UA = "Mozilla/5.0 (X11; Linux x86_64; rv:120.0) Gecko/20100101 Firefox/120.0"

DOWNLOAD_TYPES = {"Agenda", "Agenda Packet", "Minutes", "Notice"}

_TITLE_DATE_RE = re.compile(
    r"(January|February|March|April|May|June|July|August|September|"
    r"October|November|December)\s+(\d{1,2})(?:st|nd|rd|th)?,?\s+(\d{4})",
    re.IGNORECASE,
)


# --- HTTP helpers ---

def fetch_json(url):
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.load(r)


def download_file(url, dest_path):
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "*/*"})
    try:
        with urllib.request.urlopen(req, timeout=120) as r:
            with open(dest_path, "wb") as f:
                f.write(r.read())
        return True
    except Exception as e:
        print(f"  WARNING: {e}", file=sys.stderr)
        return False


# --- CivicClerk: documents ---

def fetch_civicclerk_events(cutoff, future_limit):
    start_iso = cutoff.strftime("%Y-%m-%dT00:00:00Z")
    end_iso = future_limit.strftime("%Y-%m-%dT23:59:59Z")
    filter_expr = f"eventDate ge {start_iso} and eventDate le {end_iso}"
    params = urllib.parse.urlencode({"$filter": filter_expr, "$orderby": "eventDate asc"})
    url = f"{CIVICCLERK_API}/Events?{params}"
    events = []
    while url:
        data = fetch_json(url)
        events.extend(data.get("value", []))
        url = data.get("@odata.nextLink")
        if url:
            time.sleep(API_DELAY)
    return events


def make_civicclerk_doc_url(file_id):
    return f"{CIVICCLERK_API}/Meetings/GetMeetingFileStream(fileId={file_id},plainText=false)"


def collect_docs(cutoff, future_limit, board_filter, no_minutes, no_agendas):
    docs = []
    events = fetch_civicclerk_events(cutoff, future_limit)
    for event in events:
        board = event.get("categoryName", "Unknown Board")
        if board_filter and board_filter not in board.lower():
            continue
        date_str = event.get("eventDate", "")[:10]
        try:
            event_date = datetime.datetime.strptime(date_str, "%Y-%m-%d").date()
        except ValueError:
            continue
        event_name = event.get("eventName", "Meeting")
        for f in event.get("publishedFiles", []):
            doc_type = f.get("type", "")
            if doc_type not in DOWNLOAD_TYPES:
                continue
            if no_minutes and doc_type == "Minutes":
                continue
            if no_agendas and doc_type in {"Agenda", "Agenda Packet"}:
                continue
            file_id = f.get("fileId")
            if not file_id:
                continue
            docs.append({
                "board": board,
                "meeting_date": event_date,
                "extra": event_name,
                "doc_type": doc_type,
                "href": make_civicclerk_doc_url(file_id),
            })
    return docs


# --- MMTV / Vimeo: video ---

def parse_title(title):
    """Split a Vimeo title into (board, meeting_date, suffix), or None if unparseable."""
    m = _TITLE_DATE_RE.search(title)
    if not m:
        return None
    board = title[:m.start()].strip(" -:")
    suffix = title[m.end():].strip(" -:")
    month, day, year = m.group(1), int(m.group(2)), int(m.group(3))
    try:
        meeting_date = datetime.datetime.strptime(f"{month} {day}, {year}", "%B %d, %Y").date()
    except ValueError:
        return None
    return board or "MMTV", meeting_date, suffix


def collect_vimeo_videos(cutoff, future_limit, board_filter, playlist_cap=60):
    """
    List recent MMTV Vimeo uploads (newest first) and return those whose
    parsed date falls in the window. Uses --flat-playlist (no per-video
    HTTP call) purely to enumerate id/title cheaply; actual downloads
    happen later through the player.vimeo.com embed URL.
    """
    ytdlp = shutil.which("yt-dlp")
    if not ytdlp:
        print("  WARNING: yt-dlp not found, skipping video", file=sys.stderr)
        return []

    cmd = [
        ytdlp, "--flat-playlist", "--dump-json",
        "--playlist-end", str(playlist_cap),
        VIMEO_PROFILE_URL,
    ]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
    except subprocess.TimeoutExpired:
        print("  WARNING: Vimeo listing timed out", file=sys.stderr)
        return []
    if result.returncode != 0:
        print(f"  WARNING: Vimeo listing failed: {result.stderr.strip()[:200]}", file=sys.stderr)
        return []

    videos = []
    for line in result.stdout.splitlines():
        try:
            d = json.loads(line)
        except json.JSONDecodeError:
            continue
        title = d.get("title") or ""
        parsed = parse_title(title)
        if not parsed:
            continue
        board, meeting_date, suffix = parsed
        if board_filter and board_filter not in board.lower():
            continue
        if cutoff <= meeting_date <= future_limit:
            videos.append({
                "board": board,
                "meeting_date": meeting_date,
                "extra": suffix,
                "video_id": d.get("id"),
                "title": title,
            })
    return videos


def download_video(video_id, dest_path):
    embed_url = f"https://player.vimeo.com/video/{video_id}?app_id={VIMEO_APP_ID}"
    cmd = [
        "yt-dlp",
        "-f", "bestvideo[height<=720]+bestaudio/best[height<=720]",
        "--merge-output-format", "mp4",
        "-o", dest_path,
        "--no-overwrites",
        "--quiet",
        "--no-warnings",
        embed_url,
    ]
    try:
        subprocess.run(cmd, check=True, timeout=3600)
        return True
    except FileNotFoundError:
        print("  ERROR: yt-dlp not found. Install with: pip install yt-dlp", file=sys.stderr)
        return False
    except subprocess.CalledProcessError as e:
        print(f"  WARNING: yt-dlp failed ({e})", file=sys.stderr)
        return False
    except subprocess.TimeoutExpired:
        print(f"  WARNING: yt-dlp timed out downloading video {video_id}", file=sys.stderr)
        return False


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
    """
    Assign a 0-based counter to each item sharing the same key_fn(item), so
    otherwise-identical (board, date, doc_type) entries get disambiguated
    filenames instead of colliding (e.g. two same-day events under a
    generic category name, each with its own distinct file).
    """
    seen = {}
    for item in items:
        key = key_fn(item)
        item["counter"] = seen.get(key, 0)
        seen[key] = item["counter"] + 1


# --- Main ---

def main():
    parser = argparse.ArgumentParser(
        description=(
            "Download Melrose MA municipal agendas, minutes, and video "
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
    parser.add_argument("--no-minutes", action="store_true", help="Skip minutes")
    parser.add_argument("--no-agendas", action="store_true", help="Skip agendas/packets")
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

    docs = []
    if include_docs:
        print("Fetching events (CivicClerk)...")
        try:
            docs = collect_docs(cutoff, future_limit, board_filter, args.no_minutes, args.no_agendas)
        except Exception as e:
            print(f"  WARNING: CivicClerk fetch failed: {e}", file=sys.stderr)
        print(f"  {len(docs)} document(s).\n")

    videos = []
    if include_video:
        print("Fetching MMTV Vimeo listing...")
        videos = collect_vimeo_videos(cutoff, future_limit, board_filter)
        print(f"  {len(videos)} recording(s).\n")

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
                print(f"{v['board'][:34]:<35} {v['meeting_date']!s:<12} {v['extra'][:27]:<28} {v['video_id']}")
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
        time.sleep(API_DELAY)

    for v in videos:
        dest = make_path(v["board"], "video", v["meeting_date"], v["extra"], args.output_dir,
                          ext=".mp4", counter=v["counter"])
        label = os.path.basename(dest)
        if os.path.exists(dest):
            print(f"  skip (exists)  {label}")
            skipped += 1
            continue
        print(f"  [{v['meeting_date']}] {v['board']} — video (Vimeo {v['video_id']})")
        print(f"  downloading    {label}")
        if download_video(v["video_id"], dest):
            downloaded += 1
            log_lines.append(f"{datetime.datetime.now().isoformat()}  OK       {dest}")
        else:
            failed += 1
            log_lines.append(f"{datetime.datetime.now().isoformat()}  FAILED   vimeo {v['video_id']}")

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
#    python3 scripts/download-melrose-agendas.py --dry-run
#
# 2. Just School Committee:
#    python3 scripts/download-melrose-agendas.py --board "School Committee"
#
# 3. PDFs only (no video):
#    python3 scripts/download-melrose-agendas.py --no-video
#
# 4. Video only:
#    python3 scripts/download-melrose-agendas.py --video-only
#
# 5. Change the lookback window:
#    python3 scripts/download-melrose-agendas.py --days 14
#
# 6. Run on a schedule (cron — evening):
#    0 19 * * 1-5 cd /path/to/repo && python3 scripts/download-melrose-agendas.py
#
# COVERAGE: Every board in Melrose's CivicClerk instance, including School
# Committee (Board of Education) and Park Commission (Parks & Recreation) —
# both native categories there, no external source needed for documents.
# Video comes separately from MMTV's Vimeo account (vimeo.com/mmtv3) since
# CivicClerk itself carries no media for this town — see VIDEO NOTE above
# for the two non-obvious fixes (curl_cffi version pin, embed-URL download
# path) required to make yt-dlp/Vimeo cooperate at all.
