#!/usr/bin/env python3
# download-new-hope-agendas.py
# Download meeting agendas, minutes, and video recordings from New Hope
# Borough, PA (newhopeborough.org) posted in the last N days.
#
# USAGE:
#   python3 scripts/download-new-hope-agendas.py [options]
#
# REQUIREMENTS:
#   - Python 3.6+  (stdlib only for PDFs)
#   - yt-dlp       (for Vimeo video: pip install yt-dlp)
#   - Internet connection
#
# WHAT IT DOES:
#   Documents:
#     1. Fetches https://www.newhopeborough.org/AgendaCenter (all boards on
#        one page, no pagination needed — 8 boards, ~50 rows total).
#     2. For each meeting row, extracts the "Posted" date from the h3 heading.
#     3. Downloads agenda and minutes PDFs whose Posted date falls within
#        the --days window.
#
#   Videos:
#     4. Considers all meetings whose meeting date is within --video-lookback
#        days (default 14) — these are recent enough to have a freshly
#        uploaded recording.
#     5. Checks each Vimeo URL via the oEmbed API (no auth required, works
#        with private/unlisted hash URLs) to read the upload_date.
#     6. Downloads videos whose upload_date falls within --days using yt-dlp.
#
# SITE STRUCTURE (CivicPlus Agenda Center):
#   Base:       https://www.newhopeborough.org
#   Page:       /AgendaCenter  (all 8 boards on one page)
#   Documents:  /AgendaCenter/ViewFile/Agenda/_{MMDDYYYY}-{id}
#               /AgendaCenter/ViewFile/Minutes/_{MMDDYYYY}-{id}
#   Videos:     https://vimeo.com/{id}/{hash}?... (private-share Vimeo URLs)
#   Boards:
#     New Hope Borough Council
#     New Hope Environmental Advisory Council
#     New Hope Green Initiative Committee
#     New Hope Historic Architectural Review Board (HARB)
#     New Hope Parks & Recreation Board
#     New Hope Planning Commission
#     New Hope Shade Tree Commission
#     New Hope Zoning Hearing Board
#
# NOTE: CivicPlus does not return Last-Modified on /AgendaCenter/ViewFile/
#   URLs, so the "Posted" date in the page heading is the only posting
#   timestamp available for PDFs.
#
# NOTE: The "Posted" date reflects when the AGENDA was first published and
#   does not change when minutes are added later. Minutes for very recent
#   meetings will be caught when first posted; minutes added weeks later to
#   an older row will not be re-detected.
#
# NOTE: Vimeo links use private-share hash tokens (/id/hash). yt-dlp handles
#   these correctly when given the full URL. The oEmbed API also accepts them.

import argparse
import datetime
import json

YT_DLP_NODE = "node:/home/richkirby/.nvm/versions/node/v20.20.2/bin/node"  # yt-dlp needs Node 20+; system node is 18
import os
import re
import subprocess
import sys
import time
import urllib.error
import urllib.request

# --- Configuration ---
BASE_URL      = "https://www.newhopeborough.org"
AGENDA_URL    = f"{BASE_URL}/AgendaCenter"
OUTPUT_DIR    = "beat-archive/new-hope-agendas"
DAYS_BACK     = 3
VIDEO_LOOKBACK = 14   # days: meetings this recent may have fresh video uploads

PAGE_DELAY     = 0.5
OEMBED_DELAY   = 0.25
DOWNLOAD_DELAY = 0.8

UA = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36"
)

_MONTH_MAP = {
    "jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6,
    "jul": 7, "aug": 8, "sep": 9, "oct": 10, "nov": 11, "dec": 12,
    "january": 1, "february": 2, "march": 3, "april": 4, "june": 6,
    "july": 7, "august": 8, "september": 9, "october": 10, "november": 11,
    "december": 12,
}


# --- HTTP helpers ---

def fetch_html(url):
    req = urllib.request.Request(
        url, headers={"User-Agent": UA, "Accept": "text/html,*/*"}
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            charset = r.headers.get_content_charset() or "utf-8"
            return r.read().decode(charset, errors="replace")
    except urllib.error.URLError as e:
        print(f"ERROR fetching {url}: {e}", file=sys.stderr)
        return None


def download_pdf(url, dest_path):
    full_url = BASE_URL + url if url.startswith("/") else url
    req = urllib.request.Request(
        full_url, headers={"User-Agent": UA, "Accept": "application/pdf,*/*"}
    )
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            with open(dest_path, "wb") as f:
                f.write(r.read())
        return True
    except Exception as e:
        print(f"  WARNING: {e}", file=sys.stderr)
        return False


def vimeo_upload_date(vimeo_url):
    """Use Vimeo oEmbed (no auth) to get upload_date; works with private hash URLs."""
    oembed = f"https://vimeo.com/api/oembed.json?url={vimeo_url}"
    req = urllib.request.Request(
        oembed, headers={"User-Agent": UA, "Accept": "application/json"}
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            d = json.loads(r.read())
        ud = d.get("upload_date", "")
        title = d.get("title", "")
        if ud:
            return datetime.date.fromisoformat(ud[:10]), title
    except Exception as e:
        print(f"  WARNING: oEmbed failed for {vimeo_url}: {e}", file=sys.stderr)
    return None, ""


def download_vimeo(vimeo_url, dest_path):
    cmd = [
        "yt-dlp", "--js-runtimes", YT_DLP_NODE, "--no-playlist",
        "-f", "bestvideo+bestaudio/best",
        "--merge-output-format", "mp4",
        "-o", dest_path,
        "--no-overwrites", "--quiet", "--no-warnings",
        vimeo_url,
    ]
    try:
        subprocess.run(cmd, check=True, timeout=600)
        return True
    except FileNotFoundError:
        print("  ERROR: yt-dlp not found — install with: pip install yt-dlp",
              file=sys.stderr)
        return False
    except subprocess.CalledProcessError as e:
        print(f"  WARNING: yt-dlp failed ({e})", file=sys.stderr)
        return False


# --- Parsing ---

def parse_abbr_date(abbr_text, day_str, year_str):
    """Parse a CivicPlus abbr-month date: 'Jun', '12', '2026' → date."""
    month = _MONTH_MAP.get(abbr_text.lower().strip())
    if not month:
        return None
    try:
        return datetime.date(int(year_str), month, int(day_str))
    except ValueError:
        return None


def parse_agenda_center(html_text):
    """
    Parse the New Hope CivicPlus Agenda Center HTML.
    Returns list of dicts with keys:
      board, meeting_date, posted_date, agenda_url, minutes_url, vimeo_url
    """
    items = []
    current_board = "Unknown Board"

    # Board sections start with <h2 ...>Board Name</h2>
    # Meeting rows are <tr class="catAgendaRow">
    # Split by section headers and rows interleaved
    chunk_re = re.compile(
        r'(<h2[^>]*>.*?</h2>|<tr[^>]+class="catAgendaRow"[^>]*>.*?</tr>)',
        re.DOTALL | re.IGNORECASE,
    )

    for chunk in chunk_re.finditer(html_text):
        block = chunk.group(1)

        # Board heading
        h2_m = re.match(r'<h2', block, re.IGNORECASE)
        if h2_m:
            text = re.sub(r"<[^>]+>", "", block).strip()
            if text:
                current_board = text
            continue

        # Meeting row — extract the key fields
        # 1. Meeting date: aria-label="Agenda for June 16, 2026"
        meet_m = re.search(
            r'aria-label="Agenda for ([A-Za-z]+ \d+, \d{4})"', block
        )
        try:
            meeting_date = (
                datetime.datetime.strptime(meet_m.group(1), "%B %d, %Y").date()
                if meet_m else None
            )
        except ValueError:
            meeting_date = None

        # 2. Posted date: Posted <abbr ...>Mon</abbr> DD, YYYY
        posted_m = re.search(
            r'Posted\s+<abbr[^>]*>([^<]+)</abbr>\s+(\d+),\s+(\d{4})',
            block,
        )
        posted_date = (
            parse_abbr_date(posted_m.group(1), posted_m.group(2), posted_m.group(3))
            if posted_m else None
        )

        # 3. Agenda URL
        ag_m = re.search(
            r'href="(/AgendaCenter/ViewFile/Agenda/[^"?]+)"', block
        )
        agenda_url = ag_m.group(1) if ag_m else None

        # 4. Minutes URL
        mn_m = re.search(
            r'href="(/AgendaCenter/ViewFile/Minutes/[^"?]+)"', block
        )
        minutes_url = mn_m.group(1) if mn_m else None

        # 5. Vimeo URL (private hash format: /id/hash?...)
        vi_m = re.search(
            r'href="(https://vimeo\.com/\d+/[A-Za-z0-9]+[^"]*)"', block
        )
        vimeo_url = vi_m.group(1).split("&")[0] if vi_m else None

        if posted_date or meeting_date:
            items.append({
                "board":        current_board,
                "meeting_date": meeting_date,
                "posted_date":  posted_date,
                "agenda_url":   agenda_url,
                "minutes_url":  minutes_url,
                "vimeo_url":    vimeo_url,
            })

    return items


# --- File naming ---

def slugify(text, max_len=50):
    text = re.sub(r"<[^>]+>", "", text).lower().strip()
    text = re.sub(r"[^\w\s-]", "", text)
    text = re.sub(r"[\s_]+", "-", text)
    text = re.sub(r"-{2,}", "-", text)
    return text.strip("-")[:max_len]


def make_pdf_dest(doc_type, board, meeting_date, output_dir):
    ref = meeting_date or datetime.date.today()
    month_dir = os.path.join(output_dir, ref.strftime("%Y-%m"))
    os.makedirs(month_dir, exist_ok=True)
    return os.path.join(
        month_dir,
        f"{ref.strftime('%Y-%m-%d')}-{slugify(board)}-{doc_type}.pdf"
    )


def make_video_dest(board, upload_date, output_dir, counter=0):
    month_dir = os.path.join(output_dir, upload_date.strftime("%Y-%m"))
    os.makedirs(month_dir, exist_ok=True)
    suffix = f"-{counter}" if counter > 0 else ""
    return os.path.join(
        month_dir,
        f"{upload_date.strftime('%Y-%m-%d')}-{slugify(board)}-video{suffix}.mp4"
    )


# --- Main ---

def main():
    parser = argparse.ArgumentParser(
        description=(
            "Download New Hope Borough PA meeting agendas, minutes, and "
            "Vimeo recordings posted in the past N days."
        )
    )
    parser.add_argument(
        "--days", type=int, default=DAYS_BACK, metavar="N",
        help=f"Posted window for documents (default: {DAYS_BACK})",
    )
    parser.add_argument(
        "--video-lookback", type=int, default=VIDEO_LOOKBACK, metavar="N",
        help=(
            f"Only check videos for meetings held within N days "
            f"(default: {VIDEO_LOOKBACK}); upload_date is still filtered by --days"
        ),
    )
    parser.add_argument(
        "--output-dir", default=OUTPUT_DIR, metavar="DIR",
        help=f"Destination directory (default: {OUTPUT_DIR})",
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="List matching items without downloading",
    )
    parser.add_argument(
        "--no-video", action="store_true",
        help="Skip Vimeo recordings",
    )
    parser.add_argument(
        "--video-only", action="store_true",
        help="Download only Vimeo recordings",
    )
    args = parser.parse_args()

    do_docs  = not args.video_only
    do_video = not args.no_video

    today     = datetime.date.today()
    cutoff    = today - datetime.timedelta(days=args.days)
    vid_cutoff = today - datetime.timedelta(days=args.video_lookback)

    print(f"Posted window      : {cutoff} to {today}")
    print(f"Video meeting window: {vid_cutoff} to {today} (upload filtered by posted window)")
    print(f"Source             : {AGENDA_URL}")
    if not args.dry_run:
        print(f"Output dir         : {args.output_dir}")
    print()

    # ------------------------------------------------------------------ #
    # Phase 1: fetch and parse                                             #
    # ------------------------------------------------------------------ #

    html = fetch_html(AGENDA_URL)
    if not html:
        print("ERROR: Could not fetch Agenda Center.", file=sys.stderr)
        sys.exit(1)

    items = parse_agenda_center(html)
    print(f"Total meeting rows found: {len(items)}")
    print()

    # ------------------------------------------------------------------ #
    # Phase 2: filter documents                                            #
    # ------------------------------------------------------------------ #

    doc_queue = []
    if do_docs:
        for item in items:
            if not item["posted_date"] or item["posted_date"] < cutoff:
                continue
            if item["agenda_url"]:
                doc_queue.append({
                    "doc_type":    "agenda",
                    "board":       item["board"],
                    "meeting_date": item["meeting_date"],
                    "posted_date": item["posted_date"],
                    "url":         item["agenda_url"],
                })
            if item["minutes_url"]:
                doc_queue.append({
                    "doc_type":    "minutes",
                    "board":       item["board"],
                    "meeting_date": item["meeting_date"],
                    "posted_date": item["posted_date"],
                    "url":         item["minutes_url"],
                })

    # ------------------------------------------------------------------ #
    # Phase 3: filter videos via oEmbed                                    #
    # ------------------------------------------------------------------ #

    vid_queue = []
    if do_video:
        vid_candidates = [
            item for item in items
            if item["vimeo_url"]
            and item["meeting_date"]
            and item["meeting_date"] >= vid_cutoff
        ]
        if vid_candidates:
            print(f"Checking oEmbed for {len(vid_candidates)} recent Vimeo link(s)...")
        vid_counters: dict = {}
        for item in vid_candidates:
            ud, title = vimeo_upload_date(item["vimeo_url"])
            time.sleep(OEMBED_DELAY)
            if ud is None or ud < cutoff:
                continue
            key = (item["board"], ud)
            vid_counters[key] = vid_counters.get(key, 0) + 1
            vid_queue.append({
                "board":       item["board"],
                "meeting_date": item["meeting_date"],
                "vimeo_url":   item["vimeo_url"],
                "upload_date": ud,
                "title":       title,
                "counter":     vid_counters[key] - 1,
            })

    total = len(doc_queue) + len(vid_queue)

    print(f"{len(doc_queue)} document(s) and {len(vid_queue)} video(s) "
          f"posted within {args.days} day(s).")

    if total == 0:
        print("No items found within the date window.")
        return

    # ------------------------------------------------------------------ #
    # Phase 4: report or download                                          #
    # ------------------------------------------------------------------ #

    if args.dry_run:
        if doc_queue:
            print()
            print(f"{'Board':<45} {'Meeting':<12} {'Posted':<12} Type")
            print("-" * 78)
            for d in doc_queue:
                meet = d["meeting_date"].strftime("%Y-%m-%d") if d["meeting_date"] else "unknown"
                print(
                    f"{d['board'][:44]:<45} {meet:<12} "
                    f"{d['posted_date']!s:<12} {d['doc_type']}"
                )
        if vid_queue:
            print()
            print(f"{'Board':<45} {'Meeting':<12} {'Uploaded':<12} Title")
            print("-" * 78)
            for v in vid_queue:
                meet = v["meeting_date"].strftime("%Y-%m-%d") if v["meeting_date"] else "unknown"
                print(
                    f"{v['board'][:44]:<45} {meet:<12} "
                    f"{v['upload_date']!s:<12} {v['title']}"
                )
        print(f"\n{total} item(s). Re-run without --dry-run to download.")
        return

    os.makedirs(args.output_dir, exist_ok=True)
    log_path   = os.path.join(args.output_dir, "download-log.txt")
    log_lines  = []
    downloaded = skipped = failed = 0

    for d in doc_queue:
        dest  = make_pdf_dest(
            d["doc_type"], d["board"], d["meeting_date"], args.output_dir
        )
        label = os.path.basename(dest)
        if os.path.exists(dest):
            print(f"  skip (exists)  {label}")
            skipped += 1
            continue
        meet = d["meeting_date"].strftime("%Y-%m-%d") if d["meeting_date"] else "unknown"
        print(f"  [posted {d['posted_date']}] {d['board']} — meeting {meet} — {d['doc_type']}")
        print(f"  downloading    {label}")
        if download_pdf(d["url"], dest):
            downloaded += 1
            log_lines.append(f"{datetime.datetime.now().isoformat()}  OK       {dest}")
        else:
            failed += 1
            log_lines.append(
                f"{datetime.datetime.now().isoformat()}  FAILED   {BASE_URL + d['url']}"
            )
            if os.path.exists(dest):
                os.remove(dest)
        time.sleep(DOWNLOAD_DELAY)

    for v in vid_queue:
        dest  = make_video_dest(
            v["board"], v["upload_date"], args.output_dir, v["counter"]
        )
        label = os.path.basename(dest)
        if os.path.exists(dest):
            print(f"  skip (exists)  {label}")
            skipped += 1
            continue
        meet = v["meeting_date"].strftime("%Y-%m-%d") if v["meeting_date"] else "unknown"
        print(f"  [uploaded {v['upload_date']}] {v['board']} — meeting {meet}")
        print(f"  downloading    {label}")
        print(f"  source URL:    {v['vimeo_url']}")
        if download_vimeo(v["vimeo_url"], dest):
            downloaded += 1
            log_lines.append(f"{datetime.datetime.now().isoformat()}  OK       {dest}")
        else:
            failed += 1
            log_lines.append(
                f"{datetime.datetime.now().isoformat()}  FAILED   {v['vimeo_url']}"
            )
            if os.path.exists(dest):
                os.remove(dest)
        time.sleep(DOWNLOAD_DELAY)

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
#    python3 scripts/download-new-hope-agendas.py --dry-run
#
# 2. PDFs only (skip video):
#    python3 scripts/download-new-hope-agendas.py --no-video
#
# 3. Video only:
#    python3 scripts/download-new-hope-agendas.py --video-only
#
# 4. Widen lookback:
#    python3 scripts/download-new-hope-agendas.py --days 7
#
# 5. Widen the video candidate window:
#    python3 scripts/download-new-hope-agendas.py --video-lookback 21
