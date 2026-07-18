#!/usr/bin/env python3
# download-bensalem-agendas.py
# Downloads Bensalem Township, PA meeting agendas, minutes, and video recordings.
#
# Documents: bensalempa.gov WordPress site. PDFs are hosted directly at
#   /wp-content/uploads/YEAR/MONTH/filename.pdf
# We scrape three board pages, collect all PDF links, and download any file
# not already in the local archive whose HTTP Last-Modified header falls within
# the --lookback window. Files with no Last-Modified header that aren't already
# in the archive are also downloaded.
#
# Videos: YouTube channel @BensalemTownship/streams. We use yt-dlp to list the
# streams playlist (reverse-chronological) and download any uploaded within
# --video-lookback days.

import argparse
import datetime
import email.utils
import os
import re
import subprocess
import urllib.error
import urllib.request

YT_DLP_NODE = "node:/home/richkirby/.local/bin/yt-dlp-node"  # yt-dlp needs Node 22+; symlink kept current by scripts/update-yt-dlp-node.sh
VIDEO_DOWNLOAD_TIMEOUT = 3600  # seconds; observed line speed for these streams can be well under 1MB/s

UA = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36"
)

BASE = "https://www.bensalempa.gov"
YT_STREAMS = "https://www.youtube.com/@BensalemTownship/streams"

# Board slug → page URL
BOARDS = {
    "council":            BASE + "/meeting-agendas-and-minutes/council/",
    "zoning-hearing-board": BASE + "/meeting-agendas-and-minutes/zoning-hearing-board/",
    "planning-commission":  BASE + "/meeting-agendas-and-minutes/planning-commission/",
}

REPO_DIR   = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUTPUT_DIR = os.path.join(REPO_DIR, "beat-archive", "bensalem-agendas")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _req(url, method="GET"):
    return urllib.request.Request(url, headers={"User-Agent": UA}, method=method)


def fetch_page(url):
    try:
        with urllib.request.urlopen(_req(url), timeout=30) as r:
            return r.read().decode("utf-8", errors="replace")
    except Exception as e:
        print(f"  Error fetching {url}: {e}")
        return ""


def extract_pdf_links(html):
    """Return all unique wp-content/uploads PDF URLs found in the page HTML."""
    found = re.findall(
        r'href=["\']([^"\']*wp-content/uploads/[^"\']+\.pdf)["\']',
        html, re.IGNORECASE
    )
    urls = []
    for u in found:
        if u.startswith("http"):
            urls.append(u)
        elif u.startswith("/"):
            urls.append(BASE + u)
        else:
            urls.append(BASE + "/" + u)
    # deduplicate preserving order
    seen = set()
    result = []
    for u in urls:
        if u not in seen:
            seen.add(u)
            result.append(u)
    return result


def get_last_modified(url):
    """Return datetime from Last-Modified header, or None."""
    try:
        with urllib.request.urlopen(_req(url, "HEAD"), timeout=15) as r:
            lm = r.headers.get("Last-Modified")
            if lm:
                return datetime.datetime(*email.utils.parsedate(lm)[:6])
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return None
        raise
    except Exception:
        pass
    return None


def subdir_for(year, month):
    path = os.path.join(OUTPUT_DIR, f"{year}-{month:02d}")
    os.makedirs(path, exist_ok=True)
    return path


def archive_filename(pdf_url):
    """Return (subdir, filename) for the given PDF URL."""
    # Extract YEAR/MONTH from the URL path
    m = re.search(r"/wp-content/uploads/(\d{4})/(\d{2})/([^/?#]+\.pdf)", pdf_url, re.IGNORECASE)
    if m:
        year, month, fname = int(m.group(1)), int(m.group(2)), m.group(3)
    else:
        fname  = os.path.basename(pdf_url.split("?")[0])
        today  = datetime.date.today()
        year, month = today.year, today.month
    return subdir_for(year, month), fname


def download_pdf(url, lookback_cutoff, dry_run):
    """
    Download the PDF if:
      - It is not already in the archive, AND
      - Its Last-Modified is within the lookback window (or unknown).
    Returns True if downloaded (or already present).
    """
    out_dir, fname = archive_filename(url)
    out_path = os.path.join(out_dir, fname)

    if os.path.exists(out_path):
        print(f"    Already have: {fname}")
        return True

    lm = get_last_modified(url)
    if lm is None:
        # File may have returned 404 on HEAD, or no Last-Modified; skip 404s
        try:
            with urllib.request.urlopen(_req(url, "HEAD"), timeout=15) as r:
                if r.status != 200:
                    return False
        except urllib.error.HTTPError as e:
            if e.code == 404:
                print(f"    404 — skipping: {fname}")
                return False
            raise
        except Exception:
            pass
        # No Last-Modified header; download anyway (might be new)
        print(f"    Downloading [no Last-Modified]: {fname}")
    elif lm < lookback_cutoff:
        print(f"    Skipping (Last-Modified {lm.date()} < cutoff): {fname}")
        return False
    else:
        print(f"    Downloading [Last-Modified {lm.date()}]: {fname}")

    if dry_run:
        return True

    try:
        with urllib.request.urlopen(_req(url), timeout=120) as r:
            data = r.read()
    except Exception as e:
        print(f"    ERROR downloading {fname}: {e}")
        return False

    with open(out_path, "wb") as f:
        f.write(data)

    log_path = os.path.join(out_dir, "download-log.txt")
    with open(log_path, "a") as lf:
        lf.write(
            f"{datetime.datetime.now().isoformat()}  "
            f"{fname}  {url}\n"
        )
    return True


# ---------------------------------------------------------------------------
# YouTube fetching
# ---------------------------------------------------------------------------

def get_stream_video_ids(max_videos=20):
    try:
        result = subprocess.run(
            ["yt-dlp", "--js-runtimes", YT_DLP_NODE, "--flat-playlist", "--no-update",
             "--playlist-items", f"1:{max_videos}",
             "--print", "%(id)s",
             YT_STREAMS],
            capture_output=True, text=True, timeout=60,
        )
        return [l.strip() for l in result.stdout.splitlines() if l.strip()]
    except Exception as e:
        print(f"  yt-dlp streams list failed: {e}")
        return []


def get_video_info(video_id):
    """Return (upload_date datetime, title str) or (None, None)."""
    try:
        result = subprocess.run(
            ["yt-dlp", "--js-runtimes", YT_DLP_NODE, "--no-playlist", "--no-update",
             "--print", "%(upload_date)s\t%(title)s",
             f"https://www.youtube.com/watch?v={video_id}"],
            capture_output=True, text=True, timeout=30,
        )
        line = result.stdout.strip()
        if "\t" in line:
            date_str, title = line.split("\t", 1)
            if len(date_str) == 8:
                return datetime.datetime.strptime(date_str, "%Y%m%d"), title
    except Exception:
        pass
    return None, None


def download_video(video_id, title, upload_date, dry_run):
    out_dir  = subdir_for(upload_date.year, upload_date.month)
    date_tag = upload_date.strftime("%Y%m%d")
    out_tmpl = os.path.join(out_dir, f"{date_tag}-%(title)s.%(ext)s")
    yt_url   = f"https://www.youtube.com/watch?v={video_id}"

    for fname in (os.listdir(out_dir) if os.path.isdir(out_dir) else []):
        if date_tag in fname and video_id in fname:
            print(f"    Already have: {video_id}")
            return True

    print(f"    Downloading video: '{title}'  ({upload_date.date()})")
    print(f"    Source URL:        {yt_url}")
    if dry_run:
        return True

    try:
        result = subprocess.run(
            ["yt-dlp", "--js-runtimes", YT_DLP_NODE, "--no-update", "--no-overwrites", "--no-playlist",
             "-o", out_tmpl, yt_url],
            timeout=VIDEO_DOWNLOAD_TIMEOUT,
        )
    except subprocess.TimeoutExpired:
        print(
            f"    ERROR: yt-dlp timed out after {VIDEO_DOWNLOAD_TIMEOUT}s downloading "
            f"'{title}' — partial file kept, will resume next run"
        )
        return False
    except Exception as e:
        print(f"    ERROR downloading video '{title}': {e}")
        return False

    if result.returncode != 0:
        print(f"    ERROR: yt-dlp exited with code {result.returncode} for '{title}'")
        return False

    log_path = os.path.join(out_dir, "download-log.txt")
    with open(log_path, "a") as lf:
        lf.write(
            f"{datetime.datetime.now().isoformat()}  "
            f"{date_tag}-{video_id}  {yt_url}  '{title}'\n"
        )
    return True


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        description="Download Bensalem Township, PA meeting documents and videos."
    )
    parser.add_argument(
        "--lookback", type=int, default=3,
        help="Days back to accept documents by Last-Modified date (default 3). "
             "Documents with no Last-Modified header that aren't in the archive "
             "are always downloaded.",
    )
    parser.add_argument(
        "--video-lookback", type=int, default=7,
        help="Days back for YouTube upload_date cutoff (default 7)",
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Print what would be downloaded; don't write files",
    )
    args = parser.parse_args()

    os.makedirs(OUTPUT_DIR, exist_ok=True)

    today           = datetime.date.today()
    lookback_cutoff = datetime.datetime.combine(
        today - datetime.timedelta(days=args.lookback),
        datetime.time.min,
    )
    vcutoff = datetime.datetime.combine(
        today - datetime.timedelta(days=args.video_lookback),
        datetime.time.min,
    )

    print(f"Document Last-Modified cutoff: {lookback_cutoff.date()}")
    print(f"Video upload cutoff:           {vcutoff.date()}")

    found_any = False

    # ---- Documents ----------------------------------------------------------
    for board_slug, page_url in BOARDS.items():
        print(f"\n--- {board_slug} ---")
        html = fetch_page(page_url)
        if not html:
            continue
        pdf_links = extract_pdf_links(html)
        print(f"  Found {len(pdf_links)} PDF links")
        for url in pdf_links:
            if download_pdf(url, lookback_cutoff, args.dry_run):
                found_any = True

    # ---- YouTube videos -----------------------------------------------------
    print(f"\nChecking YouTube streams for recent meeting videos ...")
    video_ids = get_stream_video_ids(max_videos=20)
    print(f"Found {len(video_ids)} stream videos to check")

    STOP_STREAK = 3
    old_streak  = 0

    for vid_id in video_ids:
        upload_dt, title = get_video_info(vid_id)
        if upload_dt is None:
            print(f"  {vid_id}: could not get info, skipping")
            continue
        if upload_dt < vcutoff:
            old_streak += 1
            print(f"  {vid_id}: uploaded {upload_dt.date()} < cutoff {vcutoff.date()}, skip")
            if old_streak >= STOP_STREAK:
                print(f"  ({STOP_STREAK} consecutive old streams — stopping)")
                break
            continue
        old_streak = 0
        print(f"  {vid_id}: uploaded {upload_dt.date()} — '{title}'")
        if download_video(vid_id, title, upload_dt, args.dry_run):
            found_any = True

    if not found_any:
        print("\nNo new files within the cutoff window.")
    print("\nDone.")


if __name__ == "__main__":
    main()
