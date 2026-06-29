#!/usr/bin/env python3
# download-newtown-boro-agendas.py
# Downloads Borough of Newtown, PA meeting agendas, minutes, audio, and video
# from the EvoGov CMS at boroughofnewtown.com.
#
# API: GET /meetings/year_events/?calendar_id=363&year=YYYY
#   Returns JSON {"html": "<tr>...</tr>"} with all meetings for that year.
#   Each row contains direct S3 URLs for agendas, minutes, audio, video, etc.
#   S3 files return Last-Modified on HEAD — used for freshness filtering.

import argparse
import datetime
import json
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
BASE       = "https://www.boroughofnewtown.com"
S3_PREFIX  = "https://evogov.s3.us-west-2.amazonaws.com/meetings/71/"
CALENDAR_ID = 363

REPO_DIR   = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUTPUT_DIR = os.path.join(REPO_DIR, "beat-archive", "newtown-boro-agendas")

# Map URL path segment → doc-type label
DOC_TYPE_MAP = {
    "agendas":    "agenda",
    "minutes":    "minutes",
    "bill_list":  "bill-list",
    "ordinances": "ordinance",
    "audio":      "audio",
    "videos":     "video",
}

# File extensions we attach / download
MEDIA_EXTENSIONS = {".pdf", ".m4a", ".mp3", ".mp4", ".mkv", ".mov"}


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


def sanitize(s, maxlen=60):
    s = re.sub(r"\s+-\s+.*", "", s)       # strip " - rescheduled from..." notes
    s = re.sub(r"[^\w\s\-]", "", s)
    s = re.sub(r"\s+", "-", s.strip())
    return s.lower()[:maxlen]


def subdir_for(date_str):
    # date_str: YYYY-MM-DD
    path = os.path.join(OUTPUT_DIR, date_str[:7])
    os.makedirs(path, exist_ok=True)
    return path


def doc_type_from_url(url):
    for segment, label in DOC_TYPE_MAP.items():
        if f"/{segment}/" in url:
            return label
    return "file"


def ext_from_url(url):
    base = url.split("?")[0].split("/")[-1]
    _, ext = os.path.splitext(base)
    return ext.lower() if ext else ".bin"


# ---------------------------------------------------------------------------
# Fetching
# ---------------------------------------------------------------------------

def fetch_year_html(year):
    url = (
        f"{BASE}/meetings/year_events/"
        f"?calendar_id={CALENDAR_ID}&year={year}"
    )
    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": UA,
            "Accept": "application/json",
            "X-Requested-With": "XMLHttpRequest",
        },
    )
    with urllib.request.urlopen(req, timeout=20) as r:
        return json.loads(r.read().decode("utf-8", errors="replace"))["html"]


def parse_meetings(html):
    """Return list of dicts: {date, name, files: [(url, doc_type, ext)]}."""
    meetings = []
    rows = re.findall(r"<tr>(.*?)</tr>", html, re.DOTALL | re.IGNORECASE)
    for row in rows:
        date_m = re.search(r'data-evo_sort_key="(\d{8})', row)
        name_m = re.search(r'class="evo_meeting_name_link"[^>]*>([^<]+)<', row)
        if not date_m or not name_m:
            continue
        date_str = date_m.group(1)                     # YYYYMMDD...
        date_iso  = f"{date_str[:4]}-{date_str[4:6]}-{date_str[6:8]}"
        name = name_m.group(1).strip()

        s3_links = re.findall(
            r'href="(https://evogov\.s3[^"]+)"', row
        )
        files = []
        for url in s3_links:
            ext = ext_from_url(url)
            if ext in MEDIA_EXTENSIONS:
                files.append((url, doc_type_from_url(url), ext))

        meetings.append({"date": date_iso, "name": name, "files": files})
    return meetings


# ---------------------------------------------------------------------------
# Downloading
# ---------------------------------------------------------------------------

def download_file(url, doc_type, ext, meeting_name, meeting_date, cutoff, dry_run):
    lm = head_last_modified(url)
    if lm is None:
        print(f"    No Last-Modified for {url.split('/')[-1]}, skipping")
        return False
    if lm < cutoff:
        print(f"    {url.split('/')[-1]}: Last-Modified {lm.date()} < cutoff {cutoff.date()}, skip")
        return False

    slug     = sanitize(meeting_name)
    date_tag = meeting_date.replace("-", "")
    filename = f"{date_tag}-{slug}-{doc_type}{ext}"
    out_dir  = subdir_for(meeting_date)
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
        description="Download Borough of Newtown, PA meeting documents."
    )
    parser.add_argument(
        "--lookback", type=int, default=3,
        help="Days back for Last-Modified cutoff (default 3)",
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Print what would be downloaded; don't write files",
    )
    args = parser.parse_args()

    now    = datetime.datetime.now()
    cutoff = now - datetime.timedelta(days=args.lookback)

    print(f"Cutoff: {cutoff.date()}  (Last-Modified >= this date)")

    # Fetch current year; also check previous year if we're in January
    years = [now.year]
    if now.month == 1:
        years.append(now.year - 1)

    all_meetings = []
    for yr in years:
        print(f"Fetching {yr} meeting calendar ...")
        html = fetch_year_html(yr)
        all_meetings.extend(parse_meetings(html))

    print(f"Total meetings in calendar: {len(all_meetings)}")

    # Only check meetings whose date is within a reasonable window
    # (meeting date within last 14 days or next 7 days — docs may be
    # posted before or shortly after the meeting)
    window_start = (now - datetime.timedelta(days=14)).date()
    window_end   = (now + datetime.timedelta(days=7)).date()

    found_any = False
    for mtg in all_meetings:
        try:
            mtg_date = datetime.date.fromisoformat(mtg["date"])
        except ValueError:
            continue
        if not (window_start <= mtg_date <= window_end):
            continue
        if not mtg["files"]:
            continue

        print(f"\nMeeting: {mtg['name']} ({mtg['date']})")
        for url, doc_type, ext in mtg["files"]:
            print(f"  [{doc_type}] {url.split('/')[-1]}")
            if download_file(url, doc_type, ext, mtg["name"], mtg["date"], cutoff, args.dry_run):
                found_any = True

    if not found_any:
        print("\nNo new files within the cutoff window.")
    print("\nDone.")


if __name__ == "__main__":
    main()
