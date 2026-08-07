#!/usr/bin/env python3
# download-sudbury-agendas.py
# Download municipal meeting agendas, minutes, and video recordings from
# Sudbury MA for meetings whose date falls within the past N days (and up
# to 7 days ahead).
#
# USAGE:
#   python3 scripts/download-sudbury-agendas.py [options]
#
# REQUIREMENTS:
#   - Python 3.6+  (no third-party packages needed for docs or video)
#   - yt-dlp       (for video: pip install yt-dlp)
#   - Internet connection
#
# WHAT IT DOES:
#   Documents (default or --docs-only):
#     1. For each board (a fixed list below — see SITE STRUCTURE), queries
#        that board's own WordPress REST API for meetings in the date
#        window
#     2. Fetches each matching meeting's live page and scrapes its
#        "Attachments" section for the real agenda/minutes/supporting-
#        materials PDF links
#     3. Downloads PDFs to beat-archive/sudbury-agendas/YYYY-MM/
#
#   Video (--include-video or --video-only):
#     4. For each board with a matching SudburyTV playlist, queries the
#        Castus VOD platform's playlist API directly (see VIDEO NOTE) and
#        downloads matching recordings via yt-dlp against a direct
#        CloudFront HLS URL
#     5. Appends a download log to beat-archive/sudbury-agendas/download-log.txt
#
# SITE STRUCTURE — Sudbury is a WordPress multisite network: every board
# has its own subsite (e.g. sudbury.ma.us/selectboard/,
# sudbury.ma.us/schcomm/), each running a "meeting" custom post type with
# its own REST API:
#   GET https://sudbury.ma.us/{slug}/wp-json/wp/v2/meeting
#         ?after=YYYY-MM-DDT00:00:00&before=YYYY-MM-DDT00:00:00
#         &orderby=date&order=desc
#     -> [{id, date (meeting date/time, ISO), link, title, ...}, ...]
#   The REST API itself exposes no document/content fields (checked
#   directly — no "content", no ACF fields, no working /wp-json/acf/v3/
#   route). Each meeting's own page HTML has to be fetched separately for
#   its actual attachments:
#     <h2 id="attachments">Attachments</h2>
#     <div class="attachments-attachment row">
#       <a class="attachment_title" href="{PDF URL}">{title}</a>
#       ...
#   PDF URLs are on a separate cdn.sudbury.ma.us subdomain. Titles follow
#   "{Board} {Mon}/{Day}/{Year} {Type}" (Type = Agenda, Supporting
#   Materials, etc.) — the doc_type used for filenames is whatever follows
#   the date in the title.
#
#   There is no central committee/board directory endpoint — the 41-board
#   list below was assembled from sudbury.ma.us/officials/ (the town's own
#   comprehensive boards-and-committees listing) and verified by checking
#   which subsite slugs actually expose a populated "meeting" REST
#   endpoint.
#
# VIDEO NOTE — SudburyTV's on-demand video (linked from board pages as
# "cloud.castus.tv/vod/sudbury/playlist/...") is a Castus Cloud
# single-page app; the URL format shown in board-page links (e.g.
# "Sudbury Select Board") doesn't match the platform's actual stored
# playlist names and 500s if queried directly. The real API and playlist
# names were found by inspecting the SPA's own network requests (via
# Playwright) rather than guessing:
#   GET https://2kbyogxrg4.execute-api.us-west-2.amazonaws.com/5faaf9b3935c930007a3045b/home/playlists
#     -> [{_id, name, date}, ...]   (63 playlists total, e.g. "Select
#         Board", "SPS School Committee", "Park and Recreation Commission")
#   GET https://imd0mxanj2.execute-api.us-west-2.amazonaws.com/playlist/sudbury/{URL-encoded playlist name}
#     -> {"response": {"payload": [{_id, metadata: {date, title, ...},
#          watchPrice: 0, downloadPrice: 0, access: "open", ...}, ...]}}
#     watchPrice/downloadPrice are both 0 and access is "open" for every
#     video checked — genuinely free, not paywalled (unlike Wayland MA's
#     dead Castus link or WayCAM's paid-membership transition).
#   Each video's actual stream, found the same way (network inspection),
#   is a direct unauthenticated CloudFront HLS URL built from the video's
#   _id — no Playwright needed at routine download time, just yt-dlp
#   against:
#     https://dlttx48mxf9m3.cloudfront.net/outputs/{_id}/Default/HLS/out.m3u8
#
#   Not every document board has a matching SudburyTV playlist (e.g. Board
#   of Assessors, Cable Advisor, Town Moderator don't) — those get
#   documents only, same as several other towns' minor boards in this
#   repo. School Committee's video lives under the playlist name "SPS
#   School Committee" (Sudbury Public Schools) rather than matching its
#   document-side name exactly.

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

YT_DLP_NODE = "node:/home/richkirby/.local/bin/yt-dlp-node"  # yt-dlp needs Node 22+; symlink kept current by scripts/update-yt-dlp-node.sh

# --- Configuration ---
BASE_URL = "https://sudbury.ma.us"
CASTUS_PLAYLIST_API = "https://imd0mxanj2.execute-api.us-west-2.amazonaws.com/playlist/sudbury/{name}"
CASTUS_HLS_URL = "https://dlttx48mxf9m3.cloudfront.net/outputs/{video_id}/Default/HLS/out.m3u8"
OUTPUT_DIR = "beat-archive/sudbury-agendas"
DAYS_BACK = 4
DAYS_AHEAD = 7
DELAY_SECONDS = 0.5

UA = "Mozilla/5.0 (X11; Linux x86_64; rv:120.0) Gecko/20100101 Firefox/120.0"

# Board display name -> (WordPress subsite slug, SudburyTV playlist name
# or None if no matching playlist exists). Assembled from
# sudbury.ma.us/officials/ and cross-checked against both the WordPress
# "meeting" REST endpoint (documents) and the Castus playlists endpoint
# (video) directly.
BOARDS = {
    "Select Board": ("selectboard", "Select Board"),
    "School Committee": ("schcomm", "SPS School Committee"),
    "Park and Recreation Commission": ("parkrecreationcommission", "Park and Recreation Commission"),
    "Lincoln-Sudbury Regional High School Committee": ("lsschoolcomm", "Lincoln-Sudbury School Committee"),
    "Planning Board": ("planning", "Planning Board"),
    "Zoning Board of Appeals": ("boardofappeals", "Zoning Board of Appeals"),
    "Conservation Commission": ("conservationcommission", "Conservation Commission"),
    "Board of Health": ("boardofhealth", "Board of Health"),
    "Board of Assessors": ("boardofassessors", None),
    "Board of Registrars": ("registrars", None),
    "Finance Committee": ("financecommittee", "Finance Committee"),
    "Historical Commission": ("historicalcommission", "Historical Commission"),
    "Historic Districts Commission": ("historicdistricts", "Historic Districts Commission"),
    "Commission on Disability": ("disability", "Commission on Disability"),
    "Sudbury Housing Authority": ("housingauthority", "Sudbury Housing Authority"),
    "Sudbury Housing Trust": ("housingtrust", "Sudbury Housing Trust"),
    "Rail Trails Advisory Committee": ("bfrt", "Rail Trails Advisory Committee"),
    "Sudbury 250 Committee": ("sudbury250", "Sudbury 250 Committee"),
    "Sudbury Transportation Committee": ("transportation", "Sudbury Transportation Committee"),
    "Energy and Sustainability Committee": ("energy", "Energy and Sustainability Committee"),
    "Diversity, Equity and Inclusion Commission": ("dei", "Diversity, Equity and Inclusion Commission"),
    "Council on Aging": ("councilonaging", "Council on Aging"),
    "Goodnow Library Trustees": ("librarytrustees", "Goodnow Library Trustees"),
    "Permanent Building Committee": ("pbc", "Permanent Building Committee"),
    "Capital Improvement Advisory Committee": ("capitalimprovement", "Capital Improvement Advisory Committee"),
    "Community Preservation Committee": ("cpc", "Community Preservation Committee"),
    "Earth Removal Board": ("earthremoval", "Earth Removal Board"),
    "Land Acquisition Review Committee": ("larc", "Land Acquisition Review Committee"),
    "Liberty Ledge / Sewataro Advisory Committee": ("sewataroll", "Liberty Ledge / Sewataro Advisory Committee"),
    "Agricultural Commission": ("agricultural", None),
    "Cable Advisor": ("cableadvisor", None),
    "Cultural Council": ("culturalcouncil", None),
    "Design Review Board": ("designreviewboard", None),
    "Local Emergency Planning Committee": ("lepc", None),
    "Community Emergency Response Team": ("cert", None),
    "Medical Reserve Corps Executive Committee": ("mrcec", None),
    "Memorial Day Committee": ("memorialday", None),
    "Ponds and Waterways Committee": ("pwc", None),
    "Route 20 Sewer Steering Committee": ("rte20sewer", None),
    "September 11 Memorial Garden Oversight Committee": ("memorialgarden", None),
    "Town Moderator": ("moderator", None),
    "Traffic Safety Coordinating Committee": ("trafficsafety", None),
}

_ATTACHMENT_RE = re.compile(
    r'class="attachment_title"[^>]*href="([^"]+)"[^>]*>(.*?)</a>', re.DOTALL
)
# Attachment titles aren't consistently ordered across boards — e.g.
# "Select Board Jul/14/2026 Agenda" (type after a Mon/Day/Year date) vs.
# "SB Packet 8-11-26" (type before an M-D-YY date, abbreviated board
# name). Rather than assume an order, strip whatever date pattern is
# present and use what's left (minus a leading board-name match) as
# doc_type.
_TITLE_DATE_ANY_RE = re.compile(
    r"(?:[A-Za-z]{3}/\d{1,2}/\d{4}|\d{1,2}[/-]\d{1,2}[/-]\d{2,4})"
)


# --- HTTP helpers ---

def fetch_json(url):
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return json.loads(r.read().decode("utf-8", errors="replace"))
    except Exception as e:
        print(f"  WARNING: fetch failed for {url}: {e}", file=sys.stderr)
        return None


def fetch_html(url):
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "text/html,*/*"})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            charset = r.headers.get_content_charset() or "utf-8"
            return r.read().decode(charset, errors="replace")
    except Exception as e:
        print(f"  WARNING: fetch failed for {url}: {e}", file=sys.stderr)
        return None


def download_file(url, dest_path):
    safe_url = urllib.parse.quote(url, safe=":/?&=%")
    req = urllib.request.Request(safe_url, headers={"User-Agent": UA, "Accept": "*/*"})
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            with open(dest_path, "wb") as f:
                f.write(r.read())
        return True
    except Exception as e:
        print(f"  WARNING: {e}", file=sys.stderr)
        return False


# --- Documents ---

def collect_board_docs(board, slug, cutoff, future_limit):
    docs = []
    start = cutoff.strftime("%Y-%m-%dT00:00:00")
    end = (future_limit + datetime.timedelta(days=1)).strftime("%Y-%m-%dT00:00:00")
    params = urllib.parse.urlencode({
        "after": start, "before": end, "orderby": "date", "order": "desc", "per_page": "50",
    })
    meetings = fetch_json(f"{BASE_URL}/{slug}/wp-json/wp/v2/meeting?{params}")
    if not meetings:
        return docs

    for m in meetings:
        date_str = (m.get("date") or "")[:10]
        try:
            meeting_date = datetime.date.fromisoformat(date_str)
        except ValueError:
            continue
        if not (cutoff <= meeting_date <= future_limit):
            continue
        link = m.get("link")
        if not link:
            continue
        html = fetch_html(link)
        if not html:
            continue
        idx = html.find('id="attachments"')
        if idx == -1:
            continue
        for href, inner in _ATTACHMENT_RE.findall(html[idx:]):
            title = re.sub(r"<[^>]+>", "", inner).strip()
            remainder = _TITLE_DATE_ANY_RE.sub("", title).strip(" -/")
            doc_type = remainder if remainder else "document"
            docs.append({
                "board": board, "meeting_date": meeting_date,
                "doc_type": doc_type, "href": href,
            })
        time.sleep(DELAY_SECONDS)
    return docs


# --- Video ---

def collect_board_videos(board, playlist_name, cutoff, future_limit):
    url = CASTUS_PLAYLIST_API.format(name=urllib.parse.quote(playlist_name))
    data = fetch_json(url)
    if not data:
        return []
    payload = (data.get("response") or {}).get("payload") or []
    videos = []
    for item in payload:
        meta = item.get("metadata") or {}
        date_str = meta.get("date") or (item.get("date") or "")[:10]
        try:
            meeting_date = datetime.date.fromisoformat(date_str[:10])
        except ValueError:
            continue
        if not (cutoff <= meeting_date <= future_limit):
            continue
        if item.get("watchPrice") or item.get("downloadPrice"):
            continue  # paid content — skip, don't attempt to bypass
        videos.append({
            "board": board, "meeting_date": meeting_date,
            "video_id": item.get("_id"), "title": meta.get("title") or board,
        })
    return videos


def download_video(video_id, dest_path):
    url = CASTUS_HLS_URL.format(video_id=video_id)
    cmd = [
        "yt-dlp", "--js-runtimes", YT_DLP_NODE,
        "-f", "best",
        "-o", dest_path,
        "--no-overwrites",
        "--quiet",
        "--no-warnings",
        url,
    ]
    try:
        subprocess.run(cmd, check=True, timeout=7200)
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


def make_path(board, doc_type, meeting_date, output_dir, ext=".pdf", counter=0):
    date_str = meeting_date.strftime("%Y-%m-%d")
    month_dir = os.path.join(output_dir, meeting_date.strftime("%Y-%m"))
    os.makedirs(month_dir, exist_ok=True)
    suffix = f"-{counter}" if counter else ""
    fname = f"{date_str}-{slugify(board)}-{slugify(doc_type)}{suffix}{ext}"
    return os.path.join(month_dir, fname)


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
            "Download Sudbury MA municipal agendas, minutes, and video "
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

    boards = BOARDS
    if board_filter:
        boards = {b: v for b, v in BOARDS.items() if board_filter in b.lower()}

    print(f"Date window : {cutoff} to {future_limit}")
    if not args.dry_run:
        print(f"Output dir  : {args.output_dir}")
    print()

    docs = []
    if include_docs:
        print(f"Checking {len(boards)} board(s) for documents...")
        for board, (slug, _playlist) in boards.items():
            docs += collect_board_docs(board, slug, cutoff, future_limit)
        print(f"  {len(docs)} document(s).\n")

    videos = []
    if include_video:
        video_boards = {b: v[1] for b, v in boards.items() if v[1]}
        print(f"Checking {len(video_boards)} board(s) with a known SudburyTV playlist...")
        for board, playlist_name in video_boards.items():
            videos += collect_board_videos(board, playlist_name, cutoff, future_limit)
            time.sleep(DELAY_SECONDS)
        print(f"  {len(videos)} video(s).\n")

    docs.sort(key=lambda x: (x["meeting_date"], x["board"]), reverse=True)
    videos.sort(key=lambda x: (x["meeting_date"], x["board"]), reverse=True)
    assign_counters(docs, lambda d: (d["board"], d["meeting_date"], d["doc_type"]))
    assign_counters(videos, lambda v: (v["board"], v["meeting_date"]))

    total = len(docs) + len(videos)
    print(f"{total} item(s) total in window.\n")

    if args.dry_run:
        if docs:
            print(f"{'Board':<40} {'Date':<12} Type")
            print("-" * 65)
            for d in docs:
                print(f"{d['board'][:39]:<40} {d['meeting_date']!s:<12} {d['doc_type']}")
            print()
        if videos:
            print(f"{'Board':<40} {'Date':<12} Video")
            print("-" * 65)
            for v in videos:
                print(f"{v['board'][:39]:<40} {v['meeting_date']!s:<12} {v['title']}")
            print()
        print(f"{total} item(s). Re-run without --dry-run to download.")
        return

    os.makedirs(args.output_dir, exist_ok=True)
    log_path = os.path.join(args.output_dir, "download-log.txt")
    log_lines = []
    downloaded = skipped = failed = 0

    for d in docs:
        dest = make_path(d["board"], d["doc_type"], d["meeting_date"], args.output_dir, counter=d["counter"])
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
        time.sleep(DELAY_SECONDS)

    for v in videos:
        dest = make_path(v["board"], "video", v["meeting_date"], args.output_dir, ext=".mp4", counter=v["counter"])
        label = os.path.basename(dest)
        if os.path.exists(dest):
            print(f"  skip (exists)  {label}")
            skipped += 1
            continue
        print(f"  [{v['meeting_date']}] {v['board']} — video ({v['title']})")
        print(f"  downloading    {label}")
        if download_video(v["video_id"], dest):
            downloaded += 1
            log_lines.append(f"{datetime.datetime.now().isoformat()}  OK       {dest}")
        else:
            failed += 1
            log_lines.append(f"{datetime.datetime.now().isoformat()}  FAILED   video {v['video_id']}")

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
#    python3 scripts/download-sudbury-agendas.py --dry-run
#
# 2. Just School Committee:
#    python3 scripts/download-sudbury-agendas.py --board "School Committee"
#
# 3. PDFs only (no video):
#    python3 scripts/download-sudbury-agendas.py --no-video
#
# 4. Video only:
#    python3 scripts/download-sudbury-agendas.py --video-only
#
# 5. Change the lookback window:
#    python3 scripts/download-sudbury-agendas.py --days 14
#
# 6. Run on a schedule (cron — evening):
#    0 19 * * 1-5 cd /path/to/repo && python3 scripts/download-sudbury-agendas.py
#
# COVERAGE: School Committee (Board of Education) and Park and Recreation
# Commission are both covered for documents (own WordPress subsite) and
# video (SudburyTV playlist) — School Committee's playlist is named "SPS
# School Committee" rather than matching its document-side name.
