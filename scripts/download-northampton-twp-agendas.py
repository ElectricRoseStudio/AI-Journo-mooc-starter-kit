#!/usr/bin/env python3
# download-northampton-twp-agendas.py
# Downloads Northampton Township, PA meeting agendas, minutes, and video recordings.
#
# Documents: All board/committee PDFs are listed on a single /government/meeting-schedules/
# page. Media files use sequential /media/{id}/{filename} paths; higher ID = newer upload.
# HEAD returns Last-Modified for freshness filtering.
#
# Videos: The township YouTube channel (UC3Jc0vqHCiol8UcL-aPj4MQ) publishes BOS,
# Planning Commission, and other meeting recordings. Recent uploads are fetched via
# yt-dlp and downloaded if within the video lookback window.

import argparse
import datetime
import os
import re
import subprocess
import sys
import urllib.error
import urllib.request
from email.utils import parsedate_to_datetime

UA = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36"
)
BASE          = "https://www.northamptontownship.com"
MEETINGS_URL  = BASE + "/government/meeting-schedules/"
YT_CHANNEL    = "https://www.youtube.com/channel/UC3Jc0vqHCiol8UcL-aPj4MQ"

REPO_DIR   = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUTPUT_DIR = os.path.join(REPO_DIR, "beat-archive", "northampton-twp-agendas")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _req(url, method="GET"):
    return urllib.request.Request(url, headers={"User-Agent": UA}, method=method)


def head_last_modified(url):
    try:
        with urllib.request.urlopen(_req(url, "HEAD"), timeout=15) as r:
            lm = r.headers.get("Last-Modified", "")
            if lm:
                return parsedate_to_datetime(lm).replace(tzinfo=None)
    except Exception:
        pass
    return None


def subdir_for(dt):
    path = os.path.join(OUTPUT_DIR, dt.strftime("%Y-%m"))
    os.makedirs(path, exist_ok=True)
    return path


# ---------------------------------------------------------------------------
# PDF fetching
# ---------------------------------------------------------------------------

def fetch_media_links():
    """Return list of (media_id, path, filename) sorted by media_id descending."""
    req = urllib.request.Request(MEETINGS_URL, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=20) as r:
        html = r.read().decode("utf-8", errors="replace")

    matches = re.findall(r'href="(/media/(\d+)/([^"]+\.pdf))"', html, re.IGNORECASE)
    seen = set()
    unique = []
    for path, mid, fname in matches:
        if path not in seen:
            seen.add(path)
            unique.append((int(mid), path, fname))
    unique.sort(key=lambda x: x[0], reverse=True)
    return unique


def download_pdf(path, fname, cutoff, dry_run):
    """Return (downloaded, too_old)."""
    url = BASE + path
    lm = head_last_modified(url)
    if lm is None:
        print(f"    No Last-Modified for {fname}, skipping")
        return False, False
    if lm < cutoff:
        return False, True

    out_dir  = subdir_for(lm)
    out_path = os.path.join(out_dir, fname)
    if os.path.exists(out_path):
        print(f"    Already have: {fname}")
        return True, False

    print(f"    Downloading: {fname}  (Last-Modified {lm.date()})")
    if dry_run:
        return True, False

    with urllib.request.urlopen(_req(url), timeout=120) as r:
        data = r.read()
    with open(out_path, "wb") as f:
        f.write(data)

    log_path = os.path.join(out_dir, "download-log.txt")
    with open(log_path, "a") as lf:
        lf.write(f"{datetime.datetime.now().isoformat()}  {fname}  {url}\n")
    return True, False


# ---------------------------------------------------------------------------
# YouTube video fetching
# ---------------------------------------------------------------------------

def get_channel_video_ids(max_videos=20):
    """Return list of video IDs from the channel (most recent first)."""
    try:
        result = subprocess.run(
            [
                "yt-dlp", "--flat-playlist", "--no-update",
                "--playlist-items", f"1:{max_videos}",
                "--print", "%(id)s",
                YT_CHANNEL,
            ],
            capture_output=True, text=True, timeout=60,
        )
        ids = [line.strip() for line in result.stdout.splitlines() if line.strip()]
        return ids
    except Exception as e:
        print(f"  yt-dlp channel list failed: {e}")
        return []


def get_video_upload_date(video_id):
    """Return upload_date as datetime, or None."""
    try:
        result = subprocess.run(
            ["yt-dlp", "--no-playlist", "--no-update", "--print", "upload_date",
             f"https://www.youtube.com/watch?v={video_id}"],
            capture_output=True, text=True, timeout=30,
        )
        raw = result.stdout.strip()
        if len(raw) == 8:
            return datetime.datetime.strptime(raw, "%Y%m%d")
    except Exception:
        pass
    return None


def download_video(video_id, title, upload_date, dry_run):
    out_dir  = subdir_for(upload_date)
    date_tag = upload_date.strftime("%Y%m%d")
    out_tmpl = os.path.join(out_dir, f"{date_tag}-%(title)s.%(ext)s")
    yt_url   = f"https://www.youtube.com/watch?v={video_id}"

    # Check if already downloaded (any file with this date_tag and video ID)
    for fname in os.listdir(out_dir) if os.path.isdir(out_dir) else []:
        if date_tag in fname and video_id in fname:
            print(f"    Already have video: {video_id}")
            return True

    print(f"    Downloading video: {video_id}  '{title}'  ({upload_date.date()})")
    print(f"    Source URL:        {yt_url}")
    if dry_run:
        return True

    subprocess.run(
        ["yt-dlp", "--no-update", "--no-overwrites", "--no-playlist",
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
        description="Download Northampton Township, PA meeting documents and videos."
    )
    parser.add_argument(
        "--lookback", type=int, default=3,
        help="Days back for PDF Last-Modified cutoff (default 3)",
    )
    parser.add_argument(
        "--video-lookback", type=int, default=14,
        help="Days back for YouTube video upload date (default 14)",
    )
    parser.add_argument(
        "--max-check", type=int, default=60,
        help="Max PDFs to HEAD-check (newest first); default 60",
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
    print(f"Found {len(media_links)} unique PDFs; checking newest {min(args.max_check, len(media_links))}")

    found_any   = False
    old_streak  = 0
    STOP_STREAK = 10

    for media_id, path, fname in media_links[:args.max_check]:
        print(f"  ID={media_id}: {fname}")
        downloaded, too_old = download_pdf(path, fname, cutoff, args.dry_run)
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
    print(f"\nChecking YouTube channel for recent videos ...")
    video_ids = get_channel_video_ids(max_videos=20)
    print(f"Found {len(video_ids)} recent channel videos")

    for vid_id in video_ids:
        upload_dt = get_video_upload_date(vid_id)
        if upload_dt is None:
            print(f"  {vid_id}: could not determine upload date, skipping")
            continue
        if upload_dt < vcutoff:
            print(f"  {vid_id}: uploaded {upload_dt.date()} < video cutoff {vcutoff.date()}, skip")
            continue

        # Get title for logging
        try:
            r = subprocess.run(
                ["yt-dlp", "--no-playlist", "--no-update", "--print", "title",
                 f"https://www.youtube.com/watch?v={vid_id}"],
                capture_output=True, text=True, timeout=20,
            )
            title = r.stdout.strip()
        except Exception:
            title = vid_id

        print(f"  {vid_id}: uploaded {upload_dt.date()} — '{title}'")
        if download_video(vid_id, title, upload_dt, args.dry_run):
            found_any = True

    if not found_any:
        print("\nNo new files within the cutoff window.")
    print("\nDone.")


if __name__ == "__main__":
    main()
