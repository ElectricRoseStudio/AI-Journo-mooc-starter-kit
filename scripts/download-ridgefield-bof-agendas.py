#!/usr/bin/env python3
# download-ridgefield-bof-agendas.py
# Download Ridgefield CT Board of Finance agendas, minutes, packets, and
# BoxCast video recordings posted in the past N days.
#
# USAGE:
#   python3 scripts/download-ridgefield-bof-agendas.py [options]
#
# REQUIREMENTS:
#   - Python 3.8+
#   - pip install beautifulsoup4
#   - yt-dlp  (for video:  pip install yt-dlp  or  sudo apt install yt-dlp)
#
# WHAT IT DOES:
#   1. Fetches the Board of Finance agendas/minutes page and downloads PDFs
#      (agendas, minutes, packets) within the lookback window.
#   2. Queries the BoxCast REST API for BOF meeting recordings broadcast
#      within the same window and downloads them via yt-dlp.
#   Files are saved to beat-archive/ridgefield-bof-agendas/YYYY-MM/.
#
# SOURCES:
#   Agendas/minutes: https://www.ridgefieldct.gov/boards_committees_commissions/board_of_finance/index.php
#   Videos:         BoxCast channel rwapliqzshvkvprbawxy (Town of Ridgefield)

import argparse
import datetime
import glob
import json
import os
import re
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

YT_DLP_NODE = "node:/home/richkirby/.local/bin/yt-dlp-node"  # yt-dlp needs Node 22+; symlink kept current by scripts/update-yt-dlp-node.sh

try:
    from bs4 import BeautifulSoup
except ImportError:
    print("ERROR: beautifulsoup4 is not installed.\n  pip install beautifulsoup4",
          file=sys.stderr)
    sys.exit(1)

# --- Configuration ---
BASE_URL   = "https://www.ridgefieldct.gov"
BOF_URL    = f"{BASE_URL}/boards_committees_commissions/board_of_finance/index.php"
OUTPUT_DIR = "beat-archive/ridgefield-bof-agendas"
DAYS_BACK  = 4
DELAY      = 1.0

# BoxCast — Town of Ridgefield channel
BOXCAST_API     = "https://rest.boxcast.com"
BOXCAST_CHANNEL = "rwapliqzshvkvprbawxy"
BOXCAST_VIEW    = "https://boxcast.tv/view-embed"
# q= search term that matches Board of Finance broadcasts
BOXCAST_QUERY   = "finance"
# l= max results; set high because BoxCast doesn't support offset pagination
BOXCAST_LIMIT   = 200

UA = "Ridgefield-BOF-Downloader/1.0 (journalism research)"

# Mapping from TD class name → document label used in filenames
DOC_COLS = {
    "agenda_doc":        "agenda",
    "packet_doc":        "packet",
    "minutes_doc":       "minutes",
    "video_url":         "video",
    "additionalContent": "attachment",
}


# --- HTTP helpers ---

def fetch_html(url):
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            raw = r.read()
            charset = r.headers.get_content_charset() or "utf-8"
            return raw.decode(charset, errors="replace")
    except urllib.error.HTTPError as e:
        print(f"  HTTP {e.code} — {url}", file=sys.stderr)
        return None
    except urllib.error.URLError as e:
        print(f"  ERROR: {e} — {url}", file=sys.stderr)
        return None


def fetch_json(url):
    req = urllib.request.Request(url, headers={
        "User-Agent": UA,
        "Accept": "application/json",
    })
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return json.loads(r.read().decode("utf-8", errors="replace"))
    except urllib.error.HTTPError as e:
        print(f"  HTTP {e.code} — {url}", file=sys.stderr)
        return None
    except Exception as e:
        print(f"  ERROR: {e} — {url}", file=sys.stderr)
        return None


def download_file(url, dest):
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            with open(dest, "wb") as f:
                f.write(r.read())
        return True
    except Exception as e:
        print(f"  WARNING: {e} — {url}", file=sys.stderr)
        return False


# --- Utilities ---

def parse_date(text):
    """Parse MM/DD/YYYY from text. Returns date or None."""
    m = re.search(r"\b(\d{1,2})/(\d{1,2})/(20\d{2})\b", text)
    if m:
        try:
            return datetime.date(int(m.group(3)), int(m.group(1)), int(m.group(2)))
        except ValueError:
            pass
    return None


def make_abs_url(href):
    """Convert a relative /Documents/... href to a fully-encoded absolute URL."""
    href = href.strip()
    if href.startswith("http"):
        return href
    path_part, _, query = href.lstrip("/").partition("?")
    encoded = "/".join(urllib.parse.quote(seg, safe="") for seg in path_part.split("/"))
    url = f"{BASE_URL}/{encoded}"
    if query:
        url += "?" + query
    return url


def slugify(text, max_len=50):
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


# --- Document (PDF) scraping ---

def scrape_meetings(html, cutoff):
    """
    Parse the BOF agendas table and return a list of dicts:
      {date, meeting_name, label, url, filename}
    Only rows with date >= cutoff are included.
    """
    soup = BeautifulSoup(html, "html.parser")
    results = []

    for row in soup.find_all("tr"):
        date_span = row.find("span", class_="agenda-date")
        if not date_span:
            continue
        date = parse_date(date_span.get_text())
        if date is None or date < cutoff:
            continue

        name_span = row.find("span", class_="agenda-name")
        meeting_name = name_span.get_text(strip=True) if name_span else "Meeting"

        for col_cls, col_label in DOC_COLS.items():
            td = row.find("td", class_=col_cls)
            if not td:
                continue
            for a in td.find_all("a", href=True):
                href = a["href"].strip()
                if not href or href.startswith("#") or href.startswith("mailto:"):
                    continue
                link_text = a.get_text(strip=True) or col_label
                abs_url  = make_abs_url(href)
                filename = href.split("?")[0].split("/")[-1]
                results.append({
                    "date":         date,
                    "meeting_name": meeting_name,
                    "label":        link_text.lower(),
                    "url":          abs_url,
                    "filename":     filename,
                })

    return results


def doc_dest_path(item, output_dir):
    d = month_dir(item["date"], output_dir)
    date_str   = item["date"].strftime("%Y-%m-%d")
    label_slug = slugify(item["label"], max_len=20)
    orig_slug  = slugify(os.path.splitext(item["filename"])[0], max_len=40)
    ext = os.path.splitext(item["filename"])[1] or ".pdf"
    return os.path.join(d, f"{date_str}-bof-{label_slug}-{orig_slug}{ext}")


# --- BoxCast video ---

def fetch_boxcast_broadcasts(cutoff, future_limit):
    """
    Query BoxCast REST API for Board of Finance broadcasts within the date window.
    Returns list of dicts: {id, name, date, boxcast_url}

    BoxCast doesn't support offset pagination, so we fetch up to BOXCAST_LIMIT
    results and filter client-side. All BOF broadcasts to date number ~22.
    """
    url = (f"{BOXCAST_API}/channels/{BOXCAST_CHANNEL}/broadcasts"
           f"?q={BOXCAST_QUERY}&l={BOXCAST_LIMIT}")
    data = fetch_json(url)
    if not data or not isinstance(data, list):
        print("  WARNING: BoxCast API returned no data.", file=sys.stderr)
        return []

    results = []
    for b in data:
        starts_at = b.get("starts_at", "")
        if not starts_at:
            continue
        # starts_at is UTC (e.g. "2026-05-12T23:00:00Z"). BOF meetings run
        # in the evening Eastern time, so the UTC date == the meeting date.
        try:
            bdate = datetime.date.fromisoformat(starts_at[:10])
        except ValueError:
            continue
        if not (cutoff <= bdate <= future_limit):
            continue
        bid  = b.get("id", "")
        name = b.get("name", "Board of Finance Meeting")
        results.append({
            "id":           bid,
            "name":         name,
            "date":         bdate,
            "boxcast_url":  f"{BOXCAST_VIEW}/{bid}",
        })

    return results


def video_dest_template(broadcast, output_dir):
    """Return yt-dlp output template for a broadcast."""
    d        = month_dir(broadcast["date"], output_dir)
    date_str = broadcast["date"].strftime("%Y-%m-%d")
    slug     = slugify(broadcast["name"], max_len=50)
    return os.path.join(d, f"{date_str}-bof-video-{slug}.%(ext)s")


def video_already_exists(dest_template):
    base = dest_template.replace(".%(ext)s", "")
    return bool(glob.glob(base + ".*"))


def download_boxcast_video(broadcast, dest_template, dry_run=False):
    """
    Download a BoxCast broadcast via yt-dlp. Returns True on success.
    yt-dlp's BoxCastVideo extractor handles the HLS stream automatically.
    """
    url = broadcast["boxcast_url"]
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
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=7200)
    except subprocess.TimeoutExpired:
        print(f"  WARNING: yt-dlp timed out downloading {url}", file=sys.stderr)
        return False
    if result.returncode != 0 and result.stderr:
        print(f"  WARNING: yt-dlp: {result.stderr[:300]}", file=sys.stderr)
    return result.returncode == 0


def _ytdlp_available():
    try:
        r = subprocess.run(["yt-dlp", "--js-runtimes", YT_DLP_NODE, "--version"], capture_output=True, timeout=5)
        return r.returncode == 0
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return False


# --- Main ---

def main():
    parser = argparse.ArgumentParser(
        description="Download Ridgefield CT Board of Finance agendas, minutes, "
                    "and BoxCast video recordings posted in the past N days."
    )
    parser.add_argument("--days", type=int, default=DAYS_BACK, metavar="N",
                        help=f"Look back N days (default: {DAYS_BACK})")
    parser.add_argument("--output-dir", default=OUTPUT_DIR, metavar="DIR",
                        help=f"Output directory (default: {OUTPUT_DIR})")
    parser.add_argument("--dry-run", action="store_true",
                        help="List matches without downloading")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--include-video", action="store_true",
                      help="Download both documents and BoxCast video recordings")
    mode.add_argument("--video-only", action="store_true",
                      help="Download BoxCast video recordings only (skip PDFs)")
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
    future_limit = today + datetime.timedelta(days=1)

    has_ytdlp = _ytdlp_available()

    print(f"Date window : {cutoff} to {today}  ({args.days} days)")
    print(f"Output dir  : {args.output_dir}")
    if do_video:
        print(f"Video       : enabled (BoxCast / yt-dlp{'  *** NOT FOUND ***' if not has_ytdlp else ''})")
    print()

    dl_ok = dl_skip = dl_fail = 0
    vd_ok = vd_skip = vd_fail = 0

    # --- Documents (PDFs) ---
    if do_docs:
        print(f"Fetching BOF agendas page...")
        html = fetch_html(BOF_URL)
        if not html:
            print("ERROR: Could not fetch the BOF agendas page.", file=sys.stderr)
            sys.exit(1)

        doc_items = scrape_meetings(html, cutoff)
        doc_items.sort(key=lambda x: x["date"], reverse=True)
        print(f"Found {len(doc_items)} document(s) in window.")
        print()

        for item in doc_items:
            dest   = doc_dest_path(item, args.output_dir)
            header = f"  [{item['date']}] {item['meeting_name']} — {item['label'].title()}"

            if args.dry_run:
                print(header)
                print(f"    {os.path.basename(dest)}")
                continue

            print(header)
            if os.path.exists(dest):
                print(f"    skip (exists)  {os.path.basename(dest)}")
                dl_skip += 1
                continue

            print(f"    downloading    {os.path.basename(dest)}")
            if download_file(item["url"], dest):
                dl_ok += 1
            else:
                dl_fail += 1
                if os.path.exists(dest):
                    os.remove(dest)

            time.sleep(DELAY)

    # --- BoxCast video recordings ---
    if do_video:
        if not has_ytdlp and not args.dry_run:
            print("WARNING: yt-dlp not found — skipping video.", file=sys.stderr)
            print("  Install with:  pip install yt-dlp  or  sudo apt install yt-dlp",
                  file=sys.stderr)
        else:
            print("Fetching BoxCast video recordings...")
            broadcasts = fetch_boxcast_broadcasts(cutoff, future_limit)
            broadcasts.sort(key=lambda x: x["date"], reverse=True)
            print(f"Found {len(broadcasts)} recording(s) in window.")
            print()

            for bc in broadcasts:
                tmpl   = video_dest_template(bc, args.output_dir)
                header = f"  [{bc['date']}] {bc['name']}"

                if args.dry_run:
                    print(header)
                    print(f"    {os.path.basename(tmpl)}")
                    print(f"    {bc['boxcast_url']}")
                    continue

                print(header)
                if video_already_exists(tmpl):
                    existing = glob.glob(tmpl.replace(".%(ext)s", ".*"))[0]
                    print(f"    skip (exists)  {os.path.basename(existing)}")
                    vd_skip += 1
                    continue

                print(f"    downloading    {os.path.basename(tmpl)}")
                if download_boxcast_video(bc, tmpl):
                    vd_ok += 1
                else:
                    vd_fail += 1

    if not args.dry_run:
        print()
        if do_docs:
            print(f"Documents  — downloaded: {dl_ok}  skipped: {dl_skip}  failed: {dl_fail}")
        if do_video:
            print(f"Video      — downloaded: {vd_ok}  skipped: {vd_skip}  failed: {vd_fail}")
        if dl_ok + dl_skip + vd_ok + vd_skip:
            print(f"Files in: {args.output_dir}")


if __name__ == "__main__":
    main()


# --- Tips ---
#
# Documents only (default, 15-day window):
#   python3 scripts/download-ridgefield-bof-agendas.py --dry-run
#   python3 scripts/download-ridgefield-bof-agendas.py
#
# Documents + video recordings:
#   python3 scripts/download-ridgefield-bof-agendas.py --include-video
#
# Video recordings only:
#   python3 scripts/download-ridgefield-bof-agendas.py --video-only
#
# Change the lookback window:
#   python3 scripts/download-ridgefield-bof-agendas.py --days 30 --include-video
#
# Save files to a custom directory:
#   python3 scripts/download-ridgefield-bof-agendas.py --output-dir ~/Downloads/bof
#
# Run daily via cron (7 AM):
#   0 7 * * * cd /path/to/repo && python3 scripts/download-ridgefield-bof-agendas.py --include-video
