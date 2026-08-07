#!/usr/bin/env python3
# download-worcester-agendas.py
# Download municipal meeting agendas, minutes, and video recordings from
# Worcester MA for meetings whose date falls within the past N days (and
# up to N_AHEAD days ahead, to catch agendas posted early for upcoming
# meetings).
#
# USAGE:
#   python3 scripts/download-worcester-agendas.py [options]
#
# REQUIREMENTS:
#   - Python 3.8+
#   - pip install playwright playwright-stealth
#   - python3 -m playwright install chromium
#   - yt-dlp (for video): pip install yt-dlp
#   - Internet connection
#
# WHAT IT DOES:
#   1. Fetches every meeting (upcoming + this year's archive) from the
#      PrimeGov public API — no committee filtering; Worcester's portal
#      lists 190+ committees (most inactive school councils), so this
#      script just takes whatever's actually scheduled/posted rather than
#      hand-picking "relevant" ones.
#   2. Downloads Agenda/Minutes/Packet/etc. PDFs to
#      beat-archive/worcester-agendas/YYYY-MM/
#   3. Downloads video: primarily from PrimeGov's own videoUrl field
#      (populated with direct YouTube links for most boards — see VIDEO
#      NOTE), plus a supplementary YouTube playlist for School Committee,
#      whose videoUrl is consistently null in the API.
#   4. Appends a download log to beat-archive/worcester-agendas/download-log.txt
#
# SITE STRUCTURE:
#   Same PrimeGov platform as Arlington MA (worcesterma.primegov.com vs.
#   arlingtonma.primegov.com — different tenant, identical API/download
#   mechanics):
#     Portal:  https://worcesterma.primegov.com/public/portal
#     API (plain HTTP, no session needed):
#       GET /api/committee/GetCommitteeesListByShowInPublicPortal
#       GET /api/v2/PublicPortal/ListUpcomingMeetings
#       GET /api/v2/PublicPortal/ListArchivedMeetings?year=YYYY
#         documentList[].templateId + .compileOutputType identify each PDF.
#         videoUrl, when populated, is a direct YouTube watch URL.
#     PDF download requires a real browser session — same as Arlington,
#     confirmed directly: this endpoint sends Content-Disposition:
#     attachment, so it triggers a real browser download rather than
#     returning a body an in-page fetch() can read. Playwright's
#     expect_download() is used, identical to Arlington's script:
#       GET /Public/CompiledDocument?meetingTemplateId={templateId}&compileOutputType={compileOutputType}
#     A small number of documents are labeled "HTM Agenda" (vs. Worcester's
#     spelling; Arlington's equivalent duplicate was "HTML Agenda") —
#     duplicate HTML renderings of the same compiled PDF, served from a
#     different URL scheme this script can't fetch correctly. Filtered out
#     the same way Arlington's script filters "html "-prefixed template
#     names.
#
# VIDEO NOTE — Worcester's PrimeGov instance populates videoUrl directly
# with YouTube links for most boards (confirmed: 239 of 441 archived 2026
# meetings have a real videoUrl) — a meaningful upgrade over Arlington,
# where every video field was empty and video required a completely
# separate ACMi YouTube-playlist hunt. The one exception found: School
# Committee's videoUrl is consistently null across every meeting checked
# (its Standing Committee counterparts and other boards are fine). Its
# video lives on a separate, actively-maintained YouTube playlist instead:
#   https://www.youtube.com/playlist?list=PLZqyT6uNMVa9VWqRvrOSa9LZehdTuo0pA
# Titles follow "Worcester School Committee Meeting - MM-DD-YY" (mostly
# consistent, occasional missing titles for unrelated/private entries in
# the same playlist, skipped naturally when title parsing fails).
#
# COVERAGE: School Committee (Board of Education) and Parks and Recreation
# Commission are both native PrimeGov committees, documents and (for Parks
# & Rec) video included automatically — no external sourcing needed beyond
# the one School Committee video supplement above.

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
import urllib.request

try:
    from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout
    from playwright_stealth import Stealth
except ImportError as _ie:
    print(
        f"ERROR: missing dependency: {_ie}\n"
        "  pip install playwright playwright-stealth\n"
        "  python3 -m playwright install chromium",
        file=sys.stderr,
    )
    sys.exit(1)

YT_DLP_NODE = "node:/home/richkirby/.local/bin/yt-dlp-node"  # yt-dlp needs Node 22+; symlink kept current by scripts/update-yt-dlp-node.sh

# --- Configuration ---
TENANT = "worcesterma"
API_BASE = f"https://{TENANT}.primegov.com"
PORTAL_URL = f"{API_BASE}/public/portal"
OUTPUT_DIR = "beat-archive/worcester-agendas"
DAYS_BACK = 4
DAYS_AHEAD = 7
DELAY_SECONDS = 1
PAGE_TIMEOUT = 30_000  # ms

UA = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)

SCHOOL_COMMITTEE_PLAYLIST = "PLZqyT6uNMVa9VWqRvrOSa9LZehdTuo0pA"
_PLAYLIST_DATE_RE = re.compile(r"(\d{1,2})[-/](\d{1,2})[-/](\d{2,4})\s*$")
PLAYLIST_SCAN_CAP = 60


# --- HTTP helpers (plain urllib — PrimeGov's JSON API needs no session) ---

def fetch_json(url):
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return json.loads(r.read().decode("utf-8", errors="replace"))
    except urllib.error.URLError as e:
        print(f"  ERROR fetching {url}: {e}", file=sys.stderr)
        return None
    except (json.JSONDecodeError, ValueError) as e:
        print(f"  ERROR parsing JSON from {url}: {e}", file=sys.stderr)
        return None


def fetch_committees():
    data = fetch_json(f"{API_BASE}/api/committee/GetCommitteeesListByShowInPublicPortal")
    return {c["id"]: c["name"] for c in (data or [])}


def fetch_meetings(archive_years):
    meetings = fetch_json(f"{API_BASE}/api/v2/PublicPortal/ListUpcomingMeetings") or []
    for year in archive_years:
        meetings += fetch_json(
            f"{API_BASE}/api/v2/PublicPortal/ListArchivedMeetings?year={year}"
        ) or []
    return meetings


# --- PDF download via Playwright (session-scoped URLs) ---

def download_pdf(pw_page, template_id, compile_output_type, dest_path):
    url = (
        f"{API_BASE}/Public/CompiledDocument"
        f"?meetingTemplateId={template_id}&compileOutputType={compile_output_type}"
    )
    try:
        with pw_page.expect_download(timeout=PAGE_TIMEOUT) as dl_info:
            try:
                pw_page.goto(url, timeout=PAGE_TIMEOUT)
            except PWTimeout:
                raise
            except Exception as nav_err:
                if "Download is starting" not in str(nav_err):
                    raise
        download = dl_info.value
        download.save_as(dest_path)
        return True
    except Exception as e:
        print(f"  WARNING: {e}", file=sys.stderr)
        return False


# --- Video download via yt-dlp ---

def download_youtube_video(video_url, dest_path):
    cmd = [
        "yt-dlp", "--js-runtimes", YT_DLP_NODE,
        "--no-playlist",
        "-f", "bestvideo+bestaudio/best",
        "--merge-output-format", "mp4",
        "-o", dest_path,
        "--no-overwrites",
        "--quiet",
        "--no-warnings",
        video_url,
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
        print(f"  WARNING: yt-dlp timed out downloading {video_url}", file=sys.stderr)
        return False


def collect_school_committee_videos(cutoff, future_limit):
    ytdlp = shutil.which("yt-dlp")
    if not ytdlp:
        print("  WARNING: yt-dlp not found, skipping School Committee playlist", file=sys.stderr)
        return []
    cmd = [ytdlp, "--flat-playlist", "--dump-json", "--playlist-end", str(PLAYLIST_SCAN_CAP),
           f"https://www.youtube.com/playlist?list={SCHOOL_COMMITTEE_PLAYLIST}"]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
    except subprocess.TimeoutExpired:
        print("  WARNING: School Committee playlist listing timed out", file=sys.stderr)
        return []
    if result.returncode != 0:
        print(f"  WARNING: playlist listing failed: {result.stderr.strip()[:200]}", file=sys.stderr)
        return []

    videos = []
    for line in result.stdout.splitlines():
        try:
            d = json.loads(line)
        except json.JSONDecodeError:
            continue
        title = d.get("title") or ""
        m = _PLAYLIST_DATE_RE.search(title)
        if not m:
            continue
        month, day, year = int(m.group(1)), int(m.group(2)), int(m.group(3))
        if year < 100:
            year += 2000
        try:
            meeting_date = datetime.date(year, month, day)
        except ValueError:
            continue
        if cutoff <= meeting_date <= future_limit:
            videos.append({
                "board": "School Committee",
                "meeting_date": meeting_date,
                "extra": title,
                "video_id": d.get("id"),
            })
    return videos


# --- File naming ---

def slugify(text, max_len=60):
    text = text.lower().strip()
    text = re.sub(r"[/\\&]", "-", text)
    text = re.sub(r"[^\w\s-]", "", text)
    text = re.sub(r"[\s_]+", "-", text)
    text = re.sub(r"-{2,}", "-", text)
    return text.strip("-")[:max_len]


def make_pdf_dest(board, doc_type, meeting_date, output_dir, counter=0):
    month_dir = os.path.join(output_dir, meeting_date.strftime("%Y-%m"))
    os.makedirs(month_dir, exist_ok=True)
    suffix = f"-{counter}" if counter > 0 else ""
    fname = f"{meeting_date.strftime('%Y-%m-%d')}-{slugify(board)}-{slugify(doc_type)}{suffix}.pdf"
    return os.path.join(month_dir, fname)


def make_video_dest(board, meeting_date, output_dir, counter=0):
    month_dir = os.path.join(output_dir, meeting_date.strftime("%Y-%m"))
    os.makedirs(month_dir, exist_ok=True)
    suffix = f"-{counter}" if counter > 0 else ""
    fname = f"{meeting_date.strftime('%Y-%m-%d')}-{slugify(board)}-video{suffix}.mp4"
    return os.path.join(month_dir, fname)


# --- Main ---

def main():
    parser = argparse.ArgumentParser(
        description=(
            "Download Worcester MA meeting agendas, minutes, and video "
            "recordings for meetings within the past N days."
        )
    )
    parser.add_argument("--days", type=int, default=DAYS_BACK, metavar="N",
                        help=f"Look back N days by meeting date (default: {DAYS_BACK})")
    parser.add_argument("--ahead", type=int, default=DAYS_AHEAD, metavar="N",
                        help=f"Also include meetings up to N days ahead (default: {DAYS_AHEAD})")
    parser.add_argument("--output-dir", default=OUTPUT_DIR, metavar="DIR",
                        help=f"Destination directory (default: {OUTPUT_DIR})")
    parser.add_argument("--dry-run", action="store_true",
                        help="List matching items without downloading")
    parser.add_argument("--no-video", action="store_true",
                        help="Skip video recordings (PDFs only)")
    parser.add_argument("--board", metavar="NAME",
                        help="Only include boards/committees containing NAME (case-insensitive)")
    parser.add_argument("--show-browser", action="store_true",
                        help="Run Playwright with a visible browser window (debugging)")
    args = parser.parse_args()

    now = datetime.datetime.now()
    if (now.weekday() == 5 and now.hour >= 18) or (now.weekday() == 6 and now.hour < 12):
        print("Skipping — no downloads on Saturday nights or Sunday mornings.")
        sys.exit(0)

    today = datetime.date.today()
    cutoff = today - datetime.timedelta(days=args.days)
    future_limit = today + datetime.timedelta(days=args.ahead)
    include_video = not args.no_video
    board_filter = args.board.lower() if args.board else None

    print(f"Date window : {cutoff} to {future_limit}")
    print(f"Portal      : {PORTAL_URL}")
    if not args.dry_run:
        print(f"Output dir  : {args.output_dir}")
    print()

    print("Fetching committee list...")
    committees = fetch_committees()
    print(f"  {len(committees)} committee(s) in public portal.")

    archive_years = sorted({cutoff.year, today.year, future_limit.year})
    print(f"Fetching meetings ({', '.join(str(y) for y in archive_years)}, plus upcoming)...")
    meetings = fetch_meetings(archive_years)
    print(f"  {len(meetings)} meeting(s) total.")
    print()

    docs = []
    videos = []
    for m in meetings:
        dt = m.get("dateTime")
        if not dt:
            continue
        meeting_date = datetime.date.fromisoformat(dt[:10])
        if not (cutoff <= meeting_date <= future_limit):
            continue
        board = committees.get(m.get("committeeId"), m.get("title") or "Unknown")
        if board_filter and board_filter not in board.lower():
            continue

        for d in m.get("documentList") or []:
            template_name = d.get("templateName") or "document"
            if template_name.lower().startswith(("html ", "htm ")):
                continue
            docs.append({
                "board": board,
                "meeting_date": meeting_date,
                "doc_type": template_name.lower(),
                "template_id": d["templateId"],
                "compile_output_type": d["compileOutputType"],
            })

        video_url = m.get("videoUrl")
        if include_video and video_url:
            videos.append({"board": board, "meeting_date": meeting_date,
                           "extra": m.get("title") or board, "video_url": video_url})

    if include_video and not (board_filter and "school" not in board_filter):
        sc_videos = collect_school_committee_videos(cutoff, future_limit)
        for v in sc_videos:
            videos.append({"board": v["board"], "meeting_date": v["meeting_date"],
                           "extra": v["extra"],
                           "video_url": f"https://www.youtube.com/watch?v={v['video_id']}"})

    # Assign per-(board, date, doc_type) counters for filename disambiguation
    key_counts = {}
    for d in docs:
        key = (d["board"], d["meeting_date"], d["doc_type"])
        key_counts[key] = key_counts.get(key, 0) + 1
    key_counter = {}
    for d in docs:
        key = (d["board"], d["meeting_date"], d["doc_type"])
        if key_counts[key] > 1:
            key_counter[key] = key_counter.get(key, 0) + 1
            d["counter"] = key_counter[key] - 1
        else:
            d["counter"] = 0

    v_key_counts = {}
    for v in videos:
        key = (v["board"], v["meeting_date"])
        v_key_counts[key] = v_key_counts.get(key, 0) + 1
    v_key_counter = {}
    for v in videos:
        key = (v["board"], v["meeting_date"])
        if v_key_counts[key] > 1:
            v_key_counter[key] = v_key_counter.get(key, 0) + 1
            v["counter"] = v_key_counter[key] - 1
        else:
            v["counter"] = 0

    docs.sort(key=lambda x: (x["meeting_date"], x["board"]), reverse=True)
    videos.sort(key=lambda x: (x["meeting_date"], x["board"]), reverse=True)

    print(f"{len(docs)} document(s) across {len({d['board'] for d in docs})} board(s) in window.")
    if include_video:
        print(f"{len(videos)} video(s) in window.")
    print()

    if args.dry_run:
        print(f"{'Board':<45} {'Date':<12} Type")
        print("-" * 70)
        for d in docs:
            print(f"{d['board'][:44]:<45} {d['meeting_date']!s:<12} {d['doc_type']}")
        if videos:
            print(f"\n{'Board':<45} {'Date':<12} Video")
            print("-" * 70)
            for v in videos:
                print(f"{v['board'][:44]:<45} {v['meeting_date']!s:<12} {v['video_url']}")
        print(f"\n{len(docs) + len(videos)} item(s). Re-run without --dry-run to download.")
        return

    os.makedirs(args.output_dir, exist_ok=True)
    log_path = os.path.join(args.output_dir, "download-log.txt")
    log_lines = []
    downloaded = skipped = failed = 0

    with Stealth().use_sync(sync_playwright()) as pw:
        browser = pw.chromium.launch(headless=not args.show_browser)
        ctx = browser.new_context(user_agent=UA, locale="en-US", accept_downloads=True)
        pw_page = ctx.new_page()

        print("Establishing session with PrimeGov portal...")
        try:
            pw_page.goto(PORTAL_URL, wait_until="networkidle", timeout=PAGE_TIMEOUT)
        except PWTimeout:
            pass
        time.sleep(1)

        for d in docs:
            dest = make_pdf_dest(d["board"], d["doc_type"], d["meeting_date"], args.output_dir, d["counter"])
            label = os.path.basename(dest)
            if os.path.exists(dest):
                print(f"  skip (exists)  {label}")
                skipped += 1
                continue

            print(f"  [{d['meeting_date']}] {d['board']} — {d['doc_type']}")
            print(f"  downloading    {label}")
            if download_pdf(pw_page, d["template_id"], d["compile_output_type"], dest):
                downloaded += 1
                log_lines.append(f"{datetime.datetime.now().isoformat()}  OK       {dest}")
            else:
                failed += 1
                log_lines.append(
                    f"{datetime.datetime.now().isoformat()}  FAILED   "
                    f"templateId={d['template_id']} compileOutputType={d['compile_output_type']}"
                )
                if os.path.exists(dest):
                    os.remove(dest)
            time.sleep(DELAY_SECONDS)

        browser.close()

    for v in videos:
        dest = make_video_dest(v["board"], v["meeting_date"], args.output_dir, v["counter"])
        label = os.path.basename(dest)
        if os.path.exists(dest):
            print(f"  skip (exists)  {label}")
            skipped += 1
            continue
        print(f"  [{v['meeting_date']}] {v['board']} — video")
        print(f"  downloading    {label}")
        if download_youtube_video(v["video_url"], dest):
            downloaded += 1
            log_lines.append(f"{datetime.datetime.now().isoformat()}  OK       {dest}")
        else:
            failed += 1
            log_lines.append(f"{datetime.datetime.now().isoformat()}  FAILED   {v['video_url']}")

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
#    python3 scripts/download-worcester-agendas.py --dry-run
#
# 2. Download the default window (4 days back, 7 ahead):
#    python3 scripts/download-worcester-agendas.py
#
# 3. Docs only, skip video:
#    python3 scripts/download-worcester-agendas.py --no-video
#
# 4. Just one board:
#    python3 scripts/download-worcester-agendas.py --board "School Committee" --days 30
#
# 5. Watch it run instead of headless (debugging the Playwright session):
#    python3 scripts/download-worcester-agendas.py --show-browser
