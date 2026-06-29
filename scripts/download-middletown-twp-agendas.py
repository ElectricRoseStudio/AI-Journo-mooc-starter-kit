#!/usr/bin/env python3
# download-middletown-twp-agendas.py
# Downloads Middletown Township, Bucks County PA meeting agendas, minutes,
# and Board of Supervisors meeting videos.
#
# PDFs: middletownbucks.org/Government/Board-of-Supervisors/Agendas-Minutes
#   All /getattachment/ PDF links are listed on one page. We HEAD each link for
#   its Last-Modified header (set by CivicEngage on upload) and download any file
#   whose Last-Modified falls within the lookback window.
#
# Videos: Swagit at https://middletowntwppa.new.swagit.com/views/367/
#   The view page lists Board of Supervisors meetings with video IDs and meeting
#   dates. Videos are posted within a day or two of the meeting. We download
#   videos whose meeting date falls within video_lookback days, using the
#   /videos/{id}/download endpoint which redirects to a signed S3 MP4 URL.

import argparse
import datetime
import email.utils
import os
import re
import urllib.error
import urllib.parse
import urllib.request

UA = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36"
)

BASE_PDF     = "https://www.middletownbucks.org"
AGENDAS_URL  = BASE_PDF + "/Government/Board-of-Supervisors/Agendas-Minutes"
SWAGIT_BASE  = "https://middletowntwppa.new.swagit.com"
SWAGIT_VIEW  = SWAGIT_BASE + "/views/367/"

REPO_DIR   = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUTPUT_DIR = os.path.join(REPO_DIR, "beat-archive", "middletown-twp-agendas")

MONTH_ABBR = {
    "Jan": 1, "Feb": 2, "Mar": 3, "Apr": 4, "May": 5, "Jun": 6,
    "Jul": 7, "Aug": 8, "Sep": 9, "Oct": 10, "Nov": 11, "Dec": 12,
}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _req(url, method="GET"):
    return urllib.request.Request(url, headers={"User-Agent": UA}, method=method)


def subdir_for(dt):
    path = os.path.join(OUTPUT_DIR, dt.strftime("%Y-%m"))
    os.makedirs(path, exist_ok=True)
    return path


def fetch_url(url, timeout=30):
    with urllib.request.urlopen(_req(url), timeout=timeout) as r:
        return r.read().decode("utf-8", errors="replace")


def head_last_modified(url):
    """Return the Last-Modified datetime for a URL, or None."""
    try:
        with urllib.request.urlopen(_req(url, method="HEAD"), timeout=15) as r:
            lm = r.headers.get("Last-Modified", "")
            if lm:
                return datetime.datetime(*email.utils.parsedate(lm)[:6])
    except Exception:
        pass
    return None


def safe_filename(url_path):
    """Derive a safe filename from a /getattachment/ URL path."""
    # Strip query string
    path = url_path.split("?")[0]
    # Use the last path component (the PDF filename)
    fname = path.rstrip("/").split("/")[-1]
    # Sanitise
    fname = re.sub(r"[^\w\.\-]", "_", fname)
    return fname or "document.pdf"


def log_download(out_dir, entry):
    with open(os.path.join(out_dir, "download-log.txt"), "a") as lf:
        lf.write(f"{datetime.datetime.now().isoformat()}  {entry}\n")


# ---------------------------------------------------------------------------
# PDF (agendas / minutes)
# ---------------------------------------------------------------------------

def process_pdfs(cutoff, dry_run):
    print(f"\nFetching agendas/minutes page ...")
    try:
        html = fetch_url(AGENDAS_URL)
    except Exception as e:
        print(f"  ERROR fetching page: {e}")
        return 0

    all_urls = re.findall(
        r'href="(https://www\.middletownbucks\.org/getattachment/[^"]+\.pdf[^"]*)"',
        html,
    )

    # Only HEAD-check URLs containing the current or prior month in the path.
    # The Calendar path encodes year/month (e.g. /Calendar/2026/June/), so
    # filtering here cuts requests from 200+ down to the handful that could
    # plausibly have a Last-Modified within the 3-day window.
    today  = datetime.date.today()
    months = set()
    for delta in range(2):
        d = today.replace(day=1) - datetime.timedelta(days=delta * 1)
        # go back month-by-month
        if delta == 0:
            months.add((today.year, today.strftime("%B")))
        else:
            prev = (today.replace(day=1) - datetime.timedelta(days=1))
            months.add((prev.year, prev.strftime("%B")))

    def url_in_window(u):
        for y, m in months:
            if f"/Calendar/{y}/{m}/" in u:
                return True
        return False

    urls = [u for u in all_urls if url_in_window(u)]
    print(f"  Found {len(all_urls)} total PDF links; checking {len(urls)} from recent months")
    downloaded = 0

    for url in urls:
        lm = head_last_modified(url)
        if lm is None:
            continue
        if lm < cutoff:
            continue

        fname    = safe_filename(urllib.parse.urlparse(url).path)
        out_dir  = subdir_for(lm)
        out_path = os.path.join(out_dir, fname)

        if os.path.exists(out_path):
            print(f"  Already have: {fname}")
            continue

        print(f"  Downloading [{lm.strftime('%Y-%m-%d')}]: {fname}")
        if dry_run:
            downloaded += 1
            continue

        try:
            with urllib.request.urlopen(_req(url), timeout=120) as r:
                data = r.read()
            with open(out_path, "wb") as f:
                f.write(data)
            log_download(out_dir, f"PDF  {fname}  {url}")
            downloaded += 1
        except Exception as e:
            print(f"    Download failed: {e}")

    return downloaded


# ---------------------------------------------------------------------------
# Swagit videos
# ---------------------------------------------------------------------------

def parse_swagit_view(html):
    """
    Return list of (video_id, meeting_date) from the Swagit view page.
    The page lists first occurrence of /videos/{id} adjacent to a date cell.
    """
    pattern = re.compile(
        r'href="/videos/(\d+)"[^<]*</a>\s*</td>\s*<td[^>]*nowrap[^>]*>\s*'
        r'([A-Za-z]{3})\s+(\d{1,2}),\s+(\d{4})',
        re.DOTALL,
    )
    results = []
    seen = set()
    for m in pattern.finditer(html):
        vid_id   = int(m.group(1))
        mon_abbr = m.group(2)
        day      = int(m.group(3))
        year     = int(m.group(4))
        if vid_id in seen:
            continue
        seen.add(vid_id)
        month = MONTH_ABBR.get(mon_abbr)
        if not month:
            continue
        try:
            results.append((vid_id, datetime.date(year, month, day)))
        except ValueError:
            continue
    return results


def get_swagit_download_url(video_id):
    """
    Follow the /download redirect to get the signed S3 URL (or direct URL).
    Returns the final URL string, or None on error.
    """
    dl_url = f"{SWAGIT_BASE}/videos/{video_id}/download"
    try:
        req = urllib.request.Request(dl_url, headers={"User-Agent": UA})
        # Don't auto-follow so we can capture the Location header
        opener = urllib.request.build_opener(
            urllib.request.HTTPRedirectHandler()
        )
        # Use a no-redirect opener
        no_redirect = urllib.request.build_opener(
            _NoRedirect()
        )
        with no_redirect.open(req, timeout=20) as r:
            location = r.headers.get("Location", "")
            if location:
                return location
    except urllib.error.HTTPError as e:
        loc = e.headers.get("Location", "")
        if loc:
            return loc
    except Exception as e:
        print(f"    Could not get download URL for video {video_id}: {e}")
    return None


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None

    def http_error_302(self, req, fp, code, msg, headers):
        raise urllib.error.HTTPError(req.full_url, code, msg, headers, fp)

    http_error_301 = http_error_303 = http_error_307 = http_error_302


def download_video(video_id, meeting_date, dry_run):
    s3_url = get_swagit_download_url(video_id)
    if not s3_url:
        print(f"    No download URL for video {video_id}")
        return False

    # Determine extension from S3 URL path
    path_part = urllib.parse.urlparse(s3_url).path
    ext = os.path.splitext(path_part)[-1] or ".mp4"

    date_tag = meeting_date.strftime("%Y%m%d")
    fname    = f"{date_tag}-board-of-supervisors-vid{video_id}{ext}"
    out_dir  = subdir_for(datetime.datetime(meeting_date.year, meeting_date.month, meeting_date.day))
    out_path = os.path.join(out_dir, fname)

    if os.path.exists(out_path):
        print(f"    Already have: {fname}")
        return True

    print(f"    Downloading video [{meeting_date}]: {fname}")
    if dry_run:
        return True

    try:
        with urllib.request.urlopen(urllib.request.Request(s3_url, headers={"User-Agent": UA}), timeout=600) as r:
            with open(out_path, "wb") as f:
                while True:
                    chunk = r.read(1024 * 1024)
                    if not chunk:
                        break
                    f.write(chunk)
        size_mb = os.path.getsize(out_path) / (1024 * 1024)
        print(f"      Saved {size_mb:.1f} MB")
        log_download(out_dir, f"VIDEO  {fname}  {SWAGIT_BASE}/videos/{video_id}")
        return True
    except Exception as e:
        print(f"    Video download failed: {e}")
        if os.path.exists(out_path):
            os.remove(out_path)
        return False


def process_videos(video_cutoff, dry_run):
    print(f"\nFetching Swagit view page ...")
    try:
        html = fetch_url(SWAGIT_VIEW)
    except Exception as e:
        print(f"  ERROR: {e}")
        return 0

    entries = parse_swagit_view(html)
    print(f"  Found {len(entries)} video entries")
    downloaded = 0

    for vid_id, meeting_date in entries:
        if meeting_date < video_cutoff:
            print(f"  vid {vid_id} ({meeting_date}) before cutoff — stopping")
            break
        print(f"  vid {vid_id}  meeting {meeting_date}")
        if download_video(vid_id, meeting_date, dry_run):
            downloaded += 1

    return downloaded


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        description="Download Middletown Township, PA meeting documents and videos."
    )
    parser.add_argument(
        "--lookback", type=int, default=3,
        help="Days back for PDF Last-Modified cutoff (default 3).",
    )
    parser.add_argument(
        "--video-lookback", type=int, default=5,
        help="Days back by meeting date for Swagit videos (default 5). "
             "Includes a buffer for the 1-2 day posting delay after meetings.",
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Print what would be downloaded; don't write files",
    )
    args = parser.parse_args()

    now   = datetime.datetime.now()
    today = now.date()

    pdf_cutoff   = datetime.datetime.combine(
        today - datetime.timedelta(days=args.lookback),
        datetime.time.min,
    )
    video_cutoff = today - datetime.timedelta(days=args.video_lookback)

    print(f"PDF Last-Modified cutoff: {pdf_cutoff.date()}")
    print(f"Video meeting-date cutoff: {video_cutoff}")

    n_pdf   = process_pdfs(pdf_cutoff, args.dry_run)
    n_video = process_videos(video_cutoff, args.dry_run)

    total = n_pdf + n_video
    if total == 0:
        print("\nNo new files within the cutoff window.")
    else:
        print(f"\n{n_pdf} PDF(s) and {n_video} video(s) downloaded.")
    print("Done.")


if __name__ == "__main__":
    main()
