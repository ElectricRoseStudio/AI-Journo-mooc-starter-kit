#!/usr/bin/env python3
# download-north-haven-agendas.py
# Download municipal meeting agendas, minutes, and Vimeo video recordings
# from North Haven CT for meetings within the past N days (and up to 7 days
# ahead, to catch agendas posted early for upcoming meetings).
#
# USAGE:
#   python3 scripts/download-north-haven-agendas.py [options]
#
# REQUIREMENTS:
#   - Python 3.6+  (no third-party packages needed for docs)
#   - yt-dlp  (for video:  pip install yt-dlp  or  sudo apt install yt-dlp)
#
# WHAT IT DOES:
#   1. Fetches the North Haven Public Meetings index to discover all boards
#      and their year-page URLs
#   2. For each board, fetches the relevant year page(s)
#   3. Parses each meeting row for the date, document links, and Vimeo URLs
#   4. Downloads Agenda and Minutes PDFs whose meeting date falls within
#      the date window to beat-archive/north-haven-agendas/YYYY-MM/
#   5. Optionally downloads Vimeo meeting recordings via yt-dlp
#   6. Appends a download log to beat-archive/north-haven-agendas/download-log.txt
#
# SITE STRUCTURE:
#   North Haven CT uses the Revize CMS (https://www.northhaven-ct.gov/).
#   Boards do not use a third-party agenda platform; instead each board has
#   a dedicated directory with year pages:
#     /government/public_meetings/{board}/index.php  — board home (has year links)
#     /government/public_meetings/{board}/{YYYY}.php — year listing page
#
#   Each year page contains one HTML table per meeting. The first <td> holds
#   the meeting date in MM/DD/YY format. Subsequent columns link to:
#     Agenda  — "Document Center/..." relative URL resolving against site root
#     Minutes — same pattern
#     Video   — https://vimeo.com/{id}?...  (Board of Selectmen, Finance, Education)
#     (Notice, More... are skipped by default)
#
#   All PDFs are served from cms4files.revize.com after a redirect.
#   No authentication is required.
#
# VIDEO SOURCE:
#   Vimeo — three boards post recordings:
#     Board of Selectmen, Board of Finance, Board of Education

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

YT_DLP_NODE = "node:/home/richkirby/.nvm/versions/node/v20.20.2/bin/node"  # yt-dlp needs Node 20+; system node is 18

# --- Configuration ---
BASE_URL   = "https://www.northhaven-ct.gov"
INDEX_URL  = f"{BASE_URL}/government/public_meetings/index.php"
OUTPUT_DIR = "beat-archive/north-haven-agendas"
DAYS_BACK  = 4
DAYS_AHEAD = 7
DELAY      = 1.0

UA = "NorthHaven-Agendas-Downloader/1.0 (journalism research)"

DOC_TYPES = {"agenda", "minutes"}


# --- HTTP helpers ---

def fetch_html(url):
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "text/html"})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return r.read().decode("utf-8", errors="replace")
    except urllib.error.URLError as e:
        print(f"  ERROR fetching {url}: {e}", file=sys.stderr)
        return None


def download_file(url, dest_path):
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            with open(dest_path, "wb") as f:
                f.write(r.read())
        return True
    except Exception as e:
        print(f"  WARNING: {e}", file=sys.stderr)
        return False


# --- HTML parsing ---

def parse_index(html):
    """
    Return (name_for_dir, years_for_dir) dicts from the public meetings index.
    name_for_dir: {board_dir: display_name}
    years_for_dir: {board_dir: [year, ...] sorted descending}
    """
    name_for_dir = {}
    years_for_dir = {}

    for href, _dirpart, name in re.findall(
        r'href="(government/[^"]+/([^/"]+)/index\.php)"[^>]*>([^<]+)</a>',
        html,
    ):
        board_dir = href.rsplit("/", 2)[1]
        name = name.strip()
        if name and board_dir not in ("town_departments", "boards"):
            name_for_dir.setdefault(board_dir, name)

    for href in re.findall(r'href="(government/[^"]+/(\d{4})\.php)"', html):
        url_path, year_str = href
        board_dir = url_path.rsplit("/", 1)[0].rsplit("/", 1)[-1]
        years_for_dir.setdefault(board_dir, set()).add(int(year_str))

    return name_for_dir, {k: sorted(v, reverse=True) for k, v in years_for_dir.items()}


def parse_meeting_rows(html, doc_types):
    """
    Parse meeting tables from a year page.
    Returns list of dicts: {meeting_date, doc_type, url} for PDFs,
    and list of dicts: {meeting_date, vimeo_id} for videos.
    """
    docs   = []
    videos = []

    tables = re.findall(r"<table[^>]*>.*?</table>", html, re.DOTALL | re.IGNORECASE)

    for table in tables:
        first_td = re.search(r"<td[^>]*>(.*?)</td>", table, re.DOTALL | re.IGNORECASE)
        if not first_td:
            continue
        td_text = re.sub(r"<[^>]+>", " ", first_td.group(1))
        td_text = re.sub(r"\s+", " ", td_text).strip()

        date_m = re.match(r"(\d{1,2})/(\d{1,2})/(\d{2})\b", td_text)
        if not date_m:
            continue
        try:
            meeting_date = datetime.date(
                2000 + int(date_m.group(3)),
                int(date_m.group(1)),
                int(date_m.group(2)),
            )
        except ValueError:
            continue

        # PDF document links
        for href, link_text in re.findall(
            r'href="([^"]+\.pdf[^"]*)"[^>]*>([^<]+)</a>', table, re.IGNORECASE
        ):
            link_text = link_text.strip()
            if link_text.lower() not in doc_types:
                continue
            href_clean = href.split("?")[0]
            doc_url = href_clean if href_clean.startswith("http") else f"{BASE_URL}/{href_clean}"
            docs.append({"meeting_date": meeting_date, "doc_type": link_text, "url": doc_url})

        # Vimeo video links
        for vimeo_url in re.findall(r'(https://vimeo\.com/(\d+)[^"]*)', table):
            full_url, vimeo_id = vimeo_url
            videos.append({"meeting_date": meeting_date, "vimeo_id": vimeo_id})

    return docs, videos


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


def make_doc_dest(board_name, doc_type, meeting_date, output_dir, suffix=""):
    d        = month_dir(meeting_date, output_dir)
    date_str = meeting_date.strftime("%Y-%m-%d")
    board    = slugify(board_name, max_len=40)
    dtype    = slugify(doc_type, max_len=10)
    return os.path.join(d, f"{date_str}-{board}-{dtype}{suffix}.pdf")


def video_dest_template(board_name, meeting_date, output_dir):
    d        = month_dir(meeting_date, output_dir)
    date_str = meeting_date.strftime("%Y-%m-%d")
    board    = slugify(board_name, max_len=40)
    return os.path.join(d, f"{date_str}-{board}-video.%(ext)s")


def video_already_exists(dest_template):
    base = dest_template.replace(".%(ext)s", "")
    return bool(glob.glob(base + ".*"))


# --- yt-dlp helpers ---

def _ytdlp_available():
    try:
        r = subprocess.run(["yt-dlp", "--js-runtimes", YT_DLP_NODE, "--version"], capture_output=True, timeout=5)
        return r.returncode == 0
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return False


def download_vimeo_video(vimeo_id, dest_template, dry_run=False):
    url = f"https://vimeo.com/{vimeo_id}"
    if dry_run:
        print(f"    [dry-run] would download: {url}")
        return True
    cmd = [
        "yt-dlp", "--js-runtimes", YT_DLP_NODE,
        "--no-playlist",
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
            "Download North Haven CT municipal agendas, minutes, and Vimeo "
            "meeting recordings for meetings within the past N days."
        )
    )
    parser.add_argument("--days", type=int, default=DAYS_BACK, metavar="N",
                        help=f"Look back N days by meeting date (default: {DAYS_BACK})")
    parser.add_argument("--ahead", type=int, default=DAYS_AHEAD, metavar="N",
                        help=f"Also include meetings up to N days ahead (default: {DAYS_AHEAD})")
    parser.add_argument("--output-dir", default=OUTPUT_DIR, metavar="DIR",
                        help=f"Destination directory (default: {OUTPUT_DIR})")
    parser.add_argument("--dry-run", action="store_true",
                        help="List matching documents/videos without downloading")
    parser.add_argument("--board", metavar="NAME",
                        help="Only process boards whose name contains NAME (case-insensitive)")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--include-video", action="store_true",
                      help="Download both documents and Vimeo recordings")
    mode.add_argument("--video-only", action="store_true",
                      help="Download Vimeo recordings only (skip PDFs)")
    mode.add_argument("--docs-only", action="store_true",
                      help="Download PDFs only (skip video)")
    args = parser.parse_args()

    now = datetime.datetime.now()
    if (now.weekday() == 5 and now.hour >= 18) or (now.weekday() == 6 and now.hour < 12):  # Saturday night, Sunday morning
        print("Skipping — no downloads on Saturday nights or Sunday mornings.")
        sys.exit(0)

    do_docs  = not args.video_only
    do_video = args.include_video or args.video_only

    today        = datetime.date.today()
    cutoff       = today - datetime.timedelta(days=args.days)
    future_limit = today + datetime.timedelta(days=args.ahead)

    years_needed = {today.year}
    if cutoff.year != today.year:
        years_needed.add(cutoff.year)
    if future_limit.year != today.year:
        years_needed.add(future_limit.year)

    has_ytdlp = _ytdlp_available()

    print(f"Date window : {cutoff} to {future_limit}")
    print(f"Site        : {BASE_URL}")
    if do_video:
        print(f"Video       : enabled (Vimeo / yt-dlp"
              f"{'  *** NOT FOUND ***' if not has_ytdlp else ''})")
    if not args.dry_run:
        print(f"Output dir  : {args.output_dir}")
    print()

    # --- Step 1: index ---
    print("Fetching public meetings index...")
    index_html = fetch_html(INDEX_URL)
    if not index_html:
        print("ERROR: Could not fetch the public meetings index.", file=sys.stderr)
        sys.exit(1)

    name_for_dir, years_for_dir = parse_index(index_html)

    boards = []
    for board_dir, available_years in years_for_dir.items():
        board_name = name_for_dir.get(board_dir, board_dir.replace("_", " ").title())
        boards.append({"name": board_name, "dir": board_dir, "years": available_years})
    boards.sort(key=lambda b: b["name"])
    print(f"Found {len(boards)} board(s) with year pages.")

    if args.board:
        boards = [b for b in boards if args.board.lower() in b["name"].lower()]
        print(f"Filtered to {len(boards)} board(s) matching '{args.board}'.")
    print()

    # --- Step 2: collect documents and videos ---
    doc_matches   = []
    video_matches = []  # list of {board, meeting_date, vimeo_id}
    seen_videos   = set()  # deduplicate by vimeo_id

    for board in boards:
        board_name      = board["name"]
        board_dir       = board["dir"]
        available_years = board["years"]

        section_pattern = re.compile(
            rf'href="(government/[^"]+/{re.escape(board_dir)}/(\d{{4}})\.php)"'
        )
        section_match = section_pattern.search(index_html)
        if not section_match:
            continue
        year_url_template = section_match.group(1).rsplit("/", 1)[0]

        fetch_years = [y for y in years_needed if y in available_years]
        if not fetch_years:
            continue

        for year in sorted(fetch_years, reverse=True):
            year_url  = f"{BASE_URL}/{year_url_template}/{year}.php"
            year_html = fetch_html(year_url)
            if not year_html:
                continue

            rows_docs, rows_videos = parse_meeting_rows(year_html, DOC_TYPES)

            if do_docs:
                for row in rows_docs:
                    if cutoff <= row["meeting_date"] <= future_limit:
                        doc_matches.append({
                            "board":        board_name,
                            "meeting_date": row["meeting_date"],
                            "doc_type":     row["doc_type"],
                            "url":          row["url"],
                        })

            if do_video:
                for row in rows_videos:
                    if cutoff <= row["meeting_date"] <= future_limit:
                        if row["vimeo_id"] not in seen_videos:
                            seen_videos.add(row["vimeo_id"])
                            video_matches.append({
                                "board":        board_name,
                                "meeting_date": row["meeting_date"],
                                "vimeo_id":     row["vimeo_id"],
                            })

            time.sleep(0.3)

    # Disambiguate duplicate (board, doc_type, date) filename collisions
    seen_keys: dict = {}
    for m in doc_matches:
        key = (m["board"], m["doc_type"], m["meeting_date"])
        seen_keys[key] = seen_keys.get(key, 0) + 1
    key_ctr: dict = {}
    for m in doc_matches:
        key = (m["board"], m["doc_type"], m["meeting_date"])
        if seen_keys[key] > 1:
            key_ctr[key] = key_ctr.get(key, 0) + 1
            m["suffix"] = f"-{key_ctr[key]}"
        else:
            m["suffix"] = ""

    doc_matches.sort(  key=lambda x: (x["meeting_date"], x["board"]), reverse=True)
    video_matches.sort(key=lambda x: (x["meeting_date"], x["board"]), reverse=True)

    if do_docs:
        print(
            f"Found {len(doc_matches)} document(s) across "
            f"{len({m['board'] for m in doc_matches})} board(s)."
        )
    if do_video:
        print(f"Found {len(video_matches)} video recording(s) in window.")
    print()

    log_lines = []
    dl_ok = dl_skip = dl_fail = 0
    vd_ok = vd_skip = vd_fail = 0

    # --- Step 3a: documents ---
    if do_docs and doc_matches:
        if args.dry_run:
            print(f"{'Board':<42} {'Date':<12} Type")
            print("-" * 68)
            for m in doc_matches:
                print(f"{m['board'][:41]:<42} {m['meeting_date']!s:<12} {m['doc_type']}")
            print()
        else:
            os.makedirs(args.output_dir, exist_ok=True)
            for m in doc_matches:
                dest  = make_doc_dest(
                    m["board"], m["doc_type"], m["meeting_date"],
                    args.output_dir, suffix=m.get("suffix", ""),
                )
                label = os.path.basename(dest)
                if os.path.exists(dest):
                    print(f"  skip (exists)  {label}")
                    dl_skip += 1
                    continue
                print(f"  [{m['meeting_date']}] {m['board']} — {m['doc_type']}")
                print(f"  downloading    {label}")
                parsed      = urllib.parse.urlparse(m["url"])
                enc_path    = urllib.parse.quote(parsed.path, safe="/")
                dl_url      = urllib.parse.urlunparse(parsed._replace(path=enc_path))
                if download_file(dl_url, dest):
                    dl_ok += 1
                    log_lines.append(
                        f"{datetime.datetime.now().isoformat()}  OK       {dest}"
                    )
                else:
                    dl_fail += 1
                    log_lines.append(
                        f"{datetime.datetime.now().isoformat()}  FAILED   {m['url']}"
                    )
                    if os.path.exists(dest):
                        os.remove(dest)
                time.sleep(DELAY)

    # --- Step 3b: video ---
    if do_video and video_matches:
        if not has_ytdlp and not args.dry_run:
            print("WARNING: yt-dlp not found — skipping video.", file=sys.stderr)
            print("  Install with:  pip install yt-dlp  or  sudo apt install yt-dlp",
                  file=sys.stderr)
        else:
            for m in video_matches:
                tmpl   = video_dest_template(m["board"], m["meeting_date"], args.output_dir)
                header = f"  [{m['meeting_date']}] {m['board']}"
                url    = f"https://vimeo.com/{m['vimeo_id']}"

                if args.dry_run:
                    print(header)
                    print(f"    {os.path.basename(tmpl)}")
                    print(f"    {url}")
                    continue

                print(header)
                if video_already_exists(tmpl):
                    existing = glob.glob(tmpl.replace(".%(ext)s", ".*"))[0]
                    print(f"    skip (exists)  {os.path.basename(existing)}")
                    vd_skip += 1
                    continue

                print(f"    downloading    {os.path.basename(tmpl)}")
                print(f"    source URL:    {url}")
                os.makedirs(os.path.dirname(tmpl), exist_ok=True)
                if download_vimeo_video(m["vimeo_id"], tmpl):
                    vd_ok += 1
                    log_lines.append(
                        f"{datetime.datetime.now().isoformat()}  OK       {url}"
                    )
                else:
                    vd_fail += 1
                    log_lines.append(
                        f"{datetime.datetime.now().isoformat()}  FAILED   {url}"
                    )

    if not args.dry_run:
        if log_lines:
            log_path = os.path.join(args.output_dir, "download-log.txt")
            os.makedirs(args.output_dir, exist_ok=True)
            with open(log_path, "a") as f:
                f.write("\n".join(log_lines) + "\n")
        if do_docs:
            print(f"\nDocuments  — downloaded: {dl_ok}  skipped: {dl_skip}  failed: {dl_fail}")
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
# Preview without downloading (docs only, 30-day window):
#   python3 scripts/download-north-haven-agendas.py --dry-run
#
# Download documents + Vimeo recordings:
#   python3 scripts/download-north-haven-agendas.py --include-video
#
# Video recordings only:
#   python3 scripts/download-north-haven-agendas.py --video-only
#
# Preview video recordings in window:
#   python3 scripts/download-north-haven-agendas.py --video-only --dry-run
#
# Narrow to one board:
#   python3 scripts/download-north-haven-agendas.py --include-video --board "Board of Selectmen"
#
# Change the lookback window:
#   python3 scripts/download-north-haven-agendas.py --include-video --days 15
#
# Run daily via cron (7 AM):
#   0 7 * * * cd /path/to/repo && python3 scripts/download-north-haven-agendas.py --include-video
#
# NOTE: The --ahead flag (default: 7 days) captures agendas for upcoming meetings
# already published. Run daily to stay current.
#
# NOTE: Only three boards post Vimeo recordings: Board of Selectmen, Board of
# Finance, and Board of Education. Other boards have no web video.
#
# NOTE: North Haven does not use CivicPlus, CivicClerk, or any third-party
# agenda platform. Documents are served from cms4files.revize.com.
# No authentication is required.
