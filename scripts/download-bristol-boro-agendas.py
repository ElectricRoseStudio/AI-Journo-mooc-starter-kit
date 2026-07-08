#!/usr/bin/env python3
# download-bristol-boro-agendas.py
# Downloads Bristol Borough, PA meeting agendas and video recordings.
#
# Documents: bristolborough.com is a React SPA. Meeting agendas are hardcoded
# in the JavaScript bundle as links to /pdf/agendas/YYYY-MM-DD-BOARDTYPE-AGENDA.pdf.
# These static PDFs ARE served by Apache with Last-Modified headers, so we can
# use exact upload timestamps to filter. The approach:
#   1. Fetch the homepage to get the current bundle URL (hash changes on rebuild).
#   2. Download the bundle and extract all /pdf/agendas/ links.
#   3. HEAD each link for Last-Modified; download if within the cutoff window.
#
# Note: Meeting minutes are not published on the website.
#
# Videos: YouTube channel UCCKBkzpaDEkPXWt7UlyDZXg (Bristol Borough YouTube Channel).
# Channel is roughly reverse-chronological; early-stop after 3 consecutive old videos.

import argparse
import datetime
import email.utils
import os
import re
import subprocess
import urllib.error
import urllib.request

YT_DLP_NODE = "node:/home/richkirby/.local/bin/yt-dlp-node"  # yt-dlp needs Node 22+; symlink kept current by scripts/update-yt-dlp-node.sh

UA = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36"
)
BASE        = "https://www.bristolborough.com"
YT_CHANNEL  = "https://www.youtube.com/channel/UCCKBkzpaDEkPXWt7UlyDZXg"

REPO_DIR   = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUTPUT_DIR = os.path.join(REPO_DIR, "beat-archive", "bristol-boro-agendas")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _req(url, method="GET"):
    return urllib.request.Request(
        url, headers={"User-Agent": UA}, method=method
    )


def subdir_for(dt):
    path = os.path.join(OUTPUT_DIR, dt.strftime("%Y-%m"))
    os.makedirs(path, exist_ok=True)
    return path


# ---------------------------------------------------------------------------
# Bundle scraping
# ---------------------------------------------------------------------------

def get_bundle_url():
    """Fetch the homepage and extract the current JS bundle URL."""
    with urllib.request.urlopen(_req(BASE), timeout=20) as r:
        html = r.read().decode("utf-8", errors="replace")
    m = re.search(r'src="(/assets/index-[A-Za-z0-9_-]+\.js)"', html)
    if not m:
        raise RuntimeError("Could not find bundle URL in homepage HTML")
    return BASE + m.group(1)


def get_agenda_links(bundle_url):
    """Download the JS bundle and return all /pdf/agendas/ links."""
    with urllib.request.urlopen(_req(bundle_url), timeout=60) as r:
        js = r.read().decode("utf-8", errors="replace")
    # Links appear as href:`/pdf/agendas/YYYY-MM-DD-BOARDTYPE-AGENDA.pdf`
    links = re.findall(r'href:`(/pdf/agendas/[^`"\'<>\s]+\.pdf)`', js)
    return sorted(set(links))


def get_last_modified(url):
    """HEAD request; return Last-Modified as datetime or None."""
    try:
        with urllib.request.urlopen(_req(url, "HEAD"), timeout=15) as r:
            lm = r.headers.get("Last-Modified")
            if lm:
                return datetime.datetime(*email.utils.parsedate(lm)[:6])
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return None
        raise
    return None


def download_agenda(path, cutoff, dry_run):
    """
    Download /pdf/agendas/ file if Last-Modified >= cutoff and not already saved.
    Returns True if a file was downloaded (or would be in dry-run).
    """
    url   = BASE + path
    fname = os.path.basename(path)

    # Parse date from filename for subdirectory placement (YYYY-MM-DD prefix)
    date_m = re.match(r"(\d{4})-(\d{2})-(\d{2})", fname)
    if date_m:
        fdate = datetime.datetime(int(date_m.group(1)), int(date_m.group(2)),
                                  int(date_m.group(3)))
    else:
        fdate = datetime.datetime.now()

    out_dir  = subdir_for(fdate)
    out_path = os.path.join(out_dir, fname)

    if os.path.exists(out_path):
        print(f"  Already have: {fname}")
        return False

    lm = get_last_modified(url)
    if lm is None:
        print(f"  Not found (404): {fname}")
        return False

    if lm < cutoff:
        print(f"  {fname}  last-modified {lm.date()} < cutoff {cutoff.date()}, skip")
        return False

    print(f"  Downloading: {fname}  (uploaded {lm.date()})")
    if dry_run:
        return True

    with urllib.request.urlopen(_req(url), timeout=120) as r:
        data = r.read()
    with open(out_path, "wb") as f:
        f.write(data)

    log_path = os.path.join(out_dir, "download-log.txt")
    with open(log_path, "a") as lf:
        lf.write(
            f"{datetime.datetime.now().isoformat()}  "
            f"[Agenda]  {fname}  uploaded={lm.isoformat()}  {url}\n"
        )
    return True


# ---------------------------------------------------------------------------
# YouTube
# ---------------------------------------------------------------------------

def get_video_ids(max_videos=30):
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
            print(f"  Already have video: {video_id}")
            return False

    print(f"  Downloading video: '{title}'  ({upload_date.date()})")
    print(f"  Source URL:        {yt_url}")
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
        description="Download Bristol Borough, PA meeting agendas and videos."
    )
    parser.add_argument(
        "--lookback", type=int, default=14,
        help="Days back for Last-Modified cutoff on agenda PDFs (default 14)",
    )
    parser.add_argument(
        "--video-lookback", type=int, default=14,
        help="Days back for YouTube upload_date cutoff (default 14)",
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Print what would be downloaded; don't write files",
    )
    args = parser.parse_args()

    now    = datetime.datetime.now()
    today  = now.date()
    cutoff = datetime.datetime.combine(
        today - datetime.timedelta(days=args.lookback),
        datetime.time.min,
    )
    vcutoff = datetime.datetime.combine(
        today - datetime.timedelta(days=args.video_lookback),
        datetime.time.min,
    )

    print(f"Agenda Last-Modified cutoff: {cutoff.date()}")
    print(f"Video upload cutoff:         {vcutoff.date()}")

    # ---- Agendas -----------------------------------------------------------
    print("\nFetching homepage to find bundle URL ...")
    try:
        bundle_url = get_bundle_url()
        print(f"Bundle: {bundle_url}")
    except Exception as e:
        print(f"ERROR: {e}")
        return

    print("Extracting agenda links from bundle ...")
    links = get_agenda_links(bundle_url)
    print(f"Found {len(links)} agenda link(s): {[os.path.basename(l) for l in links]}")

    found_any = False
    for path in links:
        if download_agenda(path, cutoff, args.dry_run):
            found_any = True

    # ---- Videos ------------------------------------------------------------
    print(f"\nChecking YouTube channel for recent meeting videos ...")
    video_ids = get_video_ids(max_videos=30)
    print(f"Found {len(video_ids)} channel videos to check")

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
                print(f"  ({STOP_STREAK} consecutive old videos — stopping)")
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
