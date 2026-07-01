#!/usr/bin/env python3
# download-falls-twp-agendas.py
# Downloads Falls Township, PA meeting agendas, minutes, and video recordings.
#
# Documents: fallstwp.com/government/meetings/ lists all boards on one page.
# Files are served at /media/{id}/{filename} with sequential IDs (higher = newer).
# HEAD requests return Last-Modified. Sorted descending with early-stop.
#
# Videos: YouTube channel UCXkR2V1sBOCmVB3S5aHEjTA. Videos are embedded in the
# meetings table HTML, each linked to a specific meeting date. We parse the page
# to collect video IDs from meeting rows whose meeting_date falls within a broad
# window, then use yt-dlp to get the actual upload_date and filter by that.

import argparse
import datetime
import os
import re
import subprocess
import urllib.error
import urllib.request

YT_DLP_NODE = "node:/home/richkirby/.nvm/versions/node/v20.20.2/bin/node"  # yt-dlp needs Node 20+; system node is 18

UA = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36"
)
BASE         = "https://www.fallstwp.com"
MEETINGS_URL = BASE + "/government/meetings/"
YT_CHANNEL   = "https://www.youtube.com/channel/UCXkR2V1sBOCmVB3S5aHEjTA"

REPO_DIR   = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUTPUT_DIR = os.path.join(REPO_DIR, "beat-archive", "falls-twp-agendas")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _req(url, method="GET"):
    return urllib.request.Request(url, headers={"User-Agent": UA}, method=method)


def parse_last_modified(header_val):
    if not header_val:
        return None
    for fmt in ("%a, %d %b %Y %H:%M:%S %Z", "%a, %d %b %Y %H:%M:%S GMT"):
        try:
            return datetime.datetime.strptime(header_val.strip(), fmt)
        except ValueError:
            continue
    return None


def subdir_for(dt):
    path = os.path.join(OUTPUT_DIR, dt.strftime("%Y-%m"))
    os.makedirs(path, exist_ok=True)
    return path


# ---------------------------------------------------------------------------
# PDF fetching
# ---------------------------------------------------------------------------

def fetch_media_links():
    """
    Fetch the meetings page and return a list of (media_id, path, label)
    sorted by media_id descending (highest = most recently uploaded).
    """
    req = urllib.request.Request(MEETINGS_URL, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=60) as r:
        html = r.read().decode("utf-8", errors="replace")

    matches = re.findall(
        r'href="(/media/(\d+)/[^"]+\.[a-zA-Z0-9]+)"[^>]*aria-label="([^"]+)"',
        html,
    )
    seen = {}
    for path, mid, label in matches:
        if mid not in seen:
            seen[mid] = (int(mid), path, label)

    return sorted(seen.values(), key=lambda x: x[0], reverse=True)


def download_file(media_id, path, label, cutoff, dry_run):
    """Return (downloaded: bool, too_old: bool)."""
    url = BASE + path
    try:
        with urllib.request.urlopen(_req(url, "HEAD"), timeout=15) as r:
            lm_raw = r.headers.get("Last-Modified", "")
    except urllib.error.HTTPError:
        return False, False

    lm = parse_last_modified(lm_raw)
    if lm is None:
        print(f"    No Last-Modified for {path}, skipping")
        return False, False
    if lm < cutoff:
        return False, True

    fname    = path.rsplit("/", 1)[-1]
    out_dir  = subdir_for(lm)
    out_path = os.path.join(out_dir, fname)

    if os.path.exists(out_path):
        print(f"    Already have: {fname}")
        return True, False

    print(f"    Downloading [{label}]: {fname}  (uploaded {lm.date()})")
    if dry_run:
        return True, False

    with urllib.request.urlopen(_req(url), timeout=120) as r:
        data = r.read()
    with open(out_path, "wb") as f:
        f.write(data)

    log_path = os.path.join(out_dir, "download-log.txt")
    with open(log_path, "a") as lf:
        lf.write(
            f"{datetime.datetime.now().isoformat()}  "
            f"[{label}]  {fname}  {url}\n"
        )
    return True, False


# ---------------------------------------------------------------------------
# Video fetching
# ---------------------------------------------------------------------------

def get_channel_video_ids(max_videos=30):
    """
    Return video IDs from the channel. Channel order is non-chronological
    (newest upload is first, but older and newer videos are interleaved),
    so all results must be checked by upload_date.
    """
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

    subprocess.run(
        ["yt-dlp", "--js-runtimes", YT_DLP_NODE, "--no-update", "--no-overwrites", "--no-playlist",
         "-o", out_tmpl, yt_url],
        timeout=600,
    )
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
        description="Download Falls Township, PA meeting documents and videos."
    )
    parser.add_argument(
        "--lookback", type=int, default=3,
        help="Days back for PDF Last-Modified cutoff (default 3)",
    )
    parser.add_argument(
        "--video-lookback", type=int, default=14,
        help="Days back for YouTube upload_date cutoff (default 14)",
    )
    parser.add_argument(
        "--max-check", type=int, default=60,
        help="Max media IDs to check descending (default 60)",
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

    print(f"PDF cutoff:   {cutoff.date()}  (Last-Modified >= this date)")
    print(f"Video cutoff: {vcutoff.date()}  (upload_date >= this date)")

    # ---- PDFs ---------------------------------------------------------------
    print(f"\nFetching meetings page ...")
    media_links = fetch_media_links()
    print(f"Found {len(media_links)} unique media files; checking top {min(args.max_check, len(media_links))} by ID")

    STOP_STREAK = 10
    found_any  = False
    old_streak = 0

    for media_id, path, label in media_links[:args.max_check]:
        print(f"  ID {media_id}  {path.rsplit('/', 1)[-1]}")
        downloaded, too_old = download_file(media_id, path, label, cutoff, args.dry_run)
        if downloaded:
            found_any  = True
            old_streak = 0
        elif too_old:
            old_streak += 1
            if old_streak >= STOP_STREAK:
                print(f"  ({STOP_STREAK} consecutive old files — stopping early)")
                break
        else:
            old_streak = 0

    # ---- YouTube videos -----------------------------------------------------
    # Channel order is non-chronological; newest upload tends to be near position
    # 1 but older uploads are interspersed. Check top 30 and filter by upload_date.
    print(f"\nChecking YouTube channel for recent videos ...")
    video_ids = get_channel_video_ids(max_videos=30)
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
