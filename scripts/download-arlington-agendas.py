#!/usr/bin/env python3
# download-arlington-agendas.py
# Download municipal meeting agendas, minutes, and video recordings from
# Arlington MA for meetings whose date falls within the past N days (and
# up to N_AHEAD days ahead, to catch agendas posted early for upcoming
# meetings).
#
# USAGE:
#   python3 scripts/download-arlington-agendas.py [options]
#
# REQUIREMENTS:
#   - Python 3.8+
#   - pip install playwright playwright-stealth
#   - python3 -m playwright install chromium
#   - yt-dlp (for video): pip install yt-dlp   OR   sudo apt install yt-dlp
#   - Internet connection
#
# WHAT IT DOES:
#   1. Fetches every board/committee's meetings (upcoming + this year's
#      archive) from the PrimeGov public API.
#   2. Downloads Agenda and Minutes PDFs whose meeting date falls within
#      the date window to beat-archive/arlington-agendas/YYYY-MM/.
#   3. Downloads video recordings from ACMi's per-board YouTube playlists
#      for meetings within the window (--no-video to skip).
#   4. Appends a download log to beat-archive/arlington-agendas/download-log.txt
#
# SITE STRUCTURE:
#   The town's own site (www.arlingtonma.gov) sits behind an Akamai WAF
#   that 403s plain HTTP requests outright — but it doesn't matter, because
#   the town's "Agendas and Minutes" page just embeds a third-party portal
#   in an iframe, and that portal is the real source of truth:
#
#   Documents (PrimeGov):
#     Portal:  https://arlingtonma.primegov.com/public/portal
#     API (plain HTTP, no session needed):
#       GET /api/committee/GetCommitteeesListByShowInPublicPortal
#           → [{id, name}, ...] — includes School Committee (Arlington's
#             name for what's elsewhere called Board of Education) and
#             Parks & Recreation Commission, same as every other board.
#       GET /api/v2/PublicPortal/ListUpcomingMeetings
#       GET /api/v2/PublicPortal/ListArchivedMeetings?year=YYYY
#           → [{id, committeeId, title, dateTime, documentList: [...]}]
#           documentList[].templateId + .compileOutputType identify each
#           PDF (templateName "Agenda" / "Minutes" / etc).
#     PDF download requires a real browser session — plain curl/urllib
#     gets "NotFound" even with the exact right URL and cookies from a
#     prior request. Confirmed the URL pattern directly from a live click
#     in-browser:
#       GET /Public/CompiledDocument?meetingTemplateId={templateId}&compileOutputType={compileOutputType}
#     Unlike Westport/Easton/etc., this endpoint sends
#     Content-Disposition: attachment, so it triggers a real browser
#     download rather than returning a body an in-page fetch() can read —
#     fetch() to this URL fails outright with "TypeError: Failed to fetch"
#     (confirmed against a live authenticated tab), and Page.goto() throws
#     "Download is starting". Playwright's expect_download() is the
#     correct tool for this shape and is used below instead of the
#     fetch()-based pattern other scripts in this repo use.
#
#   Video (ACMi — Arlington Community Media, Inc., not part of PrimeGov):
#     None of PrimeGov's video fields (videoUrl/swagitId/isMediaManagerVideo)
#     are populated for Arlington — video isn't hosted there at all.
#     ACMi publishes recordings to YouTube channel UCztbi9KA9roQAAoT... via
#     per-board "Gov - <Board Name>" playlists (COMMITTEE_PLAYLISTS below).
#     Only ~10 boards have a dedicated playlist; smaller boards/commissions
#     including Parks & Recreation Commission don't appear to be televised
#     at all — the script just skips video for any board without a mapped
#     playlist and still downloads its PDFs normally.

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

# --- Configuration ---
TENANT = "arlingtonma"
API_BASE = f"https://{TENANT}.primegov.com"
PORTAL_URL = f"{API_BASE}/public/portal"
OUTPUT_DIR = "beat-archive/arlington-agendas"
DAYS_BACK = 4
DAYS_AHEAD = 7
DELAY_SECONDS = 1
PAGE_TIMEOUT = 30_000  # ms

UA = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)

# Committee name (as returned by GetCommitteeesListByShowInPublicPortal) ->
# ACMi "Gov - <Board>" YouTube uploads playlist. Confirmed by listing
# https://www.youtube.com/c/AcmiTv/playlists — only these ~7 boards have a
# dedicated playlist AND a matching entry in PrimeGov's committee list.
# ACMi also has "Gov - Housing Authority" and "Gov - Artificial Turf Study
# Committee" playlists, but neither name matches any committee in PrimeGov's
# list or any meeting title in 2025/2026 (checked directly against the API)
# — those boards appear dormant/not running meetings through PrimeGov, so
# they're deliberately left out; a board string from this script's meeting
# data will never equal either name. Board names not listed here (including
# Parks & Recreation Commission) get PDFs only; no video source has been
# found for them.
COMMITTEE_PLAYLISTS = {
    "Select Board": "PLztbi9KA9roVibSYmXmzb1iHjbR4QWwoK",
    "School Committee": "PLztbi9KA9roU84XO67gsq9n8mMraWjSn1",
    "Finance Committee": "PLztbi9KA9roVK8k_bEfN-Y5619nGGgGAK",
    "Zoning Board of Appeals": "PLztbi9KA9roVxQQBRInD-qTvK1AII4QKb",
    "Conservation Commission": "PLztbi9KA9roW66OqGD3D49dlcyOudnRW_",
    "Redevelopment Board": "PLztbi9KA9roXI_dttpoxK9FO-5FkBTDkx",
    "Town Meeting": "PLztbi9KA9roVEhX9eU23jGuYZrNp9s3Ly",
}


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
                # goto() throws once the download starts (e.g. "Download is
                # starting") — that's expected, not a failure; the download
                # itself is captured via expect_download() below.
                if "Download is starting" not in str(nav_err):
                    raise
        download = dl_info.value
        download.save_as(dest_path)
        return True
    except Exception as e:
        print(f"  WARNING: {e}", file=sys.stderr)
        return False


# --- Video download via yt-dlp (per-board ACMi playlists) ---

def download_videos_for_board(board, playlist_id, cutoff, output_dir, dry_run):
    ytdlp = shutil.which("yt-dlp")
    if not ytdlp:
        print("  WARNING: yt-dlp not found, skipping video", file=sys.stderr)
        return 0, 0

    date_str = cutoff.strftime("%Y%m%d")
    board_slug = slugify(board, max_len=45)
    out_tmpl = os.path.join(output_dir, "%(upload_date)s", f"%(upload_date)s-{board_slug}-video-%(id)s.%(ext)s")

    deno_path = os.path.expanduser("~/.deno/bin/deno")
    deno_arg = f"deno:{deno_path}" if os.path.exists(deno_path) else "deno"

    cmd = [
        ytdlp,
        "--dateafter", date_str,
        "--break-match-filters", f"upload_date>={date_str}",
        # Playlists are newest-first and can run for years of history;
        # cap the walk so a rate-limit mid-scan can't turn into a full
        # unbounded crawl (same guard as download-ridgefield-boe-meetings.py).
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
        cmd += ["--simulate", "--print", f"  [dry] {board}: %(upload_date)s  %(title)s  [%(id)s]"]
    cmd.append(f"https://www.youtube.com/playlist?list={playlist_id}")

    try:
        result = subprocess.run(cmd, timeout=1800, capture_output=dry_run, text=True)
        if dry_run and result.stdout:
            print(result.stdout, end="")
    except subprocess.TimeoutExpired:
        print(f"  WARNING: yt-dlp timed out for {board} — partial file(s) kept", file=sys.stderr)
        return 0, 1
    return (0 if result.returncode == 0 else 1), 0


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
    fname = f"{meeting_date.strftime('%Y-%m-%d')}-{slugify(board)}-{doc_type}{suffix}.pdf"
    return os.path.join(month_dir, fname)


# --- Main ---

def main():
    parser = argparse.ArgumentParser(
        description=(
            "Download Arlington MA meeting agendas, minutes, and video "
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

    # Filter to date window and attach a resolved board name + PDF doc list
    docs = []
    video_boards = {}  # board -> earliest-in-window meeting date seen (for dry-run/logging only)
    for m in meetings:
        dt = m.get("dateTime")
        if not dt:
            continue
        meeting_date = datetime.date.fromisoformat(dt[:10])
        if not (cutoff <= meeting_date <= future_limit):
            continue
        board = committees.get(m.get("committeeId"), m.get("title") or "Unknown")
        if args.board and args.board.lower() not in board.lower():
            continue

        for d in m.get("documentList") or []:
            template_name = d.get("templateName") or "document"
            if template_name.lower().startswith("html "):
                # Duplicate HTML rendering of the same compiled PDF
                # (compileOutputType 3 vs 1), served from a different URL
                # scheme (/Portal/Meeting, not /Public/CompiledDocument).
                # The PDF version of the same document is already in this
                # list, so skip the HTML variant rather than fetch it wrong.
                continue
            doc_type = template_name.lower()
            docs.append({
                "board": board,
                "meeting_date": meeting_date,
                "doc_type": doc_type,
                "template_id": d["templateId"],
                "compile_output_type": d["compileOutputType"],
            })
        if board in COMMITTEE_PLAYLISTS:
            video_boards.setdefault(board, meeting_date)

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

    docs.sort(key=lambda x: (x["meeting_date"], x["board"]), reverse=True)

    print(f"{len(docs)} document(s) across {len({d['board'] for d in docs})} board(s) in window.")
    if include_video:
        boards_with_video = sorted(b for b in video_boards if b in COMMITTEE_PLAYLISTS)
        print(f"{len(boards_with_video)} board(s) in window have a known video playlist: "
              f"{', '.join(boards_with_video) or '(none)'}")
    print()

    if args.dry_run:
        print(f"{'Board':<40} {'Date':<12} Type")
        print("-" * 64)
        for d in docs:
            print(f"{d['board'][:39]:<40} {d['meeting_date']!s:<12} {d['doc_type']}")
        if include_video:
            print(f"\nVideo playlists to check: {sorted(video_boards.keys())}")
        print(f"\n{len(docs)} document(s). Re-run without --dry-run to download.")
        if include_video:
            for board in sorted(video_boards):
                if board in COMMITTEE_PLAYLISTS:
                    download_videos_for_board(board, COMMITTEE_PLAYLISTS[board], cutoff, args.output_dir, dry_run=True)
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

    if include_video:
        for board in sorted(video_boards):
            if board not in COMMITTEE_PLAYLISTS:
                continue
            print(f"\nChecking ACMi video playlist for {board}...")
            fail, err = download_videos_for_board(
                board, COMMITTEE_PLAYLISTS[board], cutoff, args.output_dir, dry_run=False
            )
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
#    python3 scripts/download-arlington-agendas.py --dry-run
#
# 2. Download the default window (4 days back, 7 ahead):
#    python3 scripts/download-arlington-agendas.py
#
# 3. Docs only, skip the ACMi video check:
#    python3 scripts/download-arlington-agendas.py --no-video
#
# 4. Just one board (e.g. catching up School Committee specifically):
#    python3 scripts/download-arlington-agendas.py --board "School Committee" --days 30
#
# 5. Watch it run instead of headless (debugging the Playwright session):
#    python3 scripts/download-arlington-agendas.py --show-browser
