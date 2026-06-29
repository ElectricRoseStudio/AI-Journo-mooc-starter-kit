#!/usr/bin/env python3
# download-upper-makefield-twp-agendas.py
# Downloads Upper Makefield Township, PA meeting agendas, minutes, and related
# documents from the WordPress-based site at uppermakefield.org.
#
# Approach: scrape each board/committee page for PDF links (hosted at
# /wp-content/uploads/YYYY/MM/), then HEAD-check Last-Modified on files
# whose URL month is within the recent window before downloading.
# YouTube meeting recordings are linked from the site but not publicly
# downloadable via yt-dlp (channel videos are unavailable).

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
BASE = "https://uppermakefield.org"

# Board/committee pages to scrape for document links
BOARD_PAGES = [
    ("board-of-supervisors",              "/reports/board-of-supervisors/"),
    ("planning-commission",               "/reports/boards-commissions/planning-commission/"),
    ("zoning-hearing-board",              "/reports/boards-commissions/zoning-hearing-board/"),
    ("parks-recreation-board",            "/reports/boards-commissions/parks-recreation-board/"),
    ("environmental-advisory",            "/reports/boards-commissions/environmental-advisory/"),
    ("historical-advisory-commission",    "/reports/boards-commissions/historical-advisory-commission/"),
    ("harb",                              "/reports/boards-commissions/historical-architectural-review-board/"),
    ("financial-advisory-committee",      "/reports/boards-commissions/financial-advisory-committee/"),
    ("investment-committee",              "/reports/boards-commissions/investment-committee/"),
    ("traffic-advisory-committee",        "/reports/boards-commissions/traffic-advisory-committee/"),
]

REPO_DIR   = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUTPUT_DIR = os.path.join(REPO_DIR, "beat-archive", "upper-makefield-twp-agendas")


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


def url_upload_month(url):
    """Extract (year, month) from /wp-content/uploads/YYYY/MM/ URL."""
    m = re.search(r"/wp-content/uploads/(\d{4})/(\d{2})/", url)
    if m:
        return int(m.group(1)), int(m.group(2))
    return None, None


def subdir_for(year, month):
    path = os.path.join(OUTPUT_DIR, f"{year:04d}-{month:02d}")
    os.makedirs(path, exist_ok=True)
    return path


# ---------------------------------------------------------------------------
# Fetching
# ---------------------------------------------------------------------------

def fetch_page_pdfs(slug, path):
    """Return list of unique PDF URLs found on the given page path."""
    url = BASE + path
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            html = r.read().decode("utf-8", errors="replace")
    except Exception as e:
        print(f"  WARNING: could not fetch {url}: {e}")
        return []
    pdfs = re.findall(r'href="(https?://[^"]+\.pdf)"', html, re.IGNORECASE)
    # Deduplicate while preserving order
    seen = set()
    unique = []
    for p in pdfs:
        if p not in seen:
            seen.add(p)
            unique.append(p)
    return unique


# ---------------------------------------------------------------------------
# Downloading
# ---------------------------------------------------------------------------

def download_file(url, board_slug, cutoff, dry_run):
    lm = head_last_modified(url)
    if lm is None:
        print(f"    No Last-Modified for {url.split('/')[-1]}, skipping")
        return False
    if lm < cutoff:
        return False

    yr, mo = url_upload_month(url)
    if yr is None:
        yr  = lm.year
        mo  = lm.month

    original_name = url.split("/")[-1].split("?")[0]
    # Prefix with board slug to disambiguate across boards
    filename = f"{board_slug}_{original_name}"
    out_dir  = subdir_for(yr, mo)
    out_path = os.path.join(out_dir, filename)

    if os.path.exists(out_path):
        print(f"    Already have: {filename}")
        return True

    print(f"    Downloading: {filename}  (Last-Modified {lm.date()})")
    if dry_run:
        return True

    with urllib.request.urlopen(_req(url), timeout=120) as r:
        data = r.read()
    with open(out_path, "wb") as f:
        f.write(data)

    log_path = os.path.join(out_dir, "download-log.txt")
    with open(log_path, "a") as lf:
        lf.write(
            f"{datetime.datetime.now().isoformat()}  {filename}  {url}\n"
        )
    return True


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        description="Download Upper Makefield Township, PA meeting documents."
    )
    parser.add_argument(
        "--lookback", type=int, default=3,
        help="Days back for Last-Modified cutoff (default 3)",
    )
    parser.add_argument(
        "--url-months", type=int, default=2,
        help="How many months back in upload URL to consider (default 2)",
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Print what would be downloaded; don't write files",
    )
    args = parser.parse_args()

    now    = datetime.datetime.now()
    # Use midnight of the cutoff date so files uploaded any time on that day are included
    cutoff = datetime.datetime.combine(
        now.date() - datetime.timedelta(days=args.lookback),
        datetime.time.min,
    )

    # Only HEAD-check files whose /YYYY/MM/ upload path is within url_months ago
    month_cutoff = now - datetime.timedelta(days=30 * args.url_months)

    print(f"Cutoff: {cutoff.date()}  (Last-Modified >= this date)")
    print(f"Checking upload months >= {month_cutoff.strftime('%Y-%m')}")

    found_any = False
    for slug, path in BOARD_PAGES:
        print(f"\nScraping {slug} ...")
        pdfs = fetch_page_pdfs(slug, path)
        print(f"  Found {len(pdfs)} PDFs on page")

        for url in pdfs:
            yr, mo = url_upload_month(url)
            if yr is None:
                continue
            # Skip if upload month is older than url_months ago
            upload_month = datetime.datetime(yr, mo, 1)
            if upload_month < month_cutoff.replace(day=1, hour=0, minute=0, second=0):
                continue

            if download_file(url, slug, cutoff, args.dry_run):
                found_any = True

    if not found_any:
        print("\nNo new files within the cutoff window.")
    print("\nDone.")


if __name__ == "__main__":
    main()
