#!/usr/bin/env python3
# download-wrightstown-twp-agendas.py
# Downloads Wrightstown Township, PA meeting agendas, minutes, and public notices
# from wrightstownpa.org.
#
# All board documents (BOS, Planning Commission, EAC, Historical Commission,
# Zoning Hearing Board, Park & Recreation, Joint Zoning Council, etc.) are
# listed on a single /government/meetings/ page.
# PDFs are served at /media/{id}/{filename} with sequential IDs;
# higher IDs = more recently uploaded.
# HEAD returns Last-Modified which is used for freshness filtering.
# Note: meetings are streamed via Facebook Live only — no downloadable recordings.

import argparse
import datetime
import os
import re
import sys
import urllib.error
import urllib.request
from email.utils import parsedate_to_datetime

UA = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36"
)
BASE         = "https://www.wrightstownpa.org"
MEETINGS_URL = BASE + "/government/meetings/"

REPO_DIR   = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUTPUT_DIR = os.path.join(REPO_DIR, "beat-archive", "wrightstown-twp-agendas")


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


def subdir_for(lm_date):
    path = os.path.join(OUTPUT_DIR, lm_date.strftime("%Y-%m"))
    os.makedirs(path, exist_ok=True)
    return path


# ---------------------------------------------------------------------------
# Fetching
# ---------------------------------------------------------------------------

def fetch_media_links():
    """Return list of (media_id, path, filename) sorted by media_id descending."""
    req = urllib.request.Request(MEETINGS_URL, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=20) as r:
        html = r.read().decode("utf-8", errors="replace")

    matches = re.findall(r'href="(/media/(\d+)/([^"]+\.pdf))"', html, re.IGNORECASE)
    # Deduplicate preserving highest-ID-first order
    seen = set()
    unique = []
    for path, mid, fname in matches:
        if path not in seen:
            seen.add(path)
            unique.append((int(mid), path, fname))

    unique.sort(key=lambda x: x[0], reverse=True)
    return unique


# ---------------------------------------------------------------------------
# Downloading
# ---------------------------------------------------------------------------

def download_file(path, fname, cutoff, dry_run):
    url = BASE + path
    lm = head_last_modified(url)
    if lm is None:
        print(f"    No Last-Modified for {fname}, skipping")
        return False, False   # (downloaded, too_old)
    if lm < cutoff:
        return False, True    # too old — signal caller to stop scanning

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
        lf.write(
            f"{datetime.datetime.now().isoformat()}  {fname}  {url}\n"
        )
    return True, False


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        description="Download Wrightstown Township, PA meeting documents."
    )
    parser.add_argument(
        "--lookback", type=int, default=3,
        help="Days back for Last-Modified cutoff (default 3)",
    )
    parser.add_argument(
        "--max-check", type=int, default=60,
        help="Max number of PDFs to HEAD-check (newest first); default 60",
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

    print(f"Cutoff: {cutoff.date()}  (Last-Modified >= this date)")
    print(f"Fetching meetings page ...")

    media_links = fetch_media_links()
    print(f"Found {len(media_links)} unique PDFs; checking newest {min(args.max_check, len(media_links))}")

    found_any     = False
    old_streak    = 0  # consecutive files older than cutoff
    STOP_STREAK   = 10  # stop after this many consecutive old files

    for media_id, path, fname in media_links[:args.max_check]:
        print(f"  ID={media_id}: {fname}")
        downloaded, too_old = download_file(path, fname, cutoff, args.dry_run)
        if downloaded:
            found_any  = True
            old_streak = 0
        elif too_old:
            old_streak += 1
            if old_streak >= STOP_STREAK:
                print(f"  ({STOP_STREAK} consecutive old files — stopping early)")
                break
        else:
            old_streak = 0  # no Last-Modified, keep going

    if not found_any:
        print("\nNo new files within the cutoff window.")
    print("\nDone.")


if __name__ == "__main__":
    main()
