#!/usr/bin/env python3
# download-bridgeport-meetings.py
# Download all Bridgeport CT municipal meeting agendas, minutes, and video
# recordings from all four sources the city uses.
#
# USAGE:
#   python3 scripts/download-bridgeport-meetings.py [options]
#
# REQUIREMENTS:
#   Python 3.6+  (no third-party packages needed for PDF/document downloads)
#   yt-dlp optional, for YouTube video downloads:
#     pip install yt-dlp   OR   sudo apt install yt-dlp
#
# SOURCES:
#   1. Legistar REST API  — City Council agendas/minutes PDF
#                           (bridgeportct.legistar.com)
#   2. City website       — All boards/commissions PDFs
#                           (bridgeportct.gov/sites/default/files/YYYY-MM/)
#   3. Granicus           — Meeting video recordings when available
#                           (bridgeportct.granicus.com)
#   4. YouTube            — Livestreamed meetings via yt-dlp
#                           (requires --video flag)
#
# NOTE: As of mid-2026 Bridgeport is mid-migration to Legistar/Granicus.
# Most content still lives on the city website. The Legistar and Granicus
# sources will grow as the transition continues.
#
# OUTPUT STRUCTURE:
#   beat-archive/bridgeport-agendas/
#     YYYY-MM/
#       <legistar>  YYYY-MM-DD-{board-slug}-{agenda|minutes}.pdf
#       <website>   {board-slug}-{original-filename}.pdf
#       videos/     YYYY-MM-DD-{title-slug}-granicus.{ext}
#     videos/
#       {channel-slug}/   (YouTube downloads, via yt-dlp)
#     download-log.txt

import argparse
import calendar
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
import xml.etree.ElementTree as ET

# ── Configuration ──────────────────────────────────────────────────────────────

SITE        = "https://www.bridgeportct.gov"
LEGISTAR    = "https://webapi.legistar.com/v1/bridgeportct"
GRANICUS    = "https://bridgeportct.granicus.com"
OUTPUT_DIR  = "beat-archive/bridgeport-agendas"
DAYS_BACK   = 4
DAYS_AHEAD  = 7
DELAY       = 1.0

UA = "Bridgeport-Meetings-Downloader/2.0 (journalism research)"

# Board/commission main pages on bridgeportct.gov.
# The scraper finds document subpages automatically from each main page.
BOARD_PAGES = [
    ("Board of Assessment Appeals",       "/government/boards-and-commissions/board-assessment-appeals"),
    ("Board of Public Purchases",         "/government/boards-and-commissions/board-public-purchases-bpp"),
    ("Civil Service Commission",          "/government/boards-and-commissions/civil-service-commission"),
    ("City Council",                      "/government/boards-and-commissions/city-council"),
    ("Ethics Commission",                 "/government/boards-and-commissions/ethics-commission"),
    ("Fire Commission",                   "/government/boards-and-commissions/fire-commission"),
    ("Harbor Commission",                 "/government/boards-and-commissions/harbor-commission"),
    ("Historic District Commission",      "/government/boards-and-commissions/historic-district-commission"),
    ("Inland Wetlands Agency",            "/government/boards-and-commissions/inland-wetlands-watercourses-agency"),
    ("Parks Commission",                  "/government/boards-and-commissions/parks-commission"),
    ("Planning and Zoning Commission",    "/government/boards-and-commissions/planning-and-zoning-commission"),
    ("Police Commission",                 "/government/boards-and-commissions/police-commission"),
    ("Port Authority Commission",         "/government/boards-and-commissions/port-authority-commision"),
    ("School Building Committee",         "/government/boards-and-commissions/school-building-committee"),
    ("Water Pollution Control Authority", "/government/boards-and-commissions/water-pollution-control-authority-commission"),
    ("Zoning Board of Appeals",           "/government/boards-and-commissions/zoning-board-appeals"),
]

# YouTube channels that broadcast or archive Bridgeport public meetings.
YOUTUBE_CHANNELS = [
    ("City of Bridgeport", "https://www.youtube.com/@CityofBridgeport1901"),
    ("Bridgeport Police",  "https://www.youtube.com/@bridgeportpolice9071"),
]

# Granicus view IDs; view 1 is designated for public meetings.
GRANICUS_VIEWS = [1, 2]

# MIME type → file extension for Granicus enclosures.
MIME_EXT = {
    "video/x-ms-wmv": "wmv",
    "video/mp4":      "mp4",
    "video/quicktime": "mov",
    "audio/mpeg":     "mp3",
    "audio/x-ms-wma": "wma",
}

# ── HTTP helpers ───────────────────────────────────────────────────────────────

def _req(url):
    return urllib.request.Request(url, headers={"User-Agent": UA})

def fetch_json(url):
    try:
        with urllib.request.urlopen(_req(url), timeout=30) as r:
            return json.loads(r.read())
    except Exception as e:
        print(f"  WARN fetch_json: {e}", file=sys.stderr)
        return None

def fetch_text(url):
    try:
        with urllib.request.urlopen(_req(url), timeout=30) as r:
            return r.read().decode("utf-8", errors="replace")
    except Exception as e:
        print(f"  WARN fetch_text {url}: {e}", file=sys.stderr)
        return ""

def download_file(url, dest_path):
    try:
        with urllib.request.urlopen(_req(url), timeout=120) as r:
            with open(dest_path, "wb") as f:
                f.write(r.read())
        return True
    except Exception as e:
        print(f"  WARN download: {e}", file=sys.stderr)
        if os.path.exists(dest_path):
            os.remove(dest_path)
        return False

# ── Utilities ──────────────────────────────────────────────────────────────────

def slugify(text, max_len=50):
    text = re.sub(r"[/\\&]", "-", text.lower().strip())
    text = re.sub(r"[^\w\s-]", "", text)
    text = re.sub(r"[\s_]+", "-", text)
    text = re.sub(r"-{2,}", "-", text)
    return text.strip("-")[:max_len]

def month_in_window(ym, cutoff, future_limit):
    """True if the YYYY-MM month has any overlap with [cutoff, future_limit]."""
    try:
        year, month = int(ym[:4]), int(ym[5:7])
        last_day = calendar.monthrange(year, month)[1]
        m_start = datetime.date(year, month, 1)
        m_end   = datetime.date(year, month, last_day)
        return m_start <= future_limit and m_end >= cutoff
    except Exception:
        return False

def ensure_dir(path):
    os.makedirs(path, exist_ok=True)
    return path

def log_entry(log_lines, status, detail):
    log_lines.append(f"{datetime.datetime.now().isoformat()}  {status:<8} {detail}")

# ── Source 1: Legistar REST API ────────────────────────────────────────────────

def collect_legistar(cutoff, future_limit, board_filter):
    print("Source 1 · Legistar REST API (City Council) …")
    events = fetch_json(f"{LEGISTAR}/Events?$orderby=EventDate+desc") or []
    print(f"  {len(events)} total event(s) from API")
    items = []
    seen = set()
    for ev in events:
        raw = ev.get("EventDate", "")
        try:
            meeting_date = datetime.datetime.strptime(raw[:10], "%Y-%m-%d").date()
        except ValueError:
            continue
        if meeting_date < cutoff or meeting_date > future_limit:
            continue
        body = ev.get("EventBodyName", "Unknown")
        if board_filter and board_filter.lower() not in body.lower():
            continue
        month_dir = meeting_date.strftime("%Y-%m")
        date_pfx  = meeting_date.strftime("%Y-%m-%d")
        body_slug = slugify(body)
        for doc_type, field in (("agenda", "EventAgendaFile"), ("minutes", "EventMinutesFile")):
            url = (ev.get(field) or "").strip()
            if not url or url in seen:
                continue
            seen.add(url)
            items.append({
                "url":      url,
                "subdir":   month_dir,
                "filename": f"{date_pfx}-{body_slug}-{doc_type}.pdf",
                "label":    f"[Legistar] {body} {date_pfx} {doc_type}",
            })
    print(f"  {len(items)} document(s) in date window")
    return items

# ── Source 2: City website boards/commissions ──────────────────────────────────

def find_doc_subpages(html, board_path):
    """Return paths of 'meeting-minutes/agendas' subpages linked from board_path."""
    subpages = set()
    for href in re.findall(r'href=["\']([^"\'#?]+)["\']', html):
        path = href if href.startswith("/") else ("/" + href.lstrip("/"))
        if (path.startswith(board_path + "/") and
                any(kw in path for kw in ("minutes", "agendas", "notices", "archived"))):
            subpages.add(path)
    return subpages

def extract_pdf_links(html):
    """Return list of (ym, full_url, raw_filename) for /sites/default/files/ PDFs."""
    results = []
    for href in re.findall(r'href=["\']([^"\']+\.pdf[^"\']*)["\']', html, re.IGNORECASE):
        href_clean = href.split("?")[0]
        href_decoded = urllib.parse.unquote(href_clean)
        m = re.search(r"/sites/default/files/(\d{4}-\d{2})/", href_decoded)
        if not m:
            continue
        ym = m.group(1)
        filename = os.path.basename(href_decoded)
        if href_clean.startswith("http"):
            url = href_clean
        elif href_clean.startswith("/"):
            url = SITE + href_clean
        else:
            url = SITE + "/" + href_clean.lstrip("/")
        results.append((ym, url, filename))
    return results

def collect_website(cutoff, future_limit, board_filter):
    print("Source 2 · City website (boards & commissions) …")
    items = []
    seen_urls = set()
    for board_name, board_path in BOARD_PAGES:
        if board_filter and board_filter.lower() not in board_name.lower():
            continue
        print(f"  Checking {board_name} …", end=" ", flush=True)
        main_html = fetch_text(SITE + board_path)
        if not main_html:
            print("no response")
            continue
        # Gather subpages plus the main page itself
        subpages   = find_doc_subpages(main_html, board_path)
        pages      = {board_path: main_html}
        for sp in sorted(subpages):
            sp_html = fetch_text(SITE + sp)
            if sp_html:
                pages[sp] = sp_html
            time.sleep(DELAY)
        board_slug = slugify(board_name)
        count = 0
        for _, html in pages.items():
            for ym, url, filename in extract_pdf_links(html):
                if url in seen_urls:
                    continue
                if not month_in_window(ym, cutoff, future_limit):
                    continue
                seen_urls.add(url)
                safe_name = re.sub(r"[^\w.\-]", "_", filename)
                items.append({
                    "url":      url,
                    "subdir":   ym,
                    "filename": f"{board_slug}-{safe_name}",
                    "label":    f"[Website] {board_name} {ym} · {filename[:50]}",
                })
                count += 1
        print(f"{count} doc(s)")
        time.sleep(DELAY)
    print(f"  {len(items)} document(s) total from city website")
    return items

# ── Source 3: Granicus video recordings ────────────────────────────────────────

def collect_granicus(cutoff, future_limit, board_filter):
    print("Source 3 · Granicus video recordings …")
    items = []
    for view_id in GRANICUS_VIEWS:
        rss_url  = f"{GRANICUS}/ViewPublisherRSS.php?view_id={view_id}&mode=vpodcast"
        xml_text = fetch_text(rss_url)
        if not xml_text:
            continue
        try:
            root = ET.fromstring(xml_text)
        except ET.ParseError as e:
            print(f"  WARN Granicus RSS parse error (view {view_id}): {e}", file=sys.stderr)
            continue
        for item in root.iter("item"):
            title_el = item.find("title")
            title    = (title_el.text or "").strip() if title_el is not None else "unknown"
            if board_filter and board_filter.lower() not in title.lower():
                continue
            pub_el = item.find("pubDate")
            meeting_date = None
            if pub_el is not None and pub_el.text:
                for fmt in ("%a, %d %b %Y %H:%M:%S %z", "%a, %d %b %Y %H:%M:%S"):
                    try:
                        meeting_date = datetime.datetime.strptime(
                            pub_el.text.strip(), fmt
                        ).date()
                        break
                    except ValueError:
                        pass
            if meeting_date and (meeting_date < cutoff or meeting_date > future_limit):
                continue
            enclosure = item.find("enclosure")
            if enclosure is None:
                continue
            url  = enclosure.get("url", "").strip()
            mime = enclosure.get("type", "")
            if not url:
                continue
            ext = MIME_EXT.get(mime, url.rsplit(".", 1)[-1] if "." in url else "wmv")
            m = re.search(r"clip_id=(\d+)", url)
            clip_id   = m.group(1) if m else "0"
            date_pfx  = meeting_date.strftime("%Y-%m-%d") if meeting_date else "unknown"
            month_dir = meeting_date.strftime("%Y-%m")    if meeting_date else "unknown"
            fname     = f"{date_pfx}-{slugify(title)}-v{view_id}c{clip_id}.{ext}"
            items.append({
                "url":      url,
                "subdir":   os.path.join(month_dir, "videos"),
                "filename": fname,
                "label":    f"[Granicus] view={view_id} clip={clip_id} · {title[:60]}",
            })
    print(f"  {len(items)} recording(s) in date window")
    return items

# ── Source 4: YouTube via yt-dlp ──────────────────────────────────────────────

def download_youtube(cutoff, output_dir, board_filter, dry_run):
    print("Source 4 · YouTube recordings (yt-dlp) …")
    ytdlp = shutil.which("yt-dlp")
    if not ytdlp:
        print("  yt-dlp not found — install with: pip install yt-dlp")
        print("  Skipping YouTube downloads.")
        return
    date_str = cutoff.strftime("%Y%m%d")
    for channel_name, channel_url in YOUTUBE_CHANNELS:
        if board_filter and board_filter.lower() not in channel_name.lower():
            continue
        channel_slug = slugify(channel_name)
        video_dir    = ensure_dir(os.path.join(output_dir, "videos", channel_slug))
        out_tmpl     = os.path.join(video_dir, "%(upload_date)s-%(title)s.%(ext)s")
        cmd = [
            ytdlp,
            "--dateafter", date_str,
            "--format", "bestvideo[ext=mp4]+bestaudio[ext=m4a]/best[ext=mp4]/best",
            "--output", out_tmpl,
            "--restrict-filenames",
            "--no-playlist",
            "--write-description",
            "--write-info-json",
        ]
        if dry_run:
            cmd += ["--simulate", "--print", "%(upload_date)s %(title)s"]
        cmd.append(channel_url)
        print(f"  {channel_name} (--dateafter {date_str})")
        subprocess.run(cmd)

# ── Download queue ─────────────────────────────────────────────────────────────

def run_downloads(items, output_dir, dry_run, log_lines):
    downloaded = skipped = failed = 0
    for item in items:
        dest = os.path.join(ensure_dir(os.path.join(output_dir, item["subdir"])), item["filename"])
        if os.path.exists(dest):
            skipped += 1
            continue
        if dry_run:
            print(f"  would fetch: {item['label']}")
            continue
        print(f"  {item['label']}")
        if download_file(item["url"], dest):
            downloaded += 1
            log_entry(log_lines, "OK", dest)
        else:
            failed += 1
            log_entry(log_lines, "FAILED", item["url"])
        time.sleep(DELAY)
    return downloaded, skipped, failed

# ── Main ───────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description=(
            "Download Bridgeport CT municipal meeting agendas, minutes, and "
            "video recordings from all available sources."
        )
    )
    parser.add_argument("--days", type=int, default=DAYS_BACK, metavar="N",
                        help=f"Look back N days by date (default: {DAYS_BACK})")
    parser.add_argument("--ahead", type=int, default=DAYS_AHEAD, metavar="N",
                        help=f"Include meetings up to N days ahead (default: {DAYS_AHEAD})")
    parser.add_argument("--output-dir", default=OUTPUT_DIR, metavar="DIR",
                        help=f"Root output directory (default: {OUTPUT_DIR})")
    parser.add_argument("--dry-run", action="store_true",
                        help="List matching documents without downloading")
    parser.add_argument("--board", metavar="NAME",
                        help="Only download from bodies whose name contains NAME (case-insensitive)")
    parser.add_argument("--video", action="store_true",
                        help="Also download YouTube recordings via yt-dlp")
    parser.add_argument("--no-legistar",  action="store_true", help="Skip Legistar source")
    parser.add_argument("--no-website",   action="store_true", help="Skip city website source")
    parser.add_argument("--no-granicus",  action="store_true", help="Skip Granicus source")
    args = parser.parse_args()

    if datetime.date.today().weekday() in (6, 0):  # Sunday, Monday
        print("Skipping — no downloads on Sunday or Monday.")
        sys.exit(0)

    today        = datetime.date.today()
    cutoff       = today - datetime.timedelta(days=args.days)
    future_limit = today + datetime.timedelta(days=args.ahead)

    print(f"Date window : {cutoff} → {future_limit}")
    print(f"Output dir  : {args.output_dir}")
    if args.dry_run:
        print("Mode        : DRY RUN (no files written)")
    print()

    ensure_dir(args.output_dir)
    all_items = []

    if not args.no_legistar:
        all_items += collect_legistar(cutoff, future_limit, args.board)
        print()

    if not args.no_website:
        all_items += collect_website(cutoff, future_limit, args.board)
        print()

    if not args.no_granicus:
        all_items += collect_granicus(cutoff, future_limit, args.board)
        print()

    # Deduplicate by URL across all sources
    seen, deduped = set(), []
    for item in all_items:
        if item["url"] not in seen:
            seen.add(item["url"])
            deduped.append(item)
    all_items = deduped

    log_lines = []
    downloaded, skipped, failed = run_downloads(all_items, args.output_dir, args.dry_run, log_lines)

    if args.video:
        print()
        download_youtube(cutoff, args.output_dir, args.board, args.dry_run)

    if log_lines and not args.dry_run:
        log_path = os.path.join(args.output_dir, "download-log.txt")
        with open(log_path, "a") as f:
            f.write("\n".join(log_lines) + "\n")
        print(f"\nLog: {log_path}")

    print()
    print(
        f"Done — {len(all_items)} document(s) found "
        f"· downloaded: {downloaded} · skipped: {skipped} · failed: {failed}"
    )
    if downloaded or skipped:
        print(f"Files in: {args.output_dir}")


if __name__ == "__main__":
    main()


# ── Tips ───────────────────────────────────────────────────────────────────────
#
# Preview everything available in the past 30 days without downloading:
#   python3 scripts/download-bridgeport-meetings.py --dry-run
#
# Download the past 90 days including YouTube recordings:
#   python3 scripts/download-bridgeport-meetings.py --days 90 --video
#
# Narrow to one body (substring match, case-insensitive):
#   python3 scripts/download-bridgeport-meetings.py --board "Police"
#   python3 scripts/download-bridgeport-meetings.py --board "Planning"
#   python3 scripts/download-bridgeport-meetings.py --board "City Council"
#
# PDFs only (skip Granicus and YouTube):
#   python3 scripts/download-bridgeport-meetings.py --no-granicus
#
# Legistar only (City Council via API):
#   python3 scripts/download-bridgeport-meetings.py --no-website --no-granicus
#
# City website boards only:
#   python3 scripts/download-bridgeport-meetings.py --no-legistar --no-granicus
#
# Save to a custom directory:
#   python3 scripts/download-bridgeport-meetings.py --output-dir ~/Downloads/bridgeport
#
# Run daily via cron (8 AM, past 7 days):
#   0 8 * * * cd /path/to/repo && python3 scripts/download-bridgeport-meetings.py --days 7
#
# Then process with Claude:
#   python3 scripts/download-bridgeport-meetings.py && \
#   bash scripts/batch-process.sh beat-archive/bridgeport-agendas/
#
# SOURCE NOTES:
#
# Legistar (bridgeportct.legistar.com):
#   Currently covers City Council meetings only. Bridgeport launched Legistar
#   in early 2026; the archive grows as past and future meetings are entered.
#   API endpoint: https://webapi.legistar.com/v1/bridgeportct/Events
#
# City website (bridgeportct.gov):
#   Each board/commission hosts PDFs at /sites/default/files/YYYY-MM/.
#   This script crawls the main page + any "meeting-minutes-agendas-and-notices"
#   subpages for the 16 boards listed in BOARD_PAGES. To add a board, append
#   its (name, /path) tuple to BOARD_PAGES.
#
# Granicus (bridgeportct.granicus.com):
#   Video recording infrastructure deployed mid-2026. Public meeting recordings
#   will appear here as the city begins archiving council livestreams.
#   Download URL pattern: /DownloadFile.php?view_id=N&clip_id=N
#
# YouTube:
#   The City of Bridgeport channel (@CityofBridgeport1901) and Bridgeport Police
#   (@bridgeportpolice9071) both broadcast and archive public meetings.
#   yt-dlp is used for downloading; it handles format selection and date filtering.
#   Install: pip install yt-dlp
