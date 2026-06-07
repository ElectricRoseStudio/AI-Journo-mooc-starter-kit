#!/usr/bin/env python3
# download-west-haven-agendas.py
# Download municipal meeting agendas, minutes, and YouTube video recordings
# from West Haven CT AgendaCenter and the city's YouTube channel.
#
# USAGE:
#   python3 scripts/download-west-haven-agendas.py [options]
#
# REQUIREMENTS:
#   - Python 3.6+  (no third-party packages needed for docs)
#   - yt-dlp  (for video:  pip install yt-dlp  or  sudo apt install yt-dlp)
#
# WHAT IT DOES:
#   1. Fetches the West Haven CT AgendaCenter listing page
#   2. Finds all agendas and minutes whose meeting date falls within the
#      lookback window (note: no "posted date" is exposed by this site —
#      meeting date is used instead)
#   3. Downloads them to beat-archive/west-haven-agendas/YYYY-MM/
#   4. Optionally queries the city's YouTube channel for meeting recordings
#      within the same window (dates parsed from video titles) and downloads
#      them via yt-dlp
#   5. Appends a download log to beat-archive/west-haven-agendas/download-log.txt
#
# SITE STRUCTURE:
#   West Haven CT uses CivicPlus AgendaCenter. Board sections are collapsible
#   panels; the current year is pre-loaded in the page HTML. Previous years
#   load via a POST to /AgendaCenter/UpdateCategoryList.
#
#   Document URLs:
#     /AgendaCenter/ViewFile/Agenda/_MMDDYYYY-NNNN   → agenda PDF
#     /AgendaCenter/ViewFile/Minutes/_MMDDYYYY-NNNN  → minutes PDF
#
# VIDEO SOURCE:
#   City of West Haven YouTube channel: UC5cM1trmHe999FXNJ56X_Jg
#   Meeting recordings use title format: "YYYY-MM-DD Board Name Meeting Type"
#   Non-meeting videos have no date prefix and are skipped automatically.

import argparse
import datetime
import glob
import os
import re
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

# --- Configuration ---
BASE_URL          = "https://www.cityofwesthaven.com"
AGENDA_CENTER_URL = f"{BASE_URL}/AgendaCenter"
UPDATE_URL        = f"{BASE_URL}/AgendaCenter/UpdateCategoryList"
OUTPUT_DIR        = "beat-archive/west-haven-agendas"
DAYS_BACK         = 4
DELAY             = 1.0

YOUTUBE_CHANNEL = "https://www.youtube.com/channel/UC5cM1trmHe999FXNJ56X_Jg"

UA = "WestHaven-Agendas-Downloader/1.0 (journalism research)"

_VIDEO_TITLE_RE = re.compile(r'^(20\d{2}-\d{2}-\d{2})\s+(.+)$')


# --- HTTP helpers ---

def fetch_html(url, post_data=None):
    req = urllib.request.Request(
        url,
        data=post_data,
        headers={
            "User-Agent": UA,
            "Content-Type": "application/x-www-form-urlencoded" if post_data else "text/html",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return r.read().decode("utf-8", errors="replace")
    except urllib.error.URLError as e:
        print(f"  ERROR fetching {url}: {e}", file=sys.stderr)
        return None


def download_file(path, dest_path):
    url = BASE_URL + path if path.startswith("/") else path
    url = url.split("?")[0]
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            if r.status != 200:
                print(f"  WARNING: HTTP {r.status} — {url}", file=sys.stderr)
                return False
            with open(dest_path, "wb") as f:
                f.write(r.read())
        return True
    except Exception as e:
        print(f"  WARNING: {e} — {url}", file=sys.stderr)
        return False


# --- AgendaCenter HTML parsing ---

def parse_boards(html):
    pattern = r'aria-controls="category-panel-(\d+)"[^>]*>\s*([^<]+)\s*</h2>'
    return [(cat_id, name.strip()) for cat_id, name in re.findall(pattern, html)]


def _parse_rows_html(html):
    rows = re.findall(r'<tr[^>]+class="catAgendaRow"[^>]*>(.*?)</tr>', html, re.DOTALL)
    items = []
    for row in rows:
        date_m = re.search(r'aria-label="Agenda for ([^"]+)"', row)
        if not date_m:
            continue
        try:
            meeting_date = datetime.datetime.strptime(date_m.group(1), "%B %d, %Y").date()
        except ValueError:
            try:
                meeting_date = datetime.datetime.strptime(date_m.group(1), "%B %-d, %Y").date()
            except ValueError:
                continue
        agenda_m  = re.search(r'href="(/AgendaCenter/ViewFile/Agenda/[^"?]+)', row)
        minutes_m = re.search(r'href="(/AgendaCenter/ViewFile/Minutes/[^"?]+)', row)
        title_m   = re.search(r'<p[^>]*>.*?<a[^>]+>\s*([^<]+)\s*</a>', row, re.DOTALL)
        items.append({
            "date":        meeting_date,
            "agenda_url":  agenda_m.group(1)  if agenda_m  else None,
            "minutes_url": minutes_m.group(1) if minutes_m else None,
            "title":       title_m.group(1).strip() if title_m else "",
        })
    return items


def parse_rows(html, cat_id):
    panel_start = html.find(f'id="category-panel-{cat_id}"')
    if panel_start < 0:
        return []
    next_panel = html.find('id="category-panel-', panel_start + 1)
    chunk = html[panel_start: next_panel if next_panel > 0 else len(html)]
    return _parse_rows_html(chunk)


# --- Utilities ---

def slugify(text, max_len=60):
    text = text.lower().strip()
    text = re.sub(r"[/\\]", "-", text)
    text = re.sub(r"\s+-\s+", "-", text)
    text = re.sub(r"[^\w\s-]", "", text)
    text = re.sub(r"[\s_]+", "-", text)
    text = re.sub(r"-{2,}", "-", text)
    return text.strip("-")[:max_len]


def month_dir(date, output_dir):
    path = os.path.join(output_dir, date.strftime("%Y-%m"))
    os.makedirs(path, exist_ok=True)
    return path


def make_doc_dest(board_name, doc_type, meeting_date, output_dir):
    d        = month_dir(meeting_date, output_dir)
    date_str = meeting_date.strftime("%Y-%m-%d")
    board    = slugify(board_name)
    return os.path.join(d, f"{date_str}-{board}-{doc_type}.pdf")


def video_dest_template(date, title_rest, output_dir):
    d        = month_dir(date, output_dir)
    date_str = date.strftime("%Y-%m-%d")
    slug     = slugify(title_rest, max_len=60)
    return os.path.join(d, f"{date_str}-{slug}.%(ext)s")


def video_already_exists(dest_template):
    base = dest_template.replace(".%(ext)s", "")
    return bool(glob.glob(base + ".*"))


# --- yt-dlp helpers ---

def _ytdlp_available():
    try:
        r = subprocess.run(["yt-dlp", "--version"], capture_output=True, timeout=5)
        return r.returncode == 0
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return False


def list_channel_videos(channel_url):
    """Return list of (video_id, date, title_rest) for all dated meeting videos."""
    cmd = [
        "yt-dlp", "--flat-playlist",
        "--print", "%(id)s\t%(title)s",
        "--no-warnings", channel_url,
    ]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
    except subprocess.TimeoutExpired:
        print("  WARNING: yt-dlp channel listing timed out.", file=sys.stderr)
        return []
    videos = []
    for line in result.stdout.splitlines():
        parts = line.split("\t", 1)
        if len(parts) != 2:
            continue
        vid_id, title = parts[0].strip(), parts[1].strip()
        m = _VIDEO_TITLE_RE.match(title)
        if not m:
            continue
        try:
            vdate = datetime.date.fromisoformat(m.group(1))
        except ValueError:
            continue
        videos.append((vid_id, vdate, m.group(2).strip()))
    return videos


def download_youtube_video(video_id, dest_template, dry_run=False):
    url = f"https://www.youtube.com/watch?v={video_id}"
    if dry_run:
        print(f"    [dry-run] would download: {url}")
        return True
    cmd = [
        "yt-dlp", "--no-playlist",
        "-f", "bestvideo+bestaudio/best",
        "--merge-output-format", "mp4",
        "-o", dest_template,
        "--no-overwrites",
        "--quiet", "--no-warnings",
        url,
    ]
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=7200)
    if result.returncode != 0 and result.stderr:
        print(f"  WARNING: yt-dlp: {result.stderr[:300]}", file=sys.stderr)
    return result.returncode == 0


# --- Main ---

def main():
    parser = argparse.ArgumentParser(
        description=(
            "Download West Haven CT municipal agendas, minutes, and YouTube "
            "meeting recordings for meetings in the past N days."
        )
    )
    parser.add_argument("--days", type=int, default=DAYS_BACK, metavar="N",
                        help=f"Look back N days (default: {DAYS_BACK})")
    parser.add_argument("--output-dir", default=OUTPUT_DIR, metavar="DIR",
                        help=f"Destination directory (default: {OUTPUT_DIR})")
    parser.add_argument("--dry-run", action="store_true",
                        help="List matching items without downloading")
    parser.add_argument("--board", metavar="NAME",
                        help="Only process boards whose name contains NAME (case-insensitive)")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--include-video", action="store_true",
                      help="Download both documents and YouTube recordings")
    mode.add_argument("--video-only", action="store_true",
                      help="Download YouTube recordings only (skip PDFs)")
    mode.add_argument("--docs-only", action="store_true",
                      help="Download PDFs only (skip video)")
    args = parser.parse_args()

    do_docs  = not args.video_only
    do_video = args.include_video or args.video_only

    today   = datetime.date.today()
    cutoff  = today - datetime.timedelta(days=args.days)
    has_ytdlp = _ytdlp_available()

    years_needed = {today.year}
    if cutoff.year != today.year:
        years_needed.add(cutoff.year)

    print(f"Date window : {cutoff} to {today}  ({args.days} days back)")
    print(f"AgendaCenter: {AGENDA_CENTER_URL}")
    if do_video:
        print(f"Video       : enabled (YouTube / yt-dlp"
              f"{'  *** NOT FOUND ***' if not has_ytdlp else ''})")
    if not args.dry_run:
        print(f"Output dir  : {args.output_dir}")
    print()

    log_lines = []
    dl_ok = dl_skip = dl_fail = 0
    vd_ok = vd_skip = vd_fail = 0

    # --- Documents ---
    if do_docs:
        print("Fetching AgendaCenter index...")
        main_html = fetch_html(AGENDA_CENTER_URL)
        if not main_html:
            print("ERROR: Could not fetch AgendaCenter page.", file=sys.stderr)
            sys.exit(1)

        boards = parse_boards(main_html)
        if not boards:
            print("ERROR: No boards found — page structure may have changed.", file=sys.stderr)
            sys.exit(1)
        print(f"Found {len(boards)} board(s).")

        if args.board:
            boards = [(cid, name) for cid, name in boards if args.board.lower() in name.lower()]
            print(f"Filtered to {len(boards)} board(s) matching '{args.board}'.")
        print()

        matches = []
        for cat_id, board_name in boards:
            rows = parse_rows(main_html, cat_id)
            if len(years_needed) > 1:
                prior_year = min(years_needed)
                post_data  = urllib.parse.urlencode(
                    {"year": prior_year, "catID": cat_id}
                ).encode()
                prior_html = fetch_html(UPDATE_URL, post_data=post_data)
                if prior_html:
                    rows += _parse_rows_html(prior_html)
                time.sleep(0.2)

            for row in rows:
                if row["date"] < cutoff or not row["agenda_url"]:
                    continue
                matches.append({
                    "board":       board_name,
                    "date":        row["date"],
                    "title":       row["title"],
                    "agenda_url":  row["agenda_url"],
                    "minutes_url": row["minutes_url"],
                })

        matches.sort(key=lambda x: (x["date"], x["board"]), reverse=True)
        total_docs = sum(1 + bool(m["minutes_url"]) for m in matches)
        print(f"Found {len(matches)} meeting(s) with up to {total_docs} document(s) in window.")
        print()

        if args.dry_run:
            print(f"{'Board':<45} {'Date':<12} Docs")
            print("-" * 70)
            for m in matches:
                docs = ["agenda"] + (["minutes"] if m["minutes_url"] else [])
                print(f"{m['board'][:44]:<45} {m['date']!s:<12} {', '.join(docs)}")
            print()
        else:
            os.makedirs(args.output_dir, exist_ok=True)
            for m in matches:
                print(f"[{m['date']}] {m['board']}")
                for doc_type, url in (("agenda", m["agenda_url"]), ("minutes", m["minutes_url"])):
                    if not url:
                        continue
                    dest  = make_doc_dest(m["board"], doc_type, m["date"], args.output_dir)
                    label = os.path.basename(dest)
                    if os.path.exists(dest):
                        print(f"  skip (exists)  {label}")
                        dl_skip += 1
                        continue
                    print(f"  downloading    {label}")
                    if download_file(url, dest):
                        dl_ok += 1
                        log_lines.append(
                            f"{datetime.datetime.now().isoformat()}  OK       {dest}"
                        )
                    else:
                        dl_fail += 1
                        log_lines.append(
                            f"{datetime.datetime.now().isoformat()}  FAILED   {BASE_URL + url}"
                        )
                        if os.path.exists(dest):
                            os.remove(dest)
                    time.sleep(DELAY)

    # --- YouTube video recordings ---
    if do_video:
        if not has_ytdlp and not args.dry_run:
            print("WARNING: yt-dlp not found — skipping video.", file=sys.stderr)
            print("  Install with:  pip install yt-dlp  or  sudo apt install yt-dlp",
                  file=sys.stderr)
        else:
            print("Fetching YouTube channel listing...")
            all_videos = list_channel_videos(YOUTUBE_CHANNEL)
            video_matches = [
                (vid_id, vdate, title_rest)
                for vid_id, vdate, title_rest in all_videos
                if cutoff <= vdate <= today
            ]
            if args.board:
                video_matches = [
                    v for v in video_matches
                    if args.board.lower() in v[2].lower()
                ]
            video_matches.sort(key=lambda x: x[1], reverse=True)
            print(f"Found {len(video_matches)} meeting recording(s) in window.")
            print()

            for vid_id, vdate, title_rest in video_matches:
                tmpl   = video_dest_template(vdate, title_rest, args.output_dir)
                header = f"  [{vdate}] {title_rest}"

                if args.dry_run:
                    print(header)
                    print(f"    {os.path.basename(tmpl)}")
                    print(f"    https://www.youtube.com/watch?v={vid_id}")
                    continue

                print(header)
                if video_already_exists(tmpl):
                    existing = glob.glob(tmpl.replace(".%(ext)s", ".*"))[0]
                    print(f"    skip (exists)  {os.path.basename(existing)}")
                    vd_skip += 1
                    continue

                print(f"    downloading    {os.path.basename(tmpl)}")
                os.makedirs(os.path.dirname(tmpl), exist_ok=True)
                if download_youtube_video(vid_id, tmpl):
                    vd_ok += 1
                    log_lines.append(
                        f"{datetime.datetime.now().isoformat()}  OK       "
                        f"https://www.youtube.com/watch?v={vid_id}  {title_rest}"
                    )
                else:
                    vd_fail += 1
                    log_lines.append(
                        f"{datetime.datetime.now().isoformat()}  FAILED   "
                        f"https://www.youtube.com/watch?v={vid_id}  {title_rest}"
                    )

    if not args.dry_run:
        if log_lines:
            log_path = os.path.join(args.output_dir, "download-log.txt")
            os.makedirs(args.output_dir, exist_ok=True)
            with open(log_path, "a") as f:
                f.write("\n".join(log_lines) + "\n")
        if do_docs:
            print(f"Documents  — downloaded: {dl_ok}  skipped: {dl_skip}  failed: {dl_fail}")
        if do_video:
            print(f"Video      — downloaded: {vd_ok}  skipped: {vd_skip}  failed: {vd_fail}")
        if dl_ok + dl_skip + vd_ok + vd_skip:
            print(f"Files in: {args.output_dir}")
        if log_lines:
            print(f"Log:      {os.path.join(args.output_dir, 'download-log.txt')}")
    elif args.dry_run:
        print("Re-run without --dry-run to download.")


if __name__ == "__main__":
    main()


# --- Tips ---
#
# Preview documents (30-day window):
#   python3 scripts/download-west-haven-agendas.py --dry-run
#
# Download documents + YouTube recordings:
#   python3 scripts/download-west-haven-agendas.py --include-video
#
# Video recordings only:
#   python3 scripts/download-west-haven-agendas.py --video-only
#
# Preview video recordings in window:
#   python3 scripts/download-west-haven-agendas.py --video-only --dry-run
#
# Narrow to one board:
#   python3 scripts/download-west-haven-agendas.py --include-video --board "City Council"
#
# Change the lookback window:
#   python3 scripts/download-west-haven-agendas.py --include-video --days 15
#
# Run daily via cron (7 AM):
#   0 7 * * * cd /path/to/repo && python3 scripts/download-west-haven-agendas.py --include-video
#
# NOTE: CivicPlus AgendaCenter exposes meeting dates, not upload/posted dates.
# The documents section filters by meeting date ≥ cutoff (with no upper bound,
# so upcoming meetings already posted appear in results).
#
# NOTE: YouTube video titles begin with "YYYY-MM-DD Board Name Meeting Type".
# Non-meeting videos lack a date prefix and are skipped automatically.
#
# VIDEO SOURCE:
#   City of West Haven YouTube channel
#   https://www.youtube.com/channel/UC5cM1trmHe999FXNJ56X_Jg
