#!/usr/bin/env python3
# download-somerville-agendas.py
# Download municipal meeting agendas, minutes, and video recordings from
# Somerville MA for meetings whose date falls within the past N days (and
# up to 7 days ahead).
#
# USAGE:
#   python3 scripts/download-somerville-agendas.py [options]
#
# REQUIREMENTS:
#   - Python 3.6+  (no third-party packages needed for docs or Granicus video)
#   - yt-dlp       (for YouTube video: pip install yt-dlp)
#   - Internet connection
#
# WHAT IT DOES:
#   1. Documents: queries the city's /meetingdocs page (a Drupal Views
#      listing, date-range filterable) for every board/committee, and
#      downloads each Agenda/Minutes/other document PDF directly from S3.
#   2. Video, three separate sources (see VIDEO NOTE below):
#      a. City Council + its standing committees: Granicus RSS feed,
#         direct HLS download via yt-dlp.
#      b. Other boards/commissions (Planning Board, Zoning, Historic
#         Preservation, etc.): the "Somerville City Commissions & Meetings"
#         YouTube channel.
#      c. School Committee: its own dedicated YouTube channel.
#   3. Appends a download log to beat-archive/somerville-agendas/download-log.txt
#
# SITE STRUCTURE:
#   CMS: Drupal 11 (radix theme). The "Agendas, Minutes, & More" page is a
#   Views listing, NOT a CivicPlus AgendaCenter or CivicClerk widget like
#   every other town in this repo:
#
#     https://www.somervillema.gov/meetingdocs
#       ?field_event_date_value[min]=YYYY-MM-DD
#       &field_event_date_value[max]=YYYY-MM-DD
#       &page=N                                    (0-indexed, 50/page)
#
#   Each row: Date | Title (links to /events/YYYY/MM/DD/{slug}) | Event
#   Documents (zero or more {type, PDF link} pairs — Agenda, Minutes, etc.,
#   each served directly from S3: https://s3.amazonaws.com/somervillema-live/
#   s3fs-public/{YYYY-MM}/{slug}.pdf — no auth, no session needed). Board
#   name is derived by stripping a trailing "Meeting"/"Special Meeting" from
#   the title; rows with no document links (many /events entries are
#   general public events, not board meetings) are skipped naturally.
#
#   Confirmed board coverage: School Committee documents ARE posted through
#   this same /meetingdocs system (checked directly — no separate scrape
#   needed). Recreation Commission is a real, distinct public board (per
#   the city's Boards & Commissions directory) but a full year of
#   /meetingdocs history has zero entries for it — it appears to simply be
#   dormant right now, not sourced elsewhere (unlike Medford's Parks
#   Commission, there's no known alternate archive to point to). It'll be
#   picked up automatically by this same scraper if/when it resumes, since
#   nothing here excludes it by name.
#
# VIDEO NOTE — Somerville splits its meeting video across three genuinely
# separate systems, more than any other town in this repo:
#
#   (a) City Council + standing committees (Finance, Land Use, Legislative
#       Matters, Licenses and Permits, Traffic and Parking, Housing/
#       Community Development and Equity, Public Health and Safety,
#       Sustainability and Infrastructure, School Building Facilities and
#       Maintenance, Confirmation of Appointments and Personnel Matters):
#         https://somervillema.granicus.com/ViewPublisherRSS.php?view_id=1
#       A clean, standard Granicus RSS feed (~100 most recent items,
#       several months of history) with structured <gran:pubDateParts>
#       date attributes — no title date-parsing needed for this source.
#       The feed's own <enclosure> URL (DownloadFile.php) serves a raw
#       archival file that turned out to be ~4.9 GB for a single meeting —
#       impractical to download nightly. Instead this script downloads the
#       same recording via yt-dlp against the player page
#       (player/clip/{id}?view_id=1&redirect=true), which exposes a single
#       HLS rendition per clip — bitrate/resolution vary by clip (seen
#       277k-3735k in spot checks), so the download always uses yt-dlp's
#       "best" selector rather than a hardcoded format ID (an earlier
#       version of this script hardcoded "hls-3735", which worked for one
#       clip and then hard-failed on the next one checked). Still large —
#       low-hundreds-of-MB to low-GB for a multi-hour meeting — but far more
#       reasonable than the raw archival file, and consistent with how
#       every other yt-dlp-based source in this repo is invoked.
#   (b) Other boards/commissions (Planning Board, Zoning Board of Appeals,
#       Historic Preservation Commission, Licensing Commission, Urban
#       Forestry Committee, Redevelopment Authority, etc.):
#         https://www.youtube.com/@SomervilleCommissions-Meetings
#       A general channel, not split into per-board playlists. Confirmed
#       it does NOT carry City Council or School Committee video. Titles
#       are inconsistent ("Somerville {Board} Meeting MM-DD-YYYY",
#       "Somerville {Board} M-D-YY", "Somerville {Board} Meeting - Month D,
#       YYYY", sometimes with a meeting number before the date) — parsed
#       with two date regexes (numeric and full-month-name), tried in that
#       order.
#   (c) School Committee: its own dedicated channel,
#         https://www.youtube.com/@SomervilleSchoolCommittee
#       Clean titles: "School Committee Meeting - Month D, YYYY".
#
#   A now-stale "Somerville City Council Meetings" YouTube playlist exists
#   (last updated October 2025) — not used here; Granicus is the live,
#   current source for Council video.

import argparse
import datetime
import json
import re
import shutil
import subprocess
import sys
import time
import os
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET

# --- Configuration ---
BASE_URL = "https://www.somervillema.gov"
MEETINGDOCS_URL = f"{BASE_URL}/meetingdocs"
GRANICUS_RSS_URL = "https://somervillema.granicus.com/ViewPublisherRSS.php?view_id=1"
GRANICUS_PLAYER_URL = "https://somervillema.granicus.com/player/clip/{clip_id}?view_id=1&redirect=true"
YT_COMMISSIONS_URL = "https://www.youtube.com/@SomervilleCommissions-Meetings/videos"
YT_SCHOOL_COMMITTEE_URL = "https://www.youtube.com/@SomervilleSchoolCommittee/videos"
OUTPUT_DIR = "beat-archive/somerville-agendas"
DAYS_BACK = 4
DAYS_AHEAD = 7
REQUEST_DELAY = 0.4

UA = "Mozilla/5.0 (X11; Linux x86_64; rv:120.0) Gecko/20100101 Firefox/120.0"

_ROW_RE = re.compile(r"<tr>(.*?)</tr>", re.DOTALL)
_DATE_RE = re.compile(r'<time\s+datetime="([^"]+)"')
_TITLE_RE = re.compile(r'href="(/events/[^"]+)"[^>]*hreflang="en">([^<]+)</a>')
_DOC_TYPE_RE = re.compile(
    r'field--name-field-event-document-type[^"]*"[^>]*>.*?field__item">([^<]+)</div>',
    re.DOTALL,
)
_DOC_FILE_RE = re.compile(
    r'field--name-field-event-document field--type-file[^"]*"[^>]*>.*?href="([^"]+)"',
    re.DOTALL,
)

_YT_TEXT_DATE_RE = re.compile(
    r"(January|February|March|April|May|June|July|August|September|"
    r"October|November|December)\s+(\d{1,2}),\s+(\d{4})\s*$",
    re.IGNORECASE,
)
_YT_NUMERIC_DATE_RE = re.compile(r"(\d{1,2})[-/](\d{1,2})[-/](\d{2,4})\s*$")


# --- HTTP helpers ---

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
    # S3-hosted filenames sometimes contain literal spaces (e.g. "Call for
    # the Week of 08-10-26_0.pdf") that urllib won't send correctly
    # unescaped — quote the URL while preserving already-valid punctuation.
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


# --- (1) Documents: /meetingdocs Views listing ---

def strip_board_suffix(title):
    return re.sub(r"\s+(Special\s+)?Meeting\s*$", "", title, flags=re.IGNORECASE).strip()


def fetch_meetingdocs_page(cutoff, future_limit, page):
    params = urllib.parse.urlencode({
        "field_event_date_value[min]": cutoff.isoformat(),
        "field_event_date_value[max]": future_limit.isoformat(),
        "page": page,
    })
    html = fetch_html(f"{MEETINGDOCS_URL}?{params}")
    return html


def collect_docs(cutoff, future_limit, board_filter):
    docs = []
    page = 0
    while True:
        html = fetch_meetingdocs_page(cutoff, future_limit, page)
        if not html:
            break
        rows = _ROW_RE.findall(html)
        # First row is the header (<th> cells) — skip if no <time> present.
        data_rows = [r for r in rows if _DATE_RE.search(r)]
        if not data_rows:
            break
        for row in data_rows:
            date_m = _DATE_RE.search(row)
            title_m = _TITLE_RE.search(row)
            if not date_m or not title_m:
                continue
            try:
                meeting_date = datetime.datetime.fromisoformat(
                    date_m.group(1).replace("Z", "+00:00")
                ).date()
            except ValueError:
                continue
            title = title_m.group(2).strip()
            board = strip_board_suffix(title)
            if board_filter and board_filter not in board.lower():
                continue
            doc_types = _DOC_TYPE_RE.findall(row)
            doc_files = _DOC_FILE_RE.findall(row)
            for doc_type, href in zip(doc_types, doc_files):
                docs.append({
                    "board": board,
                    "meeting_date": meeting_date,
                    "extra": title if title != board else "",
                    "doc_type": doc_type.strip(),
                    "href": href,
                })
        if len(data_rows) < 50:
            break  # last page (fewer than the page size)
        page += 1
        time.sleep(REQUEST_DELAY)

    # The Views listing occasionally repeats the exact same row (same
    # board/date/doc-type/href) verbatim — confirmed directly against the
    # raw page source, not a parsing artifact. Same href means same
    # underlying document, so collapse rather than double-download.
    seen_hrefs = set()
    deduped = []
    for d in docs:
        key = (d["board"], d["meeting_date"], d["doc_type"], d["href"])
        if key in seen_hrefs:
            continue
        seen_hrefs.add(key)
        deduped.append(d)
    return deduped


# --- (2a) Video: Granicus RSS (City Council + standing committees) ---

def collect_granicus_videos(cutoff, future_limit, board_filter):
    videos = []
    req = urllib.request.Request(GRANICUS_RSS_URL, headers={"User-Agent": UA})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            xml_bytes = r.read()
    except Exception as e:
        print(f"  WARNING: Granicus RSS fetch failed: {e}", file=sys.stderr)
        return videos

    ns = {"gran": "https://www.granicus.com/schema/rss-supplements"}
    try:
        root = ET.fromstring(xml_bytes)
    except ET.ParseError as e:
        print(f"  WARNING: Granicus RSS parse failed: {e}", file=sys.stderr)
        return videos

    for item in root.iter("item"):
        title_el = item.find("title")
        link_el = item.find("link")
        parts_el = item.find("gran:pubDateParts", ns)
        if title_el is None or link_el is None or parts_el is None:
            continue
        try:
            meeting_date = datetime.date(
                int(parts_el.get("yr")), int(parts_el.get("mo")), int(parts_el.get("day"))
            )
        except (TypeError, ValueError):
            continue
        if not (cutoff <= meeting_date <= future_limit):
            continue
        title = (title_el.text or "").strip()
        board = title.split(" - ")[0].strip() if " - " in title else title
        if board_filter and board_filter not in board.lower():
            continue
        clip_m = re.search(r"clip_id=(\d+)", link_el.text or "")
        if not clip_m:
            continue
        videos.append({
            "board": board,
            "meeting_date": meeting_date,
            "extra": title,
            "source": "granicus",
            "ref": clip_m.group(1),
        })
    return videos


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


# --- (2b/2c) Video: YouTube channels ---

def parse_youtube_title(title):
    """Split a YouTube title into (board, meeting_date), or None."""
    title = title.strip()
    m = _YT_TEXT_DATE_RE.search(title)
    if m:
        prefix = title[:m.start()].rstrip(" -")
        try:
            meeting_date = datetime.datetime.strptime(
                f"{m.group(1)} {m.group(2)}, {m.group(3)}", "%B %d, %Y"
            ).date()
        except ValueError:
            return None
    else:
        m = _YT_NUMERIC_DATE_RE.search(title)
        if not m:
            return None
        prefix = title[:m.start()].rstrip(" -")
        month, day, year = int(m.group(1)), int(m.group(2)), int(m.group(3))
        if year < 100:
            year += 2000
        try:
            meeting_date = datetime.date(year, month, day)
        except ValueError:
            return None
    board = re.sub(r"\s+Meeting\s*\d*\s*$", "", prefix, flags=re.IGNORECASE).strip()
    board = re.sub(r"^Somerville\s+", "", board, flags=re.IGNORECASE).strip()
    return board or "Somerville", meeting_date


def collect_youtube_videos(channel_url, cutoff, future_limit, board_filter, playlist_cap=60):
    ytdlp = shutil.which("yt-dlp")
    if not ytdlp:
        print("  WARNING: yt-dlp not found, skipping video", file=sys.stderr)
        return []
    cmd = [ytdlp, "--flat-playlist", "--dump-json", "--playlist-end", str(playlist_cap), channel_url]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
    except subprocess.TimeoutExpired:
        print(f"  WARNING: YouTube listing timed out for {channel_url}", file=sys.stderr)
        return []
    if result.returncode != 0:
        print(f"  WARNING: YouTube listing failed: {result.stderr.strip()[:200]}", file=sys.stderr)
        return []

    videos = []
    for line in result.stdout.splitlines():
        try:
            d = json.loads(line)
        except json.JSONDecodeError:
            continue
        title = d.get("title") or ""
        parsed = parse_youtube_title(title)
        if not parsed:
            continue
        board, meeting_date = parsed
        if board_filter and board_filter not in board.lower():
            continue
        if cutoff <= meeting_date <= future_limit:
            videos.append({
                "board": board,
                "meeting_date": meeting_date,
                "extra": title,
                "source": "youtube",
                "ref": d.get("id"),
            })
    return videos


def download_youtube_video(video_id, dest_path):
    cmd = [
        "yt-dlp",
        "-f", "bestvideo[height<=720]+bestaudio/best[height<=720]",
        "--merge-output-format", "mp4",
        "-o", dest_path,
        "--no-overwrites",
        "--quiet",
        "--no-warnings",
        f"https://www.youtube.com/watch?v={video_id}",
    ]
    try:
        subprocess.run(cmd, check=True, timeout=3600)
        return True
    except subprocess.CalledProcessError as e:
        print(f"  WARNING: yt-dlp failed ({e})", file=sys.stderr)
        return False
    except subprocess.TimeoutExpired:
        print(f"  WARNING: yt-dlp timed out on video {video_id}", file=sys.stderr)
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
    seen = {}
    for item in items:
        key = key_fn(item)
        item["counter"] = seen.get(key, 0)
        seen[key] = item["counter"] + 1


# --- Main ---

def main():
    parser = argparse.ArgumentParser(
        description=(
            "Download Somerville MA municipal agendas, minutes, and video "
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

    docs = []
    if include_docs:
        print("Fetching /meetingdocs...")
        docs = collect_docs(cutoff, future_limit, board_filter)
        print(f"  {len(docs)} document(s).\n")

    videos = []
    if include_video:
        print("Fetching Granicus RSS (City Council + committees)...")
        videos += collect_granicus_videos(cutoff, future_limit, board_filter)
        print("Fetching YouTube (other boards/commissions)...")
        videos += collect_youtube_videos(YT_COMMISSIONS_URL, cutoff, future_limit, board_filter)
        print("Fetching YouTube (School Committee)...")
        videos += collect_youtube_videos(YT_SCHOOL_COMMITTEE_URL, cutoff, future_limit, board_filter)
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
            print(f"{'Board':<35} {'Date':<12} {'Source':<10} Title")
            print("-" * 95)
            for v in videos:
                print(f"{v['board'][:34]:<35} {v['meeting_date']!s:<12} {v['source']:<10} {v['extra'][:40]}")
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
        print(f"  [{v['meeting_date']}] {v['board']} — video ({v['source']} {v['ref']})")
        print(f"  downloading    {label}")
        if v["source"] == "granicus":
            ok = download_granicus_video(v["ref"], dest)
        else:
            ok = download_youtube_video(v["ref"], dest)
        if ok:
            downloaded += 1
            log_lines.append(f"{datetime.datetime.now().isoformat()}  OK       {dest}")
        else:
            failed += 1
            log_lines.append(f"{datetime.datetime.now().isoformat()}  FAILED   {v['source']} {v['ref']}")

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
#    python3 scripts/download-somerville-agendas.py --dry-run
#
# 2. Just School Committee:
#    python3 scripts/download-somerville-agendas.py --board "School Committee"
#
# 3. PDFs only (no video):
#    python3 scripts/download-somerville-agendas.py --no-video
#
# 4. Video only:
#    python3 scripts/download-somerville-agendas.py --video-only
#
# 5. Change the lookback window:
#    python3 scripts/download-somerville-agendas.py --days 14
#
# 6. Run on a schedule (cron — evening):
#    0 19 * * 1-5 cd /path/to/repo && python3 scripts/download-somerville-agendas.py
#
# COVERAGE: Documents cover every board posting through /meetingdocs,
# including School Committee (Board of Education) — a native part of the
# same system, no separate scrape needed. Recreation Commission (the city's
# Parks & Recreation-equivalent public board, distinct from the staff-only
# Parks and Recreation department) exists but has posted nothing there in
# over a year — treated as dormant, not sourced elsewhere, and will be
# picked up automatically if/when it resumes. Video is split three ways —
# see VIDEO NOTE above for the full breakdown and the reasoning behind each
# source's download method.
