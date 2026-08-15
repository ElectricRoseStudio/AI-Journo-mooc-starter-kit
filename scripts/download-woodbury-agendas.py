#!/usr/bin/env python3
# download-woodbury-agendas.py
# Download municipal meeting agendas, minutes, audio, and video recordings
# from the Woodbury, CT Agenda Center for meetings within the past N days
# (and up to M days ahead).
#
# USAGE:
#   python3 scripts/download-woodbury-agendas.py [options]
#
# REQUIREMENTS:
#   - Python 3.6+  (no third-party packages needed for docs)
#   - yt-dlp       (for audio/video: pip install yt-dlp)
#   - Internet connection
#
# WHAT IT DOES:
#   Documents (default or --docs-only):
#     1. Fetches https://woodburyct.org/agendas_minutes to discover every
#        board's own GovOffice page (SEC={guid})
#     2. For each board, discovers its "<Year> Meeting Agendas & Minutes"
#        sub-page(s) (DE={guid}) for the current year (and prior year, if
#        the lookback window crosses a year boundary)
#     3. Parses each table row on that page — a hand-maintained HTML table
#        with columns for Agenda / Hybrid Link / Minutes / Audio / Video —
#        pulling every link out of the row and classifying it by URL
#        (archive.org -> audio, youtube.com -> video, *.pdf with "agenda"
#        or "minutes" in the name -> agenda/minutes)
#     4. Parses the meeting date out of whichever link in the row has a
#        recognizable M-D-YY(YY) or M/D/YYYY pattern, and filters to the
#        lookback/lookahead window
#     5. Downloads matching PDFs to beat-archive/woodbury-agendas/YYYY-MM/
#
#   Audio/video (--include-media or --media-only; off by default):
#     6. Downloads matching audio (archive.org) and video (YouTube)
#        recordings with yt-dlp — both confirmed directly to be real,
#        yt-dlp-resolvable per-meeting links (not a channel search; these
#        are explicit meeting-specific links maintained in the same
#        table as the agenda/minutes PDFs) before writing this script.
#
# SITE STRUCTURE — GovOffice (govoffice3.com), NOT CivicPlus/EvoGov/
#   Legistar/WordPress:
#   Hub:         https://woodburyct.org/agendas_minutes
#   Board pages: https://woodburyct.govoffice3.com/index.asp?SEC={guid}&Type=B_BASIC
#   Year pages:  https://woodburyct.govoffice3.com/index.asp?SEC={guid}&DE={guid}
#                  (linked from the board page as "<Year> Meeting
#                  Agendas & Minutes - <Board>")
#   Files:       https://woodburyct.govoffice3.com/vertical/Sites/{siteguid}/
#                  uploads/<M-D-YY(YY)>_<Board>_Agenda|Minutes....pdf
#   Audio:       https://archive.org/details/<slug> (yt-dlp-downloadable)
#   Video:       https://youtube.com/live/<id>?feature=share (individual
#                  per-meeting links, not a channel — yt-dlp-downloadable)
#
#   Every board's year page is a single manually-edited HTML table (no
#   API, no consistent machine-readable structure), so this script parses
#   it row-by-row and classifies whatever links are actually present
#   rather than assuming a fixed column layout — some rows have only an
#   agenda, some have agenda+minutes+multi-part audio, video links are
#   sparse and vary by board/year.

import argparse
import datetime
import glob
import html
import os
import re
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

# --- Configuration ---
HUB_URL = "https://woodburyct.org/agendas_minutes"
BOARD_BASE = "https://woodburyct.govoffice3.com/index.asp"
OUTPUT_DIR = "beat-archive/woodbury-agendas"
DAYS_BACK = 4
DAYS_AHEAD = 7
DELAY_SECONDS = 0.5

YT_DLP_NODE = "node:/home/richkirby/.local/bin/yt-dlp-node"  # yt-dlp needs Node 22+; symlink kept current by scripts/update-yt-dlp-node.sh

UA = "Woodbury-CT-Agendas-Downloader/1.0 (journalism research)"

_BOARD_LINK_RE = re.compile(
    r'<a[^>]+href="[^"]*index\.asp\?SEC=([0-9A-Fa-f-]+)&(?:amp;)?Type=B_BASIC"[^>]*>((?:(?!</a>).)*)</a>',
    re.S,
)
_YEAR_LINK_RE = re.compile(
    r'<a[^>]+href="[^"]*index\.asp\?SEC=[0-9A-Fa-f-]+&(?:amp;)?DE=([0-9A-Fa-f-]+)"[^>]*>\s*(\d{4})\s+Meeting Agendas',
)
_ROW_RE = re.compile(r"<tr[^>]*>(.*?)</tr>", re.S | re.I)
_LINK_RE = re.compile(r'<a[^>]+href="([^"]+)"[^>]*>((?:(?!</a>).)*)</a>', re.S)
_DATE_RE = re.compile(r"\b(\d{1,2})[/-](\d{1,2})[/-](\d{2,4})\b")


# --- HTTP helpers ---

def fetch_html(url):
    req = urllib.request.Request(
        url, headers={"User-Agent": UA, "Accept": "text/html,application/xhtml+xml,*/*"},
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            charset = r.headers.get_content_charset() or "utf-8"
            return r.read().decode(charset, errors="replace")
    except Exception as e:
        print(f"  ERROR fetching {url}: {e}", file=sys.stderr)
        return None


def download_pdf(url, dest_path):
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "application/pdf,*/*"})
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            with open(dest_path, "wb") as f:
                f.write(r.read())
        return True
    except Exception as e:
        print(f"  WARNING: {e}", file=sys.stderr)
        return False


# --- (1) Board + year discovery ---

def discover_boards():
    hub_html = fetch_html(HUB_URL)
    if not hub_html:
        return []
    boards = {}
    for m in _BOARD_LINK_RE.finditer(hub_html):
        sec = m.group(1)
        name = html.unescape(re.sub(r"<[^>]+>", "", m.group(2)).strip())
        if name and sec not in boards:
            boards[sec] = name
    return sorted(boards.items(), key=lambda kv: kv[1])


def discover_year_pages(sec, years_needed):
    board_url = f"{BOARD_BASE}?SEC={sec}&Type=B_BASIC"
    page_html = fetch_html(board_url)
    if not page_html:
        return {}
    years = {}
    for m in _YEAR_LINK_RE.finditer(page_html):
        de, year = m.group(1), int(m.group(2))
        if year in years_needed:
            years[year] = de
    return years


# --- (2) Row parsing ---

def parse_row_date(links):
    for href, text in links:
        for source in (text, href):
            m = _DATE_RE.search(source)
            if not m:
                continue
            mm, dd, yy = int(m.group(1)), int(m.group(2)), m.group(3)
            yyyy = int(yy) if len(yy) == 4 else 2000 + int(yy)
            try:
                return datetime.date(yyyy, mm, dd)
            except ValueError:
                continue
    return None


def classify_link(href, text):
    lower_href = href.lower()
    lower_text = text.lower()
    if "archive.org" in lower_href:
        return "audio"
    if "youtube.com" in lower_href or "youtu.be" in lower_href:
        return "video"
    if lower_href.endswith(".pdf"):
        if "agenda" in lower_href or "agenda" in lower_text:
            return "agenda"
        if "minutes" in lower_href or "minutes" in lower_text:
            return "minutes"
        return "document"
    return None


def collect_board_items(board_name, sec, de, cutoff, future_limit):
    page_html = fetch_html(f"{BOARD_BASE}?SEC={sec}&DE={de}")
    if not page_html:
        return []
    items = []
    for row_m in _ROW_RE.finditer(page_html):
        row_html = row_m.group(1)
        links = [(m.group(1), html.unescape(re.sub(r"<[^>]+>", "", m.group(2)).strip()))
                 for m in _LINK_RE.finditer(row_html)]
        if not links:
            continue
        meeting_date = parse_row_date(links)
        if not meeting_date or not (cutoff <= meeting_date <= future_limit):
            continue
        seen_hrefs = set()
        for href, text in links:
            if href in seen_hrefs:
                continue
            kind = classify_link(href, text)
            if not kind:
                continue
            seen_hrefs.add(href)
            items.append({
                "board": board_name, "meeting_date": meeting_date,
                "kind": kind, "href": href, "text": text,
            })
    return items


# --- (3) Audio/video via yt-dlp ---

def download_media(url, dest_prefix):
    """Download url (archive.org or YouTube) to dest_prefix.<ext> via yt-dlp."""
    cmd = [
        "yt-dlp", "--js-runtimes", YT_DLP_NODE,
        "--no-warnings", "--quiet",
        "-o", dest_prefix + ".%(ext)s",
        url,
    ]
    try:
        result = subprocess.run(cmd, timeout=1800)
        return result.returncode == 0
    except FileNotFoundError:
        print("  ERROR: yt-dlp not found. Install with: pip install yt-dlp", file=sys.stderr)
        return False
    except subprocess.TimeoutExpired:
        print(f"  WARNING: yt-dlp timed out on {url}", file=sys.stderr)
        return False


# --- File naming ---

def slugify(text, max_len=55):
    text = str(text).lower().strip()
    text = re.sub(r"[/\\&]", "-", text)
    text = re.sub(r"[^\w\s-]", "", text)
    text = re.sub(r"[\s_]+", "-", text)
    text = re.sub(r"-{2,}", "-", text)
    return text.strip("-")[:max_len]


def make_prefix(board, kind, meeting_date, output_dir, counter=0):
    date_str = meeting_date.strftime("%Y-%m-%d")
    month_dir = os.path.join(output_dir, meeting_date.strftime("%Y-%m"))
    os.makedirs(month_dir, exist_ok=True)
    suffix = f"-{counter}" if counter else ""
    return os.path.join(month_dir, f"{date_str}-{slugify(board)}-{slugify(kind)}{suffix}")


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
            "Download Woodbury CT municipal agendas, minutes, audio, and video "
            "recordings for meetings within the past N days (and up to M days ahead)."
        )
    )
    parser.add_argument("--days", type=int, default=DAYS_BACK, metavar="N",
                        help=f"Look back N days (default: {DAYS_BACK})")
    parser.add_argument("--ahead", type=int, default=DAYS_AHEAD, metavar="N",
                        help=f"Also include meetings up to N days ahead (default: {DAYS_AHEAD})")
    parser.add_argument("--output-dir", default=OUTPUT_DIR, metavar="DIR",
                        help=f"Destination directory (default: {OUTPUT_DIR})")
    parser.add_argument("--dry-run", action="store_true", help="List matching items without downloading")
    parser.add_argument("--board", metavar="NAME",
                        help="Only process boards whose name contains NAME (case-insensitive)")
    parser.add_argument("--no-minutes", action="store_true", help="Skip minutes")
    parser.add_argument("--no-agendas", action="store_true", help="Skip agendas")
    parser.add_argument("--include-media", action="store_true",
                        help="Also download audio (archive.org) and video (YouTube) recordings")
    parser.add_argument("--media-only", action="store_true", help="Download only audio/video recordings")
    args = parser.parse_args()

    now = datetime.datetime.now()
    if (now.weekday() == 5 and now.hour >= 18) or (now.weekday() == 6 and now.hour < 12):
        print("Skipping — no downloads on Saturday nights or Sunday mornings.")
        sys.exit(0)

    do_docs = not args.media_only
    do_media = args.include_media or args.media_only

    today = datetime.date.today()
    cutoff = today - datetime.timedelta(days=args.days)
    future_limit = today + datetime.timedelta(days=args.ahead)
    board_filter = args.board.lower() if args.board else None
    years_needed = set(range(cutoff.year, future_limit.year + 1))

    print(f"Date window : {cutoff} to {future_limit}")
    print(f"Hub URL     : {HUB_URL}")
    if not args.dry_run:
        print(f"Output dir  : {args.output_dir}")
    print()

    print("Discovering boards...")
    boards = discover_boards()
    if not boards:
        print("ERROR: Could not discover any boards.", file=sys.stderr)
        sys.exit(1)
    if board_filter:
        boards = [(sec, n) for sec, n in boards if board_filter in n.lower()]
    print(f"  {len(boards)} board(s).\n")

    docs, media = [], []
    for sec, board_name in boards:
        year_pages = discover_year_pages(sec, years_needed)
        time.sleep(DELAY_SECONDS)
        board_items = []
        for year, de in year_pages.items():
            board_items.extend(collect_board_items(board_name, sec, de, cutoff, future_limit))
            time.sleep(DELAY_SECONDS)

        for it in board_items:
            if it["kind"] == "agenda" and (not do_docs or args.no_agendas):
                continue
            if it["kind"] == "minutes" and (not do_docs or args.no_minutes):
                continue
            if it["kind"] == "document" and not do_docs:
                continue
            if it["kind"] in ("audio", "video") and not do_media:
                continue
            if it["kind"] in ("agenda", "minutes", "document"):
                docs.append(it)
            else:
                media.append(it)

        if board_items:
            print(f"  {board_name}: {len(board_items)} item(s)")

    print(f"\nDocuments   : {len(docs)} found")
    print(f"Audio/Video : {len(media)} found\n")

    docs.sort(key=lambda x: (x["meeting_date"], x["board"]), reverse=True)
    media.sort(key=lambda x: (x["meeting_date"], x["board"]), reverse=True)
    assign_counters(docs, lambda d: (d["board"], d["meeting_date"], d["kind"]))
    assign_counters(media, lambda d: (d["board"], d["meeting_date"], d["kind"]))

    total = len(docs) + len(media)
    if total == 0:
        print("No items found in the date window.")
        return

    if args.dry_run:
        if docs:
            print(f"{'Board':<40} {'Date':<12} Type")
            print("-" * 65)
            for d in docs:
                print(f"{d['board'][:39]:<40} {d['meeting_date']!s:<12} {d['kind']}")
            print()
        if media:
            print(f"{'Board':<40} {'Date':<12} Media")
            print("-" * 65)
            for m in media:
                print(f"{m['board'][:39]:<40} {m['meeting_date']!s:<12} {m['kind']} — {m['text'][:40]}")
            print()
        print(f"{total} item(s). Re-run without --dry-run to download.")
        return

    os.makedirs(args.output_dir, exist_ok=True)
    log_path = os.path.join(args.output_dir, "download-log.txt")
    log_lines = []
    downloaded = skipped = failed = 0

    for d in docs:
        prefix = make_prefix(d["board"], d["kind"], d["meeting_date"], args.output_dir, counter=d["counter"])
        dest = prefix + ".pdf"
        label = os.path.basename(dest)
        if os.path.exists(dest):
            print(f"  skip (exists)  {label}")
            skipped += 1
            continue
        print(f"  [{d['meeting_date']}] {d['board']} — {d['kind']}")
        print(f"  downloading    {label}")
        if download_pdf(d["href"], dest):
            downloaded += 1
            log_lines.append(f"{datetime.datetime.now().isoformat()}  OK       {dest}")
        else:
            failed += 1
            log_lines.append(f"{datetime.datetime.now().isoformat()}  FAILED   {d['href']}")
            if os.path.exists(dest):
                os.remove(dest)
        time.sleep(DELAY_SECONDS)

    for m in media:
        prefix = make_prefix(m["board"], m["kind"], m["meeting_date"], args.output_dir, counter=m["counter"])
        if glob.glob(prefix + ".*"):
            print(f"  skip (exists)  {os.path.basename(prefix)}.*")
            skipped += 1
            continue
        print(f"  [{m['meeting_date']}] {m['board']} — {m['kind']} ({m['text'][:40]})")
        print(f"  downloading    {os.path.basename(prefix)}.*")
        if download_media(m["href"], prefix):
            downloaded += 1
            log_lines.append(f"{datetime.datetime.now().isoformat()}  OK       {prefix}.*")
        else:
            failed += 1
            log_lines.append(f"{datetime.datetime.now().isoformat()}  FAILED   {m['href']}")

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
#    python3 scripts/download-woodbury-agendas.py --dry-run
#
# 2. Just Board of Selectmen:
#    python3 scripts/download-woodbury-agendas.py --board "Board of Selectmen"
#
# 3. Docs only (no audio/video — the default; --include-media/--media-only turn it on):
#    python3 scripts/download-woodbury-agendas.py
#
# 4. Audio/video only:
#    python3 scripts/download-woodbury-agendas.py --media-only
#
# 5. Change the lookback window:
#    python3 scripts/download-woodbury-agendas.py --days 14
#
# 6. Run on a schedule (cron — evening):
#    0 21 * * 1-5 cd /path/to/repo && python3 scripts/download-woodbury-agendas.py --include-media
