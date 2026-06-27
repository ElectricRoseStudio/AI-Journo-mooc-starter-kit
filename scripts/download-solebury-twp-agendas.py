#!/usr/bin/env python3
# download-solebury-twp-agendas.py
# Download meeting agendas, minutes, and video recordings from Solebury
# Township, PA (soleburytwp.org) posted in the last N days.
#
# USAGE:
#   python3 scripts/download-solebury-twp-agendas.py [options]
#
# REQUIREMENTS:
#   - Python 3.6+  (stdlib only for PDFs/agendas)
#   - yt-dlp       (for Vimeo video: pip install yt-dlp)
#   - Internet connection
#
# WHAT IT DOES:
#   Agendas:
#     1. Fetches https://www.soleburytwp.org/ and extracts 13 Google Drive
#        file links (one per board/committee) from the "Current Agendas"
#        section.
#     2. HEAD-checks each Drive file via the public download URL for
#        Last-Modified and downloads agendas posted within --days.
#
#   Minutes:
#     3. Fetches each of the 13 board/committee pages and collects PDF links
#        whose path prefix matches that board (e.g. minutes-plan/ for Planning).
#     4. HEAD-checks the most recent PDFs for Last-Modified and downloads
#        those posted within --days.
#
#   Videos:
#     5. Collects Vimeo video IDs from each board page.
#     6. Uses the Vimeo oEmbed API (no auth required) to read upload_date.
#     7. Downloads videos uploaded within --days using yt-dlp.
#
# SITE STRUCTURE:
#   Base:      https://www.soleburytwp.org
#   Agendas:   /  → Google Drive links (13 boards)
#   BOS:       /bos-minutes          → minutes/*, Vimeo
#   BOA:       /board-of-auditors    → minutes-boa/*
#   CPROS:     /comprehensive-park-open-space-plan-committee → minutes-cpros/*, Vimeo
#   CPC:       /comprehensive-plan-committee → minutes-cpc/*, Vimeo
#   EAC:       /environmental-advisory-council → minutes-eac/*, Vimeo
#   SFC:       /solebury-farm-committee → minutes-sfc/*, Vimeo
#   HARB:      /historical-architectural-review-board → minutes-harb/*, Vimeo
#   HRC:       /human-relations-commission → minutes-hrc/*
#   LPC:       /land-preservation-committee → minutes-lpc/*, Vimeo
#   PRB:       /park-recreation-board → minutes-rec/*, Vimeo
#   PLAN:      /planning              → minutes-plan/*, Vimeo
#   SSC:       /sustainability-subcommittee → minutes-ssc/*, Vimeo
#   ZHB:       /zoning-hearing-board  → Vimeo (no PDF minutes)
#
# NOTE: Google Drive files are public "anyone with link" shares. Last-Modified
#   on the uc?export=download URL is the upload timestamp.
#
# NOTE: Vimeo upload_date is available from the oEmbed API at
#   https://vimeo.com/api/oembed.json?url=https://vimeo.com/{ID}
#   No API key required for publicly shared videos.
#
# NOTE: Vimeo download uses yt-dlp. Google Drive agendas and minutes PDFs
#   are downloaded directly via HTTP.

import argparse
import datetime
import email.utils
import html as htmllib
import json
import os
import re
import subprocess
import sys
import time
import urllib.error
import urllib.request

# --- Configuration ---
BASE_URL   = "https://www.soleburytwp.org"
OUTPUT_DIR = "beat-archive/solebury-twp-agendas"
DAYS_BACK  = 3
MAX_PDF_CANDIDATES  = 12   # per board, most-recent-first list
MAX_VID_CANDIDATES  = 5    # per board, most-recent-first list

PAGE_DELAY     = 0.5
HEAD_DELAY     = 0.25
OEMBED_DELAY   = 0.25
DOWNLOAD_DELAY = 0.8

UA = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36"
)

BOARD_PAGES = [
    {"label": "Board of Supervisors",
     "path": "bos-minutes",          "pdf_prefix": "minutes/"},
    {"label": "Board of Auditors",
     "path": "board-of-auditors",    "pdf_prefix": "minutes-boa/"},
    {"label": "CPROS Committee",
     "path": "comprehensive-park-open-space-plan-committee",
                                     "pdf_prefix": "minutes-cpros/"},
    {"label": "Comprehensive Plan Committee",
     "path": "comprehensive-plan-committee",
                                     "pdf_prefix": "minutes-cpc/"},
    {"label": "Environmental Advisory Council",
     "path": "environmental-advisory-council",
                                     "pdf_prefix": "minutes-eac/"},
    {"label": "Solebury Farm Committee",
     "path": "solebury-farm-committee",
                                     "pdf_prefix": "minutes-sfc/"},
    {"label": "Historical Architectural Review Board",
     "path": "historical-architectural-review-board",
                                     "pdf_prefix": "minutes-harb/"},
    {"label": "Human Relations Commission",
     "path": "human-relations-commission",
                                     "pdf_prefix": "minutes-hrc/"},
    {"label": "Land Preservation Committee",
     "path": "land-preservation-committee",
                                     "pdf_prefix": "minutes-lpc/"},
    {"label": "Parks & Recreation Board",
     "path": "park-recreation-board","pdf_prefix": "minutes-rec/"},
    {"label": "Planning Commission",
     "path": "planning",             "pdf_prefix": "minutes-plan/"},
    {"label": "Sustainability Subcommittee",
     "path": "sustainability-subcommittee",
                                     "pdf_prefix": "minutes-ssc/"},
    {"label": "Zoning Hearing Board",
     "path": "zoning-hearing-board", "pdf_prefix": None},
]

_DRIVE_RE = re.compile(
    r'href="(https://drive\.google\.com/file/d/([A-Za-z0-9_-]+)/[^"]*)"[^>]*>([^<]+)</a>',
    re.IGNORECASE,
)
_VIMEO_RE = re.compile(r'vimeo\.com/(\d+)', re.IGNORECASE)


# --- HTTP helpers ---

def fetch_html(url):
    req = urllib.request.Request(
        url, headers={"User-Agent": UA, "Accept": "text/html,*/*"}
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            charset = r.headers.get_content_charset() or "utf-8"
            return r.read().decode(charset, errors="replace")
    except urllib.error.HTTPError as e:
        if e.code != 404:
            print(f"  HTTP {e.code} — {url}", file=sys.stderr)
        return None
    except urllib.error.URLError as e:
        print(f"  ERROR fetching {url}: {e}", file=sys.stderr)
        return None


def head_last_modified(url):
    req = urllib.request.Request(
        url, headers={"User-Agent": UA, "Accept": "*/*"}, method="HEAD"
    )
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            lm = r.headers.get("Last-Modified")
            if lm:
                return email.utils.parsedate_to_datetime(lm).date()
    except Exception:
        pass
    return None


def vimeo_upload_date(video_id):
    """Use oEmbed (no auth) to get upload_date for a public Vimeo video."""
    url = f"https://vimeo.com/api/oembed.json?url=https://vimeo.com/{video_id}"
    req = urllib.request.Request(
        url, headers={"User-Agent": UA, "Accept": "application/json"}
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            d = json.loads(r.read())
        ud = d.get("upload_date", "")
        title = d.get("title", "")
        if ud:
            return datetime.date.fromisoformat(ud[:10]), title
    except Exception as e:
        print(f"  WARNING: oEmbed failed for vimeo/{video_id}: {e}", file=sys.stderr)
    return None, ""


def download_pdf(url, dest_path):
    req = urllib.request.Request(
        url, headers={"User-Agent": UA, "Accept": "application/pdf,*/*"}
    )
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            with open(dest_path, "wb") as f:
                f.write(r.read())
        return True
    except Exception as e:
        print(f"  WARNING: {e}", file=sys.stderr)
        return False


def download_vimeo(video_id, dest_path):
    watch_url = f"https://vimeo.com/{video_id}"
    cmd = [
        "yt-dlp", "--no-playlist",
        "-f", "bestvideo+bestaudio/best",
        "--merge-output-format", "mp4",
        "-o", dest_path,
        "--no-overwrites", "--quiet", "--no-warnings",
        watch_url,
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

def extract_drive_agendas(html_text):
    """Return list of {file_id, board_label, download_url} from the homepage."""
    items = []
    seen  = set()
    for m in _DRIVE_RE.finditer(html_text):
        file_id     = m.group(2)
        board_label = htmllib.unescape(m.group(3)).strip()
        if file_id in seen:
            continue
        seen.add(file_id)
        items.append({
            "file_id":      file_id,
            "board_label":  board_label,
            "download_url": f"https://drive.google.com/uc?export=download&id={file_id}",
        })
    return items


def extract_board_content(html_text, pdf_prefix, max_pdfs, max_vids):
    """
    Return (pdf_urls, vimeo_ids) from a board page.
    Takes only the most-recent max_pdfs PDFs and max_vids Vimeo IDs.
    """
    pdf_urls = []
    if pdf_prefix:
        pattern = re.compile(
            rf'href="({re.escape(pdf_prefix)}[^"]+\.pdf)"', re.IGNORECASE
        )
        seen = set()
        for m in pattern.finditer(html_text):
            rel = m.group(1)
            if rel not in seen:
                seen.add(rel)
                pdf_urls.append(f"{BASE_URL}/{rel}")
            if len(pdf_urls) >= max_pdfs:
                break

    seen_v = set()
    for m in _VIMEO_RE.finditer(html_text):
        seen_v.add(m.group(1))
    # Sort by numeric ID descending so we check the most recently uploaded first
    vimeo_ids = sorted(seen_v, key=lambda x: int(x), reverse=True)[:max_vids]

    return pdf_urls, vimeo_ids


# --- File naming ---

def slugify(text, max_len=50):
    text = text.lower().strip()
    text = re.sub(r"[/\\&]", "-", text)
    text = re.sub(r"[^\w\s-]", "", text)
    text = re.sub(r"[\s_]+", "-", text)
    text = re.sub(r"-{2,}", "-", text)
    return text.strip("-")[:max_len]


def make_pdf_dest(doc_type, board_label, date_posted, output_dir, counter=0):
    month_dir = os.path.join(output_dir, date_posted.strftime("%Y-%m"))
    os.makedirs(month_dir, exist_ok=True)
    suffix = f"-{counter}" if counter > 0 else ""
    fname = (
        f"{date_posted.strftime('%Y-%m-%d')}-{slugify(board_label)}"
        f"-{doc_type}{suffix}.pdf"
    )
    return os.path.join(month_dir, fname)


def make_video_dest(board_label, upload_date, output_dir, counter=0):
    month_dir = os.path.join(output_dir, upload_date.strftime("%Y-%m"))
    os.makedirs(month_dir, exist_ok=True)
    suffix = f"-{counter}" if counter > 0 else ""
    fname = (
        f"{upload_date.strftime('%Y-%m-%d')}-{slugify(board_label)}"
        f"-video{suffix}.mp4"
    )
    return os.path.join(month_dir, fname)


# --- Main ---

def main():
    parser = argparse.ArgumentParser(
        description=(
            "Download Solebury Township PA meeting agendas, minutes, and "
            "video recordings posted in the past N days."
        )
    )
    parser.add_argument(
        "--days", type=int, default=DAYS_BACK, metavar="N",
        help=f"Look back N days for posted documents (default: {DAYS_BACK})",
    )
    parser.add_argument(
        "--output-dir", default=OUTPUT_DIR, metavar="DIR",
        help=f"Destination directory (default: {OUTPUT_DIR})",
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="List matching documents without downloading",
    )
    parser.add_argument(
        "--no-video", action="store_true",
        help="Skip Vimeo recordings (PDFs and agendas only)",
    )
    parser.add_argument(
        "--video-only", action="store_true",
        help="Download only Vimeo recordings",
    )
    args = parser.parse_args()

    do_docs  = not args.video_only
    do_video = not args.no_video

    today  = datetime.date.today()
    cutoff = today - datetime.timedelta(days=args.days)

    print(f"Posted window : {cutoff} to {today}")
    print(f"Base URL      : {BASE_URL}")
    if not args.dry_run:
        print(f"Output dir    : {args.output_dir}")
    print()

    confirmed_docs  = []   # {doc_type, board_label, download_url, last_modified, counter}
    confirmed_vids  = []   # {board_label, video_id, upload_date, title, counter}
    doc_counters:  dict = {}
    vid_counters:  dict = {}

    # ------------------------------------------------------------------ #
    # Phase 1: agendas from homepage (Google Drive)                        #
    # ------------------------------------------------------------------ #

    if do_docs:
        print(f"Fetching {BASE_URL}/ (agendas) ...")
        home_html = fetch_html(BASE_URL + "/")
        if not home_html:
            print("ERROR: Could not fetch homepage.", file=sys.stderr)
            sys.exit(1)
        drive_agendas = extract_drive_agendas(home_html)
        print(f"  Drive agenda links: {len(drive_agendas)}")
        print("  HEAD-checking Last-Modified...")
        for cand in drive_agendas:
            lm = head_last_modified(cand["download_url"])
            time.sleep(HEAD_DELAY)
            if lm is None or lm < cutoff:
                continue
            key = (cand["board_label"], "agenda", lm)
            doc_counters[key] = doc_counters.get(key, 0) + 1
            confirmed_docs.append({
                "doc_type":     "agenda",
                "board_label":  cand["board_label"],
                "download_url": cand["download_url"],
                "last_modified": lm,
                "counter":      doc_counters[key] - 1,
            })
        print()

    # ------------------------------------------------------------------ #
    # Phase 2: board pages — minutes PDFs + Vimeo                         #
    # ------------------------------------------------------------------ #

    for board in BOARD_PAGES:
        url      = f"{BASE_URL}/{board['path']}"
        label    = board["label"]
        prefix   = board["pdf_prefix"]

        print(f"Fetching {url} ...")
        html = fetch_html(url)
        if not html:
            continue
        time.sleep(PAGE_DELAY)

        pdf_urls, vimeo_ids = extract_board_content(
            html, prefix, MAX_PDF_CANDIDATES, MAX_VID_CANDIDATES
        )
        print(f"  PDFs: {len(pdf_urls)}  Vimeo: {len(vimeo_ids)}")

        if do_docs:
            for pdf_url in pdf_urls:
                lm = head_last_modified(pdf_url)
                time.sleep(HEAD_DELAY)
                if lm is None or lm < cutoff:
                    continue
                key = (label, "minutes", lm)
                doc_counters[key] = doc_counters.get(key, 0) + 1
                confirmed_docs.append({
                    "doc_type":     "minutes",
                    "board_label":  label,
                    "download_url": pdf_url,
                    "last_modified": lm,
                    "counter":      doc_counters[key] - 1,
                })

        if do_video:
            for vid_id in vimeo_ids:
                ud, title = vimeo_upload_date(vid_id)
                time.sleep(OEMBED_DELAY)
                if ud is None or ud < cutoff:
                    continue
                key = (label, ud)
                vid_counters[key] = vid_counters.get(key, 0) + 1
                confirmed_vids.append({
                    "board_label": label,
                    "video_id":    vid_id,
                    "upload_date": ud,
                    "title":       title,
                    "counter":     vid_counters[key] - 1,
                })

    # ------------------------------------------------------------------ #
    # Phase 3: report                                                       #
    # ------------------------------------------------------------------ #

    confirmed_docs.sort(key=lambda x: x["last_modified"], reverse=True)
    confirmed_vids.sort(key=lambda x: x["upload_date"], reverse=True)
    total = len(confirmed_docs) + len(confirmed_vids)

    print()
    print(f"{len(confirmed_docs)} document(s) and {len(confirmed_vids)} video(s) "
          f"posted within {args.days} day(s).")

    if total == 0:
        print("No items found within the date window.")
        return

    if args.dry_run:
        if confirmed_docs:
            print()
            print(f"{'Board/Committee':<50} {'Posted':<12} Type")
            print("-" * 74)
            for c in confirmed_docs:
                print(f"{c['board_label'][:49]:<50} {c['last_modified']!s:<12} {c['doc_type']}")
        if confirmed_vids:
            print()
            print(f"{'Board/Committee':<50} {'Uploaded':<12} Title")
            print("-" * 74)
            for c in confirmed_vids:
                print(f"{c['board_label'][:49]:<50} {c['upload_date']!s:<12} {c['title']}")
        print(f"\n{total} item(s). Re-run without --dry-run to download.")
        return

    # ------------------------------------------------------------------ #
    # Phase 4: download                                                     #
    # ------------------------------------------------------------------ #

    os.makedirs(args.output_dir, exist_ok=True)
    log_path   = os.path.join(args.output_dir, "download-log.txt")
    log_lines  = []
    downloaded = skipped = failed = 0

    for c in confirmed_docs:
        dest  = make_pdf_dest(
            c["doc_type"], c["board_label"],
            c["last_modified"], args.output_dir, c["counter"]
        )
        label = os.path.basename(dest)
        if os.path.exists(dest):
            print(f"  skip (exists)  {label}")
            skipped += 1
            continue
        print(f"  [posted {c['last_modified']}] {c['board_label']} — {c['doc_type']}")
        print(f"  downloading    {label}")
        if download_pdf(c["download_url"], dest):
            downloaded += 1
            log_lines.append(f"{datetime.datetime.now().isoformat()}  OK       {dest}")
        else:
            failed += 1
            log_lines.append(f"{datetime.datetime.now().isoformat()}  FAILED   {c['download_url']}")
            if os.path.exists(dest):
                os.remove(dest)
        time.sleep(DOWNLOAD_DELAY)

    for c in confirmed_vids:
        dest  = make_video_dest(
            c["board_label"], c["upload_date"], args.output_dir, c["counter"]
        )
        label = os.path.basename(dest)
        if os.path.exists(dest):
            print(f"  skip (exists)  {label}")
            skipped += 1
            continue
        print(f"  [uploaded {c['upload_date']}] {c['board_label']} — {c['title']}")
        print(f"  downloading    {label}")
        print(f"  source URL:    https://vimeo.com/{c['video_id']}")
        if download_vimeo(c["video_id"], dest):
            downloaded += 1
            log_lines.append(f"{datetime.datetime.now().isoformat()}  OK       {dest}")
        else:
            failed += 1
            log_lines.append(
                f"{datetime.datetime.now().isoformat()}  FAILED   vimeo/{c['video_id']}"
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
#    python3 scripts/download-solebury-twp-agendas.py --dry-run
#
# 2. PDFs only (skip video):
#    python3 scripts/download-solebury-twp-agendas.py --no-video
#
# 3. Video only:
#    python3 scripts/download-solebury-twp-agendas.py --video-only
#
# 4. Widen the lookback window:
#    python3 scripts/download-solebury-twp-agendas.py --days 7
#
# BOARD PAGES (13, all at soleburytwp.org/{path}):
#   bos-minutes, board-of-auditors,
#   comprehensive-park-open-space-plan-committee,
#   comprehensive-plan-committee, environmental-advisory-council,
#   solebury-farm-committee, historical-architectural-review-board,
#   human-relations-commission, land-preservation-committee,
#   park-recreation-board, planning, sustainability-subcommittee,
#   zoning-hearing-board
