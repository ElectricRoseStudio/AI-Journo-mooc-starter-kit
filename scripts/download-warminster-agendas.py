#!/usr/bin/env python3
# download-warminster-agendas.py
# Downloads Warminster Township, PA meeting agendas, minutes, and video recordings.
#
# Documents: warminstertownship.org WordPress site with the WP Download Manager
# (WPDM) plugin. All documents are exposed via an RSS feed at:
#   /feed/?post_type=wpdmpro
# Each RSS item includes the pubDate (when the file was posted) and a <guid>
# containing the WordPress post ID. The download URL is:
#   https://warminstertownship.org/?wpdmdl=<ID>
# The filename is taken from the Content-Disposition header on the response.
#
# Videos: YouTube channel /user/WarminsterTownship (meetings only). Uses yt-dlp
# to list recent uploads and downloads videos posted within --video-lookback days.

import argparse
import datetime
import email.utils
import os
import re
import subprocess
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET

YT_DLP_NODE = "node:/home/richkirby/.nvm/versions/node/v24.18.0/bin/node"  # yt-dlp needs Node 22+; system node is 18

UA = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36"
)

BASE     = "https://warminstertownship.org"
RSS_BASE = BASE + "/feed/?post_type=wpdmpro"
YT_CHAN  = "https://www.youtube.com/user/WarminsterTownship/videos"

REPO_DIR   = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUTPUT_DIR = os.path.join(REPO_DIR, "beat-archive", "warminster-agendas")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _req(url, method="GET"):
    return urllib.request.Request(url, headers={"User-Agent": UA}, method=method)


def subdir_for(dt):
    path = os.path.join(OUTPUT_DIR, dt.strftime("%Y-%m"))
    os.makedirs(path, exist_ok=True)
    return path


def safe_filename(s):
    return re.sub(r'[\\/:*?"<>|]', "_", s).strip()


# ---------------------------------------------------------------------------
# RSS fetching and parsing
# ---------------------------------------------------------------------------

def fetch_rss_page(page=1):
    url = RSS_BASE if page == 1 else f"{RSS_BASE}&paged={page}"
    try:
        with urllib.request.urlopen(_req(url), timeout=30) as r:
            return r.read().decode("utf-8", errors="replace")
    except Exception as e:
        print(f"  Error fetching RSS page {page}: {e}")
        return ""


def parse_rss_items(xml_text):
    """
    Return list of dicts: {id, title, pub_dt}
    ID is extracted from the <guid> tag: ?post_type=wpdmpro&p=<ID>
    """
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError as e:
        print(f"  RSS parse error: {e}")
        return []

    ns = {"dc": "http://purl.org/dc/elements/1.1/"}
    items = []
    for item in root.iter("item"):
        title_el = item.find("title")
        guid_el  = item.find("guid")
        date_el  = item.find("pubDate")
        if title_el is None or guid_el is None or date_el is None:
            continue

        title    = (title_el.text or "").strip()
        guid     = (guid_el.text  or "").strip()
        date_str = (date_el.text  or "").strip()

        # Extract post ID from guid: "...?post_type=wpdmpro&p=26656"
        m = re.search(r'[?&]p=(\d+)', guid)
        if not m:
            continue
        pkg_id = int(m.group(1))

        # Parse RFC 2822 pubDate
        try:
            pub_dt = datetime.datetime(*email.utils.parsedate(date_str)[:6])
        except Exception:
            continue

        items.append({"id": pkg_id, "title": title, "pub_dt": pub_dt})

    return items


# ---------------------------------------------------------------------------
# Document downloading
# ---------------------------------------------------------------------------

def download_doc(pkg_id, title, pub_dt, dry_run):
    """
    Download a WPDM package via ?wpdmdl=<id>. Filename is taken from the
    Content-Disposition header returned by the server. Returns True on success.
    """
    url = f"{BASE}/?wpdmdl={pkg_id}"

    # Head request to get filename without downloading yet
    try:
        with urllib.request.urlopen(_req(url, "HEAD"), timeout=15) as r:
            cd = r.headers.get("Content-Disposition", "")
            # Content-Disposition: inline;filename="062226-EAC-Agenda.pdf"
            fname_m = re.search(r'filename=["\']?([^"\';\r\n]+)', cd)
            if fname_m:
                fname = safe_filename(fname_m.group(1).strip())
            else:
                # Fallback: derive from title and ID
                slug = re.sub(r'[^a-z0-9]+', '-', title.lower()).strip('-')
                fname = f"{slug}-{pkg_id}.pdf"
    except urllib.error.HTTPError as e:
        print(f"    HEAD {pkg_id} returned HTTP {e.code} — skipping")
        return False
    except Exception as e:
        print(f"    HEAD {pkg_id} error: {e} — skipping")
        return False

    out_dir  = subdir_for(pub_dt)
    out_path = os.path.join(out_dir, fname)

    if os.path.exists(out_path):
        print(f"    Already have: {fname}")
        return True

    print(f"    Downloading [{pub_dt.strftime('%Y-%m-%d')}]: {fname}")
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
            f"{fname}  {url}  (posted {pub_dt.date()})\n"
        )
    return True


# ---------------------------------------------------------------------------
# YouTube fetching
# ---------------------------------------------------------------------------

def get_video_ids(max_videos=20):
    try:
        result = subprocess.run(
            ["yt-dlp", "--js-runtimes", YT_DLP_NODE, "--flat-playlist", "--no-update",
             "--playlist-items", f"1:{max_videos}",
             "--print", "%(id)s",
             YT_CHAN],
            capture_output=True, text=True, timeout=60,
        )
        return [l.strip() for l in result.stdout.splitlines() if l.strip()]
    except Exception as e:
        print(f"  yt-dlp list failed: {e}")
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
            if len(date_str) == 8 and date_str.isdigit():
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
        description="Download Warminster Township, PA meeting documents and videos."
    )
    parser.add_argument(
        "--lookback", type=int, default=3,
        help="Days back to accept documents by RSS pubDate (default 3).",
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

    today   = datetime.date.today()
    cutoff  = datetime.datetime.combine(
        today - datetime.timedelta(days=args.lookback),
        datetime.time.min,
    )
    vcutoff = datetime.datetime.combine(
        today - datetime.timedelta(days=args.video_lookback),
        datetime.time.min,
    )

    print(f"Document pubDate cutoff: {cutoff.date()}")
    print(f"Video upload cutoff:     {vcutoff.date()}")

    # ---- Documents ----------------------------------------------------------
    print("\nFetching RSS feed ...")
    items = []
    for page in range(1, 10):          # safety cap: 10 pages × 10 items = 100
        rss_xml   = fetch_rss_page(page)
        page_items = parse_rss_items(rss_xml)
        if not page_items:
            break
        items.extend(page_items)
        # Stop paging if the oldest item on this page is already past the cutoff
        if page_items[-1]["pub_dt"] < cutoff:
            break
    print(f"Found {len(items)} packages across RSS pages")

    found_any = False
    STOP_STREAK = 3
    old_streak  = 0

    for item in items:
        if item["pub_dt"] < cutoff:
            old_streak += 1
            print(
                f"  {item['id']:>6d}  {item['pub_dt'].date()}  "
                f"(older than cutoff — {old_streak}/{STOP_STREAK})"
            )
            if old_streak >= STOP_STREAK:
                print(f"  ({STOP_STREAK} consecutive old items — stopping)")
                break
            continue

        old_streak = 0
        print(f"  {item['id']:>6d}  {item['pub_dt'].date()}  {item['title'][:60]}")
        if download_doc(item["id"], item["title"], item["pub_dt"], args.dry_run):
            found_any = True

    # ---- YouTube videos -----------------------------------------------------
    print("\nChecking YouTube channel for recent meeting videos ...")
    video_ids = get_video_ids(max_videos=20)
    print(f"Found {len(video_ids)} videos to check")

    old_streak = 0
    for vid_id in video_ids:
        upload_dt, title = get_video_info(vid_id)
        if upload_dt is None:
            print(f"  {vid_id}: could not get info, skipping")
            continue
        if upload_dt < vcutoff:
            old_streak += 1
            print(f"  {vid_id}: uploaded {upload_dt.date()} < cutoff, skip")
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
