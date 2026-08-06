#!/usr/bin/env python3
# download-medford-agendas.py
# Download municipal meeting agendas, minutes, and video recordings from
# Medford MA for meetings whose date falls within the past N days (and up
# to 7 days ahead).
#
# USAGE:
#   python3 scripts/download-medford-agendas.py [options]
#
# REQUIREMENTS:
#   - Python 3.6+  (no third-party packages needed for docs)
#   - yt-dlp       (for video: pip install yt-dlp)
#   - Internet connection
#
# WHAT IT DOES:
#   1. City Council (+ subcommittees): calls the CivicClerk OData API,
#      same pattern as download-enfield-agendas.py.
#   2. School Committee: scrapes the current-year meeting table on the
#      Medford Public Schools site (a hand-maintained HTML table, not an
#      API — see SCHOOL COMMITTEE NOTE below).
#   3. Video: crawls the Medford Public Schools YouTube "streams" tab for
#      uploads within the date window (see VIDEO NOTE below).
#   4. Appends a download log to beat-archive/medford-agendas/download-log.txt
#
# SITE STRUCTURE — three genuinely separate systems, unlike every other
# town's downloader in this repo:
#
#   (1) City Council — CivicClerk (same platform as Enfield CT):
#     Public portal: https://medfordma.portal.civicclerk.com
#     OData API:     https://medfordma.api.civicclerk.com/v1
#     GET /Events?$filter=eventDate ge {ISO}Z and eventDate le {ISO}Z
#         &$orderby=eventDate asc
#     Confirmed via direct query: this CivicClerk instance contains ONLY
#     City Council and its four subcommittees (Committee of the Whole,
#     Planning & Permitting, Public Health & Safety, Res Services & Pub
#     Engagement) — checked a full year of events, zero results for any
#     other board. School Committee and Parks Commission are NOT here.
#     Document download: GET /v1/Meetings/GetMeetingFileStream(fileId={id},plainText=false)
#
#   (2) School Committee — Medford Public Schools site (mps02155.org),
#       NOT CivicClerk, NOT the city's own domain:
#     https://www.mps02155.org/about/school-committee/meetings
#     A hand-maintained HTML table (Date | Purpose | Materials) covering
#     the current school year. Each row's Materials cell holds a "News
#     Post" link (an article page, not fetched by this script) and,
#     once posted, a PDF link:
#       /fs/resource-manager/view/{uuid}
#     which 302-redirects to a Cloudinary-hosted PDF (resources.finalsite.net),
#     no authentication required (confirmed via curl). There is no API, no
#     stable per-meeting ID, and no minutes/agenda type distinction beyond
#     what's in the free-text "Purpose" column — this script downloads
#     whatever single PDF is linked per meeting as doc_type "materials".
#     Older years live on separate archive pages or in Google Drive folders
#     (not scraped — out of scope for the daily 4-back/7-ahead window this
#     script runs on; only the live current-year table is fetched).
#
#   (3) Parks Commission — Medford's only Parks & Recreation-equivalent
#       public board (Recreation is a staff department, not a board) posts
#       to a Google Drive folder that requires sign-in to view:
#         https://drive.google.com/drive/folders/1mb5r4Or8usfqq633Q_4M0WdAQSkgx7rW
#       last modified September 2024 (confirmed via direct check — no
#       automated access is possible without a Google login, and this
#       script will never attempt to script around that. Per an explicit
#       decision on 2026-08-06, this is NOT covered: the daily email notes
#       the gap rather than silently omitting it.
#
# VIDEO NOTE: Unlike Arlington's ACMi (one YouTube playlist per board) or
# Malden's per-meeting video links, Medford School Committee video has no
# per-meeting link at all — the meetings page just points to a general
# "Medford Public Schools" YouTube channel and a separate Medford Community
# Media VOD site (medford.vod.castus.tv), which are NOT scriptable the way
# the channel is. There's also no dedicated "School Committee" playlist —
# only two unrelated ones (Building Project, Vocational Shop Profiles) — so
# this script pulls from the channel's "streams" tab (mixed School
# Committee, building committee, and other MPS content) filtered by upload
# date, the same --dateafter/--playlist-end convention used for Arlington
# and Ridgefield. Titles are NOT forced into a board label — they're kept
# as-is since attribution from the raw title alone isn't reliable.
# City Council has no video source at all (not linked anywhere in
# CivicClerk's event data or on the city site) — PDFs only for Council.

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

YT_DLP_NODE = "node:/home/richkirby/.local/bin/yt-dlp-node"  # yt-dlp needs Node 22+; symlink kept current by scripts/update-yt-dlp-node.sh

# --- Configuration ---
CIVICCLERK_API = "https://medfordma.api.civicclerk.com/v1"
MPS_BASE = "https://www.mps02155.org"
MPS_MEETINGS_URL = f"{MPS_BASE}/about/school-committee/meetings"
YOUTUBE_STREAMS_URL = "https://www.youtube.com/@medfordpublicschools464/streams"
OUTPUT_DIR = "beat-archive/medford-agendas"
DAYS_BACK = 4
DAYS_AHEAD = 7
API_DELAY = 0.25

UA = "Mozilla/5.0 (X11; Linux x86_64; rv:120.0) Gecko/20100101 Firefox/120.0"

DOWNLOAD_TYPES = {"Agenda", "Agenda Packet", "Minutes", "Notice"}

_TABLE_DATE_RE = re.compile(
    r"(January|February|March|April|May|June|July|August|September|"
    r"October|November|December)\s+(\d{1,2}),\s+(20\d{2})"
)


# --- Shared HTTP helpers ---

def fetch_json(url):
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.load(r)


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
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "*/*"})
    try:
        with urllib.request.urlopen(req, timeout=120) as r:
            with open(dest_path, "wb") as f:
                f.write(r.read())
        return True
    except Exception as e:
        print(f"  WARNING: {e}", file=sys.stderr)
        return False


# --- Utilities ---

def slugify(text, max_len=55):
    text = str(text).lower().strip()
    text = re.sub(r"[/\\&]", "-", text)
    text = re.sub(r"[^\w\s-]", "", text)
    text = re.sub(r"[\s_]+", "-", text)
    text = re.sub(r"-{2,}", "-", text)
    return text.strip("-")[:max_len]


def make_path(board, doc_type, meeting_date, extra, output_dir, ext=".pdf"):
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
    fname = "-".join(parts) + ext
    return os.path.join(month_dir, fname)


def is_in_archive(archive_path, key):
    if not os.path.exists(archive_path):
        return False
    needle = str(key)
    with open(archive_path) as f:
        return any(needle == line.strip() for line in f)


def add_to_archive(archive_path, key):
    with open(archive_path, "a") as f:
        f.write(f"{key}\n")


# --- (1) CivicClerk: City Council ---

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


def collect_civicclerk_docs(cutoff, future_limit, board_filter, no_minutes, no_agendas):
    docs = []
    try:
        events = fetch_civicclerk_events(cutoff, future_limit)
    except Exception as e:
        print(f"  WARNING: CivicClerk fetch failed: {e}", file=sys.stderr)
        return docs

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
                "source": "civicclerk",
                "board": board,
                "meeting_date": event_date,
                "extra": event_name,
                "doc_type": doc_type,
                "href": make_civicclerk_doc_url(file_id),
            })
    return docs


# --- (2) School Committee: mps02155.org meeting table ---

def collect_school_committee_docs(cutoff, future_limit, board_filter, no_agendas):
    docs = []
    if board_filter and "school" not in board_filter and "committee" not in board_filter:
        return docs

    html = fetch_html(MPS_MEETINGS_URL)
    if not html:
        return docs
    if no_agendas:
        # The single Materials-column PDF is the only document type this
        # source has — "agendas only"/"minutes only" don't apply distinctly,
        # so --no-agendas suppresses School Committee docs entirely.
        return docs

    # Only the first table (current school year) is live/operational data;
    # the second table on the page is a list of links to archived years.
    tables = re.findall(r"<table.*?</table>", html, re.DOTALL)
    if not tables:
        return docs
    rows = re.findall(r"<tr>(.*?)</tr>", tables[0], re.DOTALL)

    for row in rows:
        cells = re.findall(r"<td.*?>(.*?)</td>", row, re.DOTALL)
        if len(cells) < 3:
            continue
        date_cell, purpose_cell, materials_cell = cells[0], cells[1], cells[2]

        m = _TABLE_DATE_RE.search(re.sub(r"<[^>]+>", " ", date_cell))
        if not m:
            continue
        try:
            meeting_date = datetime.datetime.strptime(
                f"{m.group(1)} {m.group(2)}, {m.group(3)}", "%B %d, %Y"
            ).date()
        except ValueError:
            continue
        if not (cutoff <= meeting_date <= future_limit):
            continue

        pdf_m = re.search(r'href="(/fs/resource-manager/view/[a-f0-9-]+)"', materials_cell)
        if not pdf_m:
            continue  # future meeting with nothing posted yet

        purpose = re.sub(r"<[^>]+>", " ", purpose_cell)
        purpose = re.sub(r"\s+", " ", purpose).strip() or "Meeting"

        docs.append({
            "source": "mps",
            "board": "School Committee",
            "meeting_date": meeting_date,
            "extra": purpose,
            "doc_type": "materials",
            "href": MPS_BASE + pdf_m.group(1),
        })
    return docs


# --- (3) Video: Medford Public Schools YouTube "streams" ---

def download_videos(cutoff, output_dir, dry_run):
    ytdlp = shutil.which("yt-dlp")
    if not ytdlp:
        print("  WARNING: yt-dlp not found, skipping video", file=sys.stderr)
        return 0, 0

    date_str = cutoff.strftime("%Y%m%d")
    out_tmpl = os.path.join(output_dir, "%(upload_date)s", "%(upload_date)s-mps-video-%(id)s.%(ext)s")

    deno_path = os.path.expanduser("~/.deno/bin/deno")
    deno_arg = f"deno:{deno_path}" if os.path.exists(deno_path) else "deno"

    cmd = [
        ytdlp,
        "--dateafter", date_str,
        "--break-match-filters", f"upload_date>={date_str}",
        "--playlist-end", "20",
        "--sleep-requests", "0.75",
        "--sleep-interval", "10",
        "--max-sleep-interval", "20",
        "--js-runtimes", deno_arg,
        "--remote-components", "ejs:github",
        "--format", "best[ext=mp4]/best",
        "--output", out_tmpl,
        "--restrict-filenames",
        "--write-info-json",
    ]
    if dry_run:
        cmd += ["--simulate", "--print", "  [dry] MPS video: %(upload_date)s  %(title)s  [%(id)s]"]
    cmd.append(YOUTUBE_STREAMS_URL)

    try:
        result = subprocess.run(cmd, timeout=1800, capture_output=dry_run, text=True)
        if dry_run and result.stdout:
            print(result.stdout, end="")
    except subprocess.TimeoutExpired:
        print("  WARNING: yt-dlp timed out for MPS video — partial file(s) kept", file=sys.stderr)
        return 0, 1
    return (0 if result.returncode == 0 else 1), 0


# --- Main ---

def main():
    parser = argparse.ArgumentParser(
        description=(
            "Download Medford MA municipal agendas, minutes, and video "
            "recordings for meetings within the past N days (and up to 7 ahead). "
            "Parks Commission is not covered — see script header."
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
    parser.add_argument("--no-minutes", action="store_true",
                        help="Skip minutes (CivicClerk only)")
    parser.add_argument("--no-agendas", action="store_true",
                        help="Skip agendas/packets (CivicClerk) and School Committee materials")
    parser.add_argument("--no-video", action="store_true",
                        help="Skip video recordings (PDFs only)")
    parser.add_argument("--docs-only", action="store_true",
                        help="Alias for --no-video")
    args = parser.parse_args()

    now = datetime.datetime.now()
    if (now.weekday() == 5 and now.hour >= 18) or (now.weekday() == 6 and now.hour < 12):
        print("Skipping — no downloads on Saturday nights or Sunday mornings.")
        sys.exit(0)

    include_video = not (args.no_video or args.docs_only)

    today = datetime.date.today()
    cutoff = today - datetime.timedelta(days=args.days)
    future_limit = today + datetime.timedelta(days=args.ahead)
    board_filter = args.board.lower() if args.board else None

    print(f"Date window : {cutoff} to {future_limit}")
    if not args.dry_run:
        print(f"Output dir  : {args.output_dir}")
    print("NOTE: Parks Commission is not covered by this script (login-gated, "
          "stale Google Drive source — see script header).")
    print()

    print("Fetching City Council events (CivicClerk)...")
    docs = collect_civicclerk_docs(cutoff, future_limit, board_filter, args.no_minutes, args.no_agendas)
    print(f"  {len(docs)} document(s).")

    print("Fetching School Committee meeting table (mps02155.org)...")
    sc_docs = collect_school_committee_docs(cutoff, future_limit, board_filter, args.no_agendas)
    print(f"  {len(sc_docs)} document(s).")
    docs += sc_docs
    print()

    docs.sort(key=lambda x: (x["meeting_date"], x["board"]), reverse=True)

    print(f"{len(docs)} document(s) total across {len({d['board'] for d in docs})} board(s) in window.")
    print()

    if args.dry_run:
        if docs:
            print(f"{'Board':<35} {'Date':<12} {'Extra':<28} Type")
            print("-" * 95)
            for d in docs:
                print(f"{d['board'][:34]:<35} {d['meeting_date']!s:<12} {d['extra'][:27]:<28} {d['doc_type']}")
            print()
        if include_video:
            print(f"Video (MPS YouTube streams, uploaded on/after {cutoff}):")
            download_videos(cutoff, args.output_dir, dry_run=True)
        print(f"\n{len(docs)} document(s). Re-run without --dry-run to download.")
        return

    os.makedirs(args.output_dir, exist_ok=True)
    log_path = os.path.join(args.output_dir, "download-log.txt")
    log_lines = []
    downloaded = skipped = failed = 0

    for d in docs:
        dest = make_path(d["board"], d["doc_type"], d["meeting_date"], d["extra"], args.output_dir)
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

    if include_video:
        print("\nChecking MPS YouTube streams for video in window...")
        fail, err = download_videos(cutoff, args.output_dir, dry_run=False)
        failed += fail + err

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
#    python3 scripts/download-medford-agendas.py --dry-run
#
# 2. Just City Council:
#    python3 scripts/download-medford-agendas.py --board "City Council"
#
# 3. Just School Committee:
#    python3 scripts/download-medford-agendas.py --board "School Committee"
#
# 4. PDFs only (no video):
#    python3 scripts/download-medford-agendas.py --no-video
#
# 5. Change the lookback window:
#    python3 scripts/download-medford-agendas.py --days 14
#
# 6. Run on a schedule (cron — evening):
#    0 19 * * 1-5 cd /path/to/repo && python3 scripts/download-medford-agendas.py
#
# COVERAGE: City Council (+ 4 subcommittees) via CivicClerk, School
# Committee via a hand-maintained HTML table on mps02155.org. Parks
# Commission (Medford's only Parks & Rec-equivalent board) is NOT covered —
# its agendas live in a Google Drive folder that requires sign-in and was
# last touched September 2024; see script header for the full explanation
# and the 2026-08-06 decision to skip it rather than script around a login
# wall.
