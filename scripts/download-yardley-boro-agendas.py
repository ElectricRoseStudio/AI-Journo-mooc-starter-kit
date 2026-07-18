#!/usr/bin/env python3
# download-yardley-boro-agendas.py
# Downloads Yardley Borough, PA council meeting agendas, minutes, and video recordings.
#
# Documents: Squarespace site at yardleyboro.org. PDFs are served at /s/{filename}
# which redirects to a Squarespace CDN URL containing a Unix millisecond timestamp
# in the path: /t/{hash}/{ms_timestamp}/{filename}. That timestamp is the upload date.
# Last-Modified is not available; we extract the CDN timestamp instead.
#
# Videos: YouTube channel @yardleyborough8413 publishes council meetings, work sessions,
# and police planning meetings. Channel order is non-chronological, so all IDs are
# fetched and filtered by upload_date.

import argparse
import datetime
import os
import re
import subprocess
import sys
import urllib.error
import urllib.request

YT_DLP_NODE = "node:/home/richkirby/.local/bin/yt-dlp-node"  # yt-dlp needs Node 22+; symlink kept current by scripts/update-yt-dlp-node.sh

UA = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36"
)
BASE          = "https://www.yardleyboro.org"
MEETINGS_URL  = BASE + "/council-meetings-1"
YT_CHANNEL    = "https://www.youtube.com/@yardleyborough8413"

REPO_DIR   = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUTPUT_DIR = os.path.join(REPO_DIR, "beat-archive", "yardley-boro-agendas")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _req(url, method="GET"):
    return urllib.request.Request(url, headers={"User-Agent": UA}, method=method)


def squarespace_upload_date(path):
    """
    HEAD the /s/ path, follow the Squarespace CDN redirect, extract the Unix ms
    timestamp from the CDN URL pattern /t/{hash}/{ms}/{filename}, and return a
    datetime. Returns None if the timestamp cannot be extracted.
    """
    url = BASE + path
    try:
        with urllib.request.urlopen(_req(url, "HEAD"), timeout=15) as r:
            final_url = r.url
    except Exception:
        return None

    m = re.search(r"/t/[a-f0-9]+/(\d{10,13})/", final_url)
    if not m:
        return None
    raw = int(m.group(1))
    ts_s = raw / 1000 if raw > 1e12 else raw
    return datetime.datetime.fromtimestamp(ts_s, tz=datetime.timezone.utc).replace(tzinfo=None)


def subdir_for(dt):
    path = os.path.join(OUTPUT_DIR, dt.strftime("%Y-%m"))
    os.makedirs(path, exist_ok=True)
    return path


# ---------------------------------------------------------------------------
# PDF fetching
# ---------------------------------------------------------------------------

def fetch_pdf_links():
    """
    Return list of (path, label) in page order (top = most recently added section).
    Only returns unique /s/ PDF paths.
    """
    req = urllib.request.Request(MEETINGS_URL, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=30) as r:
        html = r.read().decode("utf-8", errors="replace")

    matches = re.findall(
        r'<a[^>]+href="(/s/[^"]+\.pdf)"[^>]*>\s*([^<]{1,80})\s*</a>',
        html, re.IGNORECASE
    )
    seen = set()
    unique = []
    for path, label in matches:
        if path not in seen:
            seen.add(path)
            unique.append((path, label.strip()))
    return unique


def download_pdf(path, label, cutoff, dry_run):
    """Return (downloaded, too_old)."""
    upload_dt = squarespace_upload_date(path)
    if upload_dt is None:
        print(f"    No CDN timestamp for {path.split('/')[-1]}, skipping")
        return False, False
    if upload_dt < cutoff:
        return False, True

    fname    = path.split("/")[-1]
    out_dir  = subdir_for(upload_dt)
    out_path = os.path.join(out_dir, fname)

    if os.path.exists(out_path):
        print(f"    Already have: {fname}")
        return True, False

    print(f"    Downloading: {fname}  [{label}]  (uploaded {upload_dt.date()})")
    if dry_run:
        return True, False

    with urllib.request.urlopen(_req(BASE + path), timeout=120) as r:
        data = r.read()
    with open(out_path, "wb") as f:
        f.write(data)

    log_path = os.path.join(out_dir, "download-log.txt")
    with open(log_path, "a") as lf:
        lf.write(
            f"{datetime.datetime.now().isoformat()}  {fname}  "
            f"[{label}]  {BASE + path}\n"
        )
    return True, False


# ---------------------------------------------------------------------------
# YouTube fetching
# ---------------------------------------------------------------------------

def get_channel_video_ids(max_videos=50):
    """Return all video IDs from the channel (channel order, not upload order)."""
    try:
        result = subprocess.run(
            ["yt-dlp", "--js-runtimes", YT_DLP_NODE, "--flat-playlist", "--no-update",
             "--playlist-items", f"1:{max_videos}",
             "--print", "%(id)s",
             YT_CHANNEL],
            capture_output=True, text=True, timeout=60,
        )
        return [l.strip() for l in result.stdout.splitlines() if l.strip()]
    except Exception as e:
        print(f"  yt-dlp channel list failed: {e}")
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
    out_dir  = subdir_for(upload_date)
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
            timeout=3600,
        )
    except subprocess.TimeoutExpired:
        print(
            f"    ERROR: yt-dlp timed out after 3600s downloading "
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
        description="Download Yardley Borough, PA meeting documents and videos."
    )
    parser.add_argument(
        "--lookback", type=int, default=3,
        help="Days back for PDF upload-date cutoff (default 3)",
    )
    parser.add_argument(
        "--video-lookback", type=int, default=14,
        help="Days back for YouTube upload date (default 14)",
    )
    parser.add_argument(
        "--year-window", type=int, default=2,
        help="Only check PDFs whose filename contains one of the last N calendar years (default 2)",
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Print what would be downloaded; don't write files",
    )
    args = parser.parse_args()

    now    = datetime.datetime.now()
    cutoff = datetime.datetime.combine(
        now.date() - datetime.timedelta(days=args.lookback),
        datetime.time.min,
    )
    vcutoff = datetime.datetime.combine(
        now.date() - datetime.timedelta(days=args.video_lookback),
        datetime.time.min,
    )

    print(f"PDF cutoff:   {cutoff.date()}  (CDN upload timestamp >= this date)")
    print(f"Video cutoff: {vcutoff.date()}  (upload_date >= this date)")

    # ---- PDFs ---------------------------------------------------------------
    print(f"\nFetching council meetings page ...")
    pdf_links = fetch_pdf_links()

    # Squarespace page lists all agendas first (2026..2010), then all minutes.
    # Minutes uploaded this week may appear at position 200+. Instead of checking
    # by position, filter by year in the filename so we only HEAD-request recent docs.
    recent_years = {str(now.year - i) for i in range(args.year_window)}
    candidate_links = [
        (path, label) for path, label in pdf_links
        if any(yr in path for yr in recent_years)
    ]
    print(f"Found {len(pdf_links)} unique PDFs; {len(candidate_links)} contain a recent year ({', '.join(sorted(recent_years, reverse=True))})")

    found_any = False

    for path, label in candidate_links:
        print(f"  {path.split('/')[-1]}  [{label}]")
        downloaded, _ = download_pdf(path, label, cutoff, args.dry_run)
        if downloaded:
            found_any = True

    # ---- YouTube videos -----------------------------------------------------
    print(f"\nChecking YouTube channel for recent videos ...")
    video_ids = get_channel_video_ids(max_videos=100)
    print(f"Found {len(video_ids)} channel videos to check")

    for vid_id in video_ids:
        upload_dt, title = get_video_info(vid_id)
        if upload_dt is None:
            print(f"  {vid_id}: could not get info, skipping")
            continue
        if upload_dt < vcutoff:
            print(f"  {vid_id}: uploaded {upload_dt.date()} < cutoff {vcutoff.date()}, skip")
            continue
        print(f"  {vid_id}: uploaded {upload_dt.date()} — '{title}'")
        if download_video(vid_id, title, upload_dt, args.dry_run):
            found_any = True

    if not found_any:
        print("\nNo new files within the cutoff window.")
    print("\nDone.")


if __name__ == "__main__":
    main()
