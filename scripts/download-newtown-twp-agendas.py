#!/usr/bin/env python3
# download-newtown-twp-agendas.py
# Downloads Newtown Township, PA meeting agendas, minutes, and video recordings
# from the CivicWeb portal at newtowntownship.civicweb.net.
#
# Uses the CivicWeb REST services:
#   /Services/MeetingsService.svc/meetings?month=M&year=Y  — meeting list
#   /Services/MeetingsService.svc/meetings/{id}/meetingDocuments — doc IDs/types
#   /document/{docId}                                       — PDF download (no token)
#   /api/videolink/{meetingId}                              — YouTube video ID

import argparse
import datetime
import json
import os
import re
import subprocess
import sys
import urllib.error
import urllib.request
from email.utils import parsedate_to_datetime

YT_DLP_NODE = "node:/home/richkirby/.local/bin/yt-dlp-node"  # yt-dlp needs Node 22+; symlink kept current by scripts/update-yt-dlp-node.sh

UA = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36"
)
BASE = "https://newtowntownship.civicweb.net"

# DocumentType values for publicly released PDFs
PDF_DOC_TYPES = {4, 10, 53}  # agenda PDF, minutes PDF, adopted minutes PDF

REPO_DIR   = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUTPUT_DIR = os.path.join(REPO_DIR, "beat-archive", "newtown-twp-agendas")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _req(url, method="GET", accept="application/json"):
    return urllib.request.Request(
        url,
        headers={"User-Agent": UA, "Accept": accept},
        method=method,
    )


def fetch_json(url):
    with urllib.request.urlopen(_req(url), timeout=20) as r:
        return json.loads(r.read().decode("utf-8", errors="replace"))


def head_last_modified(url):
    try:
        with urllib.request.urlopen(_req(url, method="HEAD", accept="*/*"), timeout=15) as r:
            lm = r.headers.get("Last-Modified", "")
            if lm:
                return parsedate_to_datetime(lm).replace(tzinfo=None)
    except Exception:
        pass
    return None


def sanitize(s, maxlen=60):
    return re.sub(r"[^\w\s\-]", "", s).strip()[:maxlen]


def subdir_for(date_str):
    dt = datetime.datetime.strptime(date_str, "%Y-%m-%d")
    path = os.path.join(OUTPUT_DIR, dt.strftime("%Y-%m"))
    os.makedirs(path, exist_ok=True)
    return path


# ---------------------------------------------------------------------------
# Data fetching
# ---------------------------------------------------------------------------

def get_meetings(start_date, end_date):
    """Return all meeting records from MeetingsService for months in [start, end]."""
    meetings = []
    seen = set()
    d = start_date
    while d <= end_date:
        key = (d.year, d.month)
        if key not in seen:
            seen.add(key)
            url = (
                f"{BASE}/Services/MeetingsService.svc/meetings"
                f"?month={d.month}&year={d.year}&surroundingmonths=0"
            )
            try:
                meetings.extend(fetch_json(url))
            except Exception as e:
                print(f"  Warning: could not fetch meetings for {d.year}-{d.month:02d}: {e}")
        # advance to 1st of next month
        d = (d.replace(day=28) + datetime.timedelta(days=4)).replace(day=1)
    return meetings


def get_meeting_docs(meeting_id):
    url = f"{BASE}/Services/MeetingsService.svc/meetings/{meeting_id}/meetingDocuments"
    try:
        return fetch_json(url)
    except Exception as e:
        print(f"    Error fetching docs for meeting {meeting_id}: {e}")
        return []


def get_youtube_url(meeting_id):
    url = f"{BASE}/api/videolink/{meeting_id}"
    try:
        data = fetch_json(url)
        # The endpoint returns double-encoded JSON (a JSON string containing a JSON array)
        if isinstance(data, str):
            data = json.loads(data)
        if isinstance(data, list) and data:
            item = data[0]
            if item.get("YouTube") and item.get("YouTubeEventId"):
                vid_id = item["YouTubeEventId"].strip()
                if vid_id:
                    return f"https://www.youtube.com/watch?v={vid_id}"
    except Exception:
        pass
    return None


# ---------------------------------------------------------------------------
# Downloading
# ---------------------------------------------------------------------------

def download_pdf(doc_id, doc_name, meeting_name, meeting_date, cutoff, dry_run):
    board = meeting_name.split(" - ")[0].strip()
    date_tag = meeting_date.replace("-", "")
    board_slug = sanitize(board)
    filename = f"{date_tag}-{board_slug}.pdf"

    lm = head_last_modified(f"{BASE}/document/{doc_id}")
    if lm is None:
        print(f"    Doc {doc_id}: no Last-Modified, skipping")
        return False
    if lm < cutoff:
        print(f"    Doc {doc_id}: Last-Modified {lm.date()} < cutoff {cutoff.date()}, skip")
        return False

    out_dir  = subdir_for(meeting_date)
    out_path = os.path.join(out_dir, filename)
    if os.path.exists(out_path):
        print(f"    Already have: {filename}")
        return True

    print(f"    Downloading: {filename}  (Last-Modified {lm.date()})")
    if dry_run:
        return True

    url = f"{BASE}/document/{doc_id}"
    req = _req(url, accept="application/pdf,*/*")
    with urllib.request.urlopen(req, timeout=120) as r:
        data = r.read()
    with open(out_path, "wb") as f:
        f.write(data)

    log_path = os.path.join(out_dir, "download-log.txt")
    with open(log_path, "a") as lf:
        lf.write(
            f"{datetime.datetime.now().isoformat()}  "
            f"{filename}  {url}\n"
        )
    return True


def download_video(yt_url, meeting_name, meeting_date, cutoff, dry_run):
    # Verify upload date via yt-dlp before downloading
    try:
        result = subprocess.run(
            ["yt-dlp", "--js-runtimes", YT_DLP_NODE, "--no-playlist", "--print", "upload_date", yt_url],
            capture_output=True, text=True, timeout=30,
        )
        raw = result.stdout.strip()
        if len(raw) == 8:
            upload_dt = datetime.datetime.strptime(raw, "%Y%m%d")
            if upload_dt < cutoff:
                print(f"    Video upload {upload_dt.date()} < cutoff, skip")
                return
        else:
            print(f"    Could not determine upload date for {yt_url}, skipping")
            return
    except Exception as e:
        print(f"    yt-dlp date check failed: {e}")
        return

    board = meeting_name.split(" - ")[0].strip()
    date_tag = meeting_date.replace("-", "")
    board_slug = sanitize(board)
    out_dir  = subdir_for(meeting_date)
    out_tmpl = os.path.join(out_dir, f"{date_tag}-{board_slug}.%(ext)s")

    print(f"    Downloading video: {date_tag}-{board_slug}  ({yt_url})")
    if dry_run:
        return

    subprocess.run(
        ["yt-dlp", "--js-runtimes", YT_DLP_NODE, "--no-overwrites", "-o", out_tmpl, yt_url],
        timeout=600,
    )

    log_path = os.path.join(out_dir, "download-log.txt")
    with open(log_path, "a") as lf:
        lf.write(
            f"{datetime.datetime.now().isoformat()}  "
            f"{date_tag}-{board_slug}  {yt_url}\n"
        )


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        description="Download Newtown Township, PA meeting agendas/minutes/video."
    )
    parser.add_argument(
        "--lookback", type=int, default=3,
        help="Days back to consider a document newly posted (default 3)",
    )
    parser.add_argument(
        "--video-lookback", type=int, default=14,
        help="Days back to look for new videos (default 14)",
    )
    parser.add_argument("--dry-run", action="store_true", help="Print what would be downloaded; don't write files")
    args = parser.parse_args()

    now     = datetime.datetime.now()
    cutoff  = now - datetime.timedelta(days=args.lookback)
    vcutoff = now - datetime.timedelta(days=args.video_lookback)

    # Look back video_lookback days for meetings with videos; 7 days ahead for upcoming agendas
    start = now.date() - datetime.timedelta(days=args.video_lookback)
    end   = now.date() + datetime.timedelta(days=7)

    print(
        f"Scanning meetings {start} → {end}  "
        f"(doc cutoff {cutoff.date()}, video cutoff {vcutoff.date()})"
    )

    all_meetings = get_meetings(start, end)
    print(f"Found {len(all_meetings)} meetings in calendar window")

    for mtg in all_meetings:
        mtg_date_str = mtg["MeetingDate"]   # "YYYY-MM-DD"
        mtg_date     = datetime.datetime.strptime(mtg_date_str, "%Y-%m-%d").date()

        if not (start <= mtg_date <= end):
            continue

        mtg_id   = mtg["Id"]
        mtg_name = mtg["Name"]
        has_vid  = bool(mtg.get("VideoIcon"))

        print(f"\nMeeting {mtg_id}: {mtg_name}")

        docs = get_meeting_docs(mtg_id)
        for doc in docs:
            dt = doc.get("DocumentType")
            if dt not in PDF_DOC_TYPES:
                continue
            label = {4: "agenda-PDF", 10: "minutes-PDF", 53: "adopted-min-PDF"}.get(dt, f"type{dt}")
            print(f"  Doc {doc['Id']} ({label}): {doc.get('Name', '')}")
            download_pdf(
                doc["Id"], doc.get("Name", ""),
                mtg_name, mtg_date_str,
                cutoff, args.dry_run,
            )

        if has_vid and mtg_date >= vcutoff.date():
            yt_url = get_youtube_url(mtg_id)
            if yt_url:
                print(f"  Video: {yt_url}")
                download_video(yt_url, mtg_name, mtg_date_str, vcutoff, args.dry_run)

    print("\nDone.")


if __name__ == "__main__":
    main()
