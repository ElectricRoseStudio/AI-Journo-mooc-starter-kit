#!/usr/bin/env python3
# download-canton-agendas.py
# Download municipal meeting agendas and audio recordings from the Town of Canton, CT.
#
# USAGE:
#   python3 scripts/download-canton-agendas.py [options]
#
# REQUIREMENTS:
#   - Python 3.6+  (no third-party packages needed)
#   - Internet connection
#
# WHAT IT DOES:
#   1. Fetches each board's agenda page on townofcantonct.org (QScend CMS)
#   2. Finds all "feed entries" for the date window (one entry per year/period)
#   3. Fetches each feed entry to extract document/recording links
#   4. For meeting packets: downloads Dropbox shared-folder ZIP archives
#   5. For audio recordings: downloads MP3/MP4 files from AWS S3 (opt-in)
#   6. Saves to beat-archive/canton-agendas/YYYY-MM/
#   7. Appends a download log to beat-archive/canton-agendas/download-log.txt
#
# SITE STRUCTURE (QScend CMS, townofcantonct.org):
#   Agendas index:  /agendas-minutes-meetings
#   Board page:     /{slug}  (e.g. /CC-packets, /audio-selectmen)
#   Feed entry:     /{slug}/?FeedID={id}  (one entry per year or meeting period)
#   Archives:       /{slug}/?Archives=1&ChanID={chanid}
#
# DOCUMENT TYPES:
#   Meeting packets (agendas + supporting docs): Dropbox shared folder links
#     → Downloaded as ZIP files via Dropbox's ?dl=1 parameter
#   Audio/video recordings (meeting "minutes"): AWS S3 direct links
#     → Large files (50–300 MB each); skipped by default; use --include-audio
#
# DATE PARSING:
#   Feed entry titles contain the meeting date in formats like:
#     "05/05/26 - CC Meeting Packet"    → 2026-05-05
#     "04.22.26 BOS Regular Meeting"    → 2026-04-22
#     "1.12.2026 BOF Regular Meeting"   → 2026-01-12
#   Entries labelled only as a year (e.g. "2026") are included if the year
#   falls within the date window.
#
# NOTE: Boards post meeting packets for land-use commissions (PZC, ZBA, CC,
#   IWWA, etc.). Most other boards (Selectmen, Finance, etc.) post audio
#   recordings of meetings instead of written agendas or minutes.

import argparse
import datetime
import html as html_lib
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

# --- Configuration ---
BASE_URL = "https://www.townofcantonct.org"
OUTPUT_DIR = "beat-archive/canton-agendas"
DAYS_BACK = 4
DAYS_AHEAD = 7
PAGE_DELAY = 0.5
DOWNLOAD_DELAY = 1.0

UA = "Canton-CT-Agendas-Downloader/1.0 (journalism research)"

# Boards that post meeting packets as Dropbox shared folders
PACKET_BOARDS = {
    "Aquifer Protection Agency":                "/APA-packets",
    "Conservation Commission":                  "/CC-packets",
    "Canton Center Historic District":          "/CCHD-packets",
    "Collinsville Historic District Commission":"/CHDC-packets",
    "Economic Development Agency":              "/EDA-packets",
    "Inland Wetlands and Watercourses Agency":  "/IWWA-packet",
    "Planning and Zoning Commission":           "/PZC-packet",
    "Zoning Board of Appeals":                  "/ZBA-packet",
    "Temporary POCD Committee":                 "/POCDPackets",
}

# Boards that post audio/video recordings of meetings
AUDIO_BOARDS = {
    "Board of Finance":                                    "/audio-finance",
    "Board of Finance Communications Subcommittee":        "/boards/finance/communications-sub-committee/audio",
    "Board of Selectmen":                                  "/audio-selectmen",
    "Board of Selectmen Public Safety Committee":          "/boards/selectmen/public-safety-subcommittee/audio",
    "Canton Center Historic District Commission":          "/audio-cchdc",
    "Collinsville Historic District Commission":           "/audio-chdc",
    "Inland Wetlands and Watercourses Agency":             "/audio-iwwa",
    "Planning and Zoning Commission":                      "/audio-pzc",
    "Water Pollution Control Authority":                   "/audio-wpca",
    "Zoning Board of Appeals":                             "/audio-zba",
    "Affordable Housing Workshops":                        "/AffordableHousingaudio",
    "Appointment Committee":                               "/Appointment",
    "Aquifer Protection Agency":                           "/audio-APA",
    "Board of Assessment Appeals":                         "/BAAAudio",
    "CIP Subcommittee":                                    "/CIP",
    "Commission on Aging":                                 "/COA-Audio",
    "Conservation Commission":                             "/Conservation",
    "Economic Development Agency":                         "/EDAaudio",
    "Library Board of Trustees":                           "/Library",
    "Pension Committee":                                   "/pensionaudio",
    "P&Z Communications Facilities Sub-Committee":         "/FSC",
    "River Access Study Committee":                        "/River",
    "Temporary Bonding Committee":                         "/temporarybondingcommitteeaudio",
    "Insurance Committee":                                 "/boards/temporary-insurance-committee/audio",
    "Temp POCD Committee":                                 "/audio-POCD",
    "Temp Sustainable Waste Mgt Committee":                "/WasteAudio",
    "Temp Traffic and Pedestrian Safety Committee":        "/TrafficPedAudio",
}

# Patterns to find the news-feed channel ID (<div Data-Id="NNNNN">)
_CHAN_ID_RE = re.compile(r'NEWS_FEED_DISPLAY_TABLE[^>]*Data-Id="(\d+)"', re.I)
# FeedID links within a board page or archive page
_FEED_LINK_RE = re.compile(r'href="(/[^"]+\?FeedID=(\d+))"[^>]*>([^<]+)</a>', re.I)
# Dropbox shared folder links
_DROPBOX_RE = re.compile(r'href="(https://www\.dropbox\.com/[^"]+)"', re.I)
# AWS S3 direct links (audio/video)
_S3_RE = re.compile(r'href="(https://townmeetings\.s3\.[^"]+\.(mp[34]|m4a|wav))"', re.I)
# Date patterns in feed entry titles/link text
_DATE_MDY_RE = re.compile(r'(\d{1,2})[./](\d{1,2})[./](\d{2,4})')


# --- HTTP helpers ---

def fetch_html(url, retries=2):
    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": UA,
            "Accept": "text/html,application/xhtml+xml,*/*",
        },
    )
    for attempt in range(retries + 1):
        try:
            with urllib.request.urlopen(req, timeout=45) as r:
                charset = r.headers.get_content_charset() or "utf-8"
                return r.read().decode(charset, errors="replace")
        except urllib.error.HTTPError as e:
            if e.code == 404:
                return None
            print(f"  HTTP {e.code} fetching {url}", file=sys.stderr)
            if attempt < retries:
                time.sleep(2 * (attempt + 1))
        except (urllib.error.URLError, TimeoutError, OSError) as e:
            print(f"  WARNING (attempt {attempt+1}): {e}", file=sys.stderr)
            if attempt < retries:
                time.sleep(2 * (attempt + 1))
    return None


def download_url(url, dest_path, follow_redirects=True):
    """Download url to dest_path. Returns (ok: bool, content_type: str)."""
    req = urllib.request.Request(
        url,
        headers={"User-Agent": UA, "Accept": "*/*"},
    )
    try:
        with urllib.request.urlopen(req, timeout=300) as r:
            ct = r.headers.get_content_type() or ""
            data = r.read()
        with open(dest_path, "wb") as f:
            f.write(data)
        return True, ct
    except Exception as e:
        print(f"  WARNING: {e}", file=sys.stderr)
        return False, None


def make_dropbox_zip_url(dropbox_url):
    """Convert a Dropbox shared folder URL to a direct zip-download URL."""
    # Replace or add dl=1
    if "dl=0" in dropbox_url:
        return dropbox_url.replace("dl=0", "dl=1")
    elif "dl=1" in dropbox_url:
        return dropbox_url
    elif "?" in dropbox_url:
        return dropbox_url + "&dl=1"
    else:
        return dropbox_url + "?dl=1"


# --- Date parsing ---

def parse_date_from_text(text):
    """
    Extract meeting date from an entry title or link text.
    Handles: MM/DD/YY, MM/DD/YYYY, MM.DD.YY, MM.DD.YYYY
    Returns datetime.date or None.
    """
    for m in _DATE_MDY_RE.finditer(text):
        mm, dd, yy_s = int(m.group(1)), int(m.group(2)), m.group(3)
        yy = int(yy_s)
        yyyy = 2000 + yy if len(yy_s) == 2 else yy
        if 1 <= mm <= 12 and 1 <= dd <= 31 and 2000 <= yyyy <= 2040:
            try:
                return datetime.date(yyyy, mm, dd)
            except ValueError:
                continue
    return None


def parse_year_from_text(text):
    """Extract a 4-digit year from text. Returns int or None."""
    m = re.search(r'\b(20\d{2})\b', text)
    return int(m.group(1)) if m else None


# --- Board page scraping ---

def get_channel_id(html):
    """Extract the QScend news channel ID from a board page."""
    m = _CHAN_ID_RE.search(html)
    return m.group(1) if m else None


def get_feed_ids(html):
    """
    Return list of (path, feed_id, label) tuples from a board page or archive.
    """
    entries = []
    seen = set()
    for m in _FEED_LINK_RE.finditer(html):
        path, fid, label = m.group(1), m.group(2), m.group(3).strip()
        if fid not in seen:
            seen.add(fid)
            entries.append((path, fid, label))
    return entries


def get_links_from_feed_entry(slug, feed_id):
    """
    Fetch a feed entry page and return (dropbox_links, s3_links).
    Each dropbox item: (title_text, dropbox_url)
    Each s3 item: (title_text, s3_url, ext)
    """
    url = f"{BASE_URL}{slug}?FeedID={feed_id}"
    html = fetch_html(url)
    if not html:
        return [], []

    idx = html.find("MAIN CONTENT")
    content = html[idx:] if idx >= 0 else html

    dropbox_links = []
    s3_links = []

    # Find all <a> tags in the content section
    for link_m in re.finditer(
        r'<a\s[^>]*href="([^"]+)"[^>]*>([^<]*)</a>', content, re.I
    ):
        raw_href = link_m.group(1)
        text = link_m.group(2).strip()
        # Decode HTML entities (e.g. &amp; → &)
        href = html_lib.unescape(raw_href)
        # Clean up malformed URLs where the URL is concatenated with itself
        # (data-entry error on some Canton pages, e.g. "url1url1url1")
        if href.count("https://") > 1:
            second = href.index("https://", 8)
            href = href[:second]

        if "dropbox.com" in href.lower():
            dropbox_links.append((text, href))
        elif "s3" in href.lower() and "townmeetings" in href.lower():
            ext = os.path.splitext(href.split("?")[0])[1].lstrip(".").lower()
            s3_links.append((text, href, ext or "mp4"))

    return dropbox_links, s3_links


# --- File naming ---

def slugify(text, max_len=45):
    text = text.lower().strip()
    text = re.sub(r"[&/\\]", "-", text)
    text = re.sub(r"[^\w\s-]", "", text)
    text = re.sub(r"[\s_]+", "-", text)
    text = re.sub(r"-{2,}", "-", text)
    return text.strip("-")[:max_len]


def make_dest_path(doc, output_dir):
    date = doc["date"]
    year = doc.get("year")
    if date:
        month_dir = os.path.join(output_dir, date.strftime("%Y-%m"))
        date_prefix = date.strftime("%Y-%m-%d")
    elif year:
        month_dir = os.path.join(output_dir, str(year))
        date_prefix = str(year)
    else:
        month_dir = os.path.join(output_dir, "undated")
        date_prefix = "undated"

    os.makedirs(month_dir, exist_ok=True)
    board_slug = slugify(doc["board"])
    ext = doc["ext"]
    counter = doc.get("counter", 0)
    suffix = f"-{counter}" if counter > 0 else ""
    return os.path.join(month_dir, f"{date_prefix}_{board_slug}_{doc['type']}{suffix}.{ext}")


# --- Main ---

def main():
    parser = argparse.ArgumentParser(
        description="Download Canton, CT municipal meeting packets and recordings.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--days", type=int, default=DAYS_BACK, metavar="N",
        help=f"Look back N days (default: {DAYS_BACK})",
    )
    parser.add_argument(
        "--ahead", type=int, default=DAYS_AHEAD, metavar="N",
        help=f"Include meetings up to N days ahead (default: {DAYS_AHEAD})",
    )
    parser.add_argument(
        "--all", action="store_true",
        help="Download all available documents (no date limit)",
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
        "--board", metavar="NAME",
        help="Only fetch boards whose name contains NAME (case-insensitive)",
    )
    parser.add_argument(
        "--include-audio", action="store_true",
        help="Also download audio/video recordings (50-300 MB each — use with caution)",
    )
    parser.add_argument(
        "--no-packets", action="store_true",
        help="Skip meeting packets, download recordings only (requires --include-audio)",
    )
    args = parser.parse_args()

    today = datetime.date.today()
    if args.all:
        cutoff = datetime.date(2020, 1, 1)
        future_limit = today + datetime.timedelta(days=DAYS_AHEAD)
    else:
        cutoff = today - datetime.timedelta(days=args.days)
        future_limit = today + datetime.timedelta(days=args.ahead)

    print(f"Date window : {cutoff} to {future_limit}")
    if not args.dry_run:
        print(f"Output dir  : {args.output_dir}")
    print()

    # Build list of boards to process
    all_boards = {}
    if not args.no_packets:
        for name, slug in PACKET_BOARDS.items():
            all_boards[name] = {"slug": slug, "type": "packets"}
    if args.include_audio or args.no_packets:
        for name, slug in AUDIO_BOARDS.items():
            key = f"{name} (audio)" if name in all_boards else name
            all_boards[key] = {"slug": slug, "type": "audio"}

    if args.board:
        flt = args.board.lower()
        all_boards = {k: v for k, v in all_boards.items() if flt in k.lower()}
        if not all_boards:
            print(f"No boards match --board '{args.board}'", file=sys.stderr)
            sys.exit(1)

    # --- Step 1: collect feed entries for each board ---
    all_docs = []

    print(f"Scanning {len(all_boards)} board page(s)...")
    for board_name, board_info in all_boards.items():
        slug = board_info["slug"]
        btype = board_info["type"]
        board_url = f"{BASE_URL}{slug}"

        print(f"  {board_name}...", end=" ", flush=True)
        board_html = fetch_html(board_url)
        if not board_html:
            print("no page")
            time.sleep(PAGE_DELAY)
            continue

        chan_id = get_channel_id(board_html)
        all_feed_entries = get_feed_ids(board_html)

        # If --all or cutoff spans multiple years, also check archives
        if args.all or (cutoff.year < today.year - 1):
            if chan_id:
                archive_url = f"{board_url}/?Archives=1&ChanID={chan_id}"
                archive_html = fetch_html(archive_url)
                if archive_html:
                    for entry in get_feed_ids(archive_html):
                        if entry[1] not in {e[1] for e in all_feed_entries}:
                            all_feed_entries.append(entry)
                time.sleep(PAGE_DELAY)

        # Filter feed entries by date window using entry label
        in_window = []
        for path, fid, label in all_feed_entries:
            entry_date = parse_date_from_text(label)
            entry_year = parse_year_from_text(label) if not entry_date else None

            if entry_date:
                if cutoff <= entry_date <= future_limit:
                    in_window.append((path, fid, label, entry_date, None))
            elif entry_year:
                if cutoff.year <= entry_year <= future_limit.year:
                    in_window.append((path, fid, label, None, entry_year))
            # If no date at all, skip

        print(f"{len(in_window)} feed entries in window", flush=True)
        time.sleep(PAGE_DELAY)

        # --- Step 2: fetch each feed entry and extract links ---
        for path, fid, label, entry_date, entry_year in in_window:
            dropbox_links, s3_links = get_links_from_feed_entry(slug, fid)
            time.sleep(PAGE_DELAY)

            if btype == "packets":
                for link_text, dropbox_url in dropbox_links:
                    link_date = parse_date_from_text(link_text) or entry_date
                    link_year = parse_year_from_text(link_text) if not link_date else None

                    # Date filter on individual link
                    if link_date:
                        if not (cutoff <= link_date <= future_limit):
                            continue
                    elif link_year:
                        if not (cutoff.year <= link_year <= future_limit.year):
                            continue
                    elif entry_date:
                        pass  # use entry_date
                    else:
                        continue

                    all_docs.append({
                        "board": board_name,
                        "type": "packet",
                        "date": link_date or entry_date,
                        "year": link_year or entry_year,
                        "title": link_text or label,
                        "url": dropbox_url,
                        "ext": "zip",
                    })

            elif btype == "audio":
                for link_text, s3_url, ext in s3_links:
                    link_date = parse_date_from_text(link_text) or entry_date
                    link_year = parse_year_from_text(link_text) if not link_date else None

                    if link_date:
                        if not (cutoff <= link_date <= future_limit):
                            continue
                    elif link_year:
                        if not (cutoff.year <= link_year <= future_limit.year):
                            continue
                    elif entry_date:
                        pass
                    else:
                        continue

                    all_docs.append({
                        "board": board_name,
                        "type": "recording",
                        "date": link_date or entry_date,
                        "year": link_year or entry_year,
                        "title": link_text or label,
                        "url": s3_url,
                        "ext": ext,
                    })

    # Sort by date descending
    all_docs.sort(
        key=lambda x: x["date"] or datetime.date(x["year"] or 1900, 1, 1),
        reverse=True,
    )

    print()
    boards_repr = len({d["board"] for d in all_docs})
    print(f"Found {len(all_docs)} document(s) across {boards_repr} board(s).")
    print()

    if not all_docs:
        print("No documents found within the date window.")
        if not args.all:
            print(f"Try a wider window: --days {args.days * 6} or --all")
        sys.exit(0)

    if args.dry_run:
        print(f"{'Board':<42} {'Date':<12} {'Type':<10} Title")
        print("-" * 90)
        for doc in all_docs:
            dt = str(doc["date"]) if doc["date"] else (str(doc["year"]) if doc["year"] else "?")
            title = doc["title"][:35]
            print(f"{doc['board'][:41]:<42} {dt:<12} {doc['type']:<10} {title}")
        print(f"\n{len(all_docs)} document(s). Re-run without --dry-run to download.")
        return

    # --- Step 3: download ---
    os.makedirs(args.output_dir, exist_ok=True)
    log_path = os.path.join(args.output_dir, "download-log.txt")
    log_lines = []
    downloaded = skipped = failed = 0
    seen_dests = {}

    for doc in all_docs:
        doc["counter"] = 0
        dest = make_dest_path(doc, args.output_dir)
        count = seen_dests.get(dest, 0)
        seen_dests[dest] = count + 1
        if count > 0:
            doc["counter"] = count
            dest = make_dest_path(doc, args.output_dir)

        if os.path.exists(dest):
            print(f"  skip (exists)  {os.path.basename(dest)}")
            skipped += 1
            continue

        dt = str(doc["date"] or doc.get("year") or "?")
        print(f"  [{dt}] {doc['board']} — {doc['type']}")
        print(f"  downloading    {os.path.basename(dest)}")
        print(f"  source         {doc['url'][:80]}...")

        download_url_str = doc["url"]
        if doc["type"] == "packet":
            download_url_str = make_dropbox_zip_url(download_url_str)

        ok, _ = download_url(download_url_str, dest)
        if ok:
            downloaded += 1
            log_lines.append(f"{datetime.datetime.now().isoformat()}  OK       {dest}")
        else:
            failed += 1
            log_lines.append(f"{datetime.datetime.now().isoformat()}  FAILED   {doc['url']}")
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
# 1. Preview meeting packets for the past 30 days:
#    python3 scripts/download-canton-agendas.py --dry-run
#
# 2. Preview a longer window (1 year):
#    python3 scripts/download-canton-agendas.py --dry-run --days 365
#
# 3. Download all available packets (2020–present):
#    python3 scripts/download-canton-agendas.py --all
#
# 4. Filter to a specific board:
#    python3 scripts/download-canton-agendas.py --board "planning and zoning" --dry-run
#    python3 scripts/download-canton-agendas.py --board "conservation" --dry-run
#
# 5. Also download audio/video recordings (large files, 50-300 MB each):
#    python3 scripts/download-canton-agendas.py --include-audio --dry-run
#
# 6. Download audio recordings only:
#    python3 scripts/download-canton-agendas.py --include-audio --no-packets --dry-run
#
# 7. Save to a custom directory:
#    python3 scripts/download-canton-agendas.py --output-dir ~/Downloads/canton-meetings
#
# 8. Run on a schedule (cron — 8 AM daily):
#    0 8 * * * cd /path/to/repo && python3 scripts/download-canton-agendas.py
#
# SITE NOTES:
#   - townofcantonct.org runs the QScend CMS (server: QScend).
#   - Meeting packets are stored in Dropbox shared folders; the script downloads
#     the entire folder as a ZIP by appending ?dl=1 to the Dropbox URL.
#   - Audio/video recordings are stored as direct-download MP3/MP4 files on AWS S3.
#   - Most boards (Board of Selectmen, Board of Finance, etc.) post audio
#     recordings instead of written minutes.
#   - Land-use commissions (PZC, CC, ZBA, IWWA, etc.) post meeting packets
#     that include agendas and supporting documents as Dropbox folders.
#   - Feed entries are organized by year on each board page; the Archives
#     link provides access to older entries.
#
# BOARDS WITH MEETING PACKETS (~9 as of 2026):
#   Aquifer Protection Agency, Conservation Commission,
#   Canton Center Historic District, Collinsville Historic District Commission,
#   Economic Development Agency, Inland Wetlands and Watercourses Agency,
#   Planning and Zoning Commission, Zoning Board of Appeals,
#   Temporary POCD Committee
#
# BOARDS WITH AUDIO RECORDINGS (~20+ as of 2026):
#   Board of Finance, Board of Selectmen, Planning and Zoning Commission,
#   Conservation Commission, Inland Wetlands and Watercourses Agency,
#   Zoning Board of Appeals, Water Pollution Control Authority,
#   Board of Assessment Appeals, Commission on Aging, Library Board of Trustees,
#   Economic Development Agency, Pension Committee, and many others
