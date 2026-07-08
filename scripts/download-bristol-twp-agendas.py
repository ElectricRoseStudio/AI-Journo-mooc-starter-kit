#!/usr/bin/env python3
# download-bristol-twp-agendas.py
# Downloads Bristol Township, PA meeting agendas, minutes, and video recordings.
#
# Documents: bristoltwppa.gov CivicPlus AgendaCenter. The page loads meetings
# via a per-board AJAX API (POST /AgendaCenter/UpdateCategoryList?year=Y&catID=N).
# CivicPlus does not return Last-Modified headers, so we use two proxies:
#   • "Amended" timestamp: shown in the HTML when an agenda is revised — the
#     exact modification datetime is embedded as text, e.g. "Amended Jun 18, 2026 10:34 AM".
#   • Meeting date: used as a proxy for new (un-amended) documents. We check
#     meetings within a broad window (past 30 days + next 14 days for upcoming
#     agendas) and download files not already in the local archive.
#
# Videos: YouTube channel @bristoltownship4817/streams. The streams playlist is
# in strict reverse-chronological order, so we can early-stop once we hit a
# video older than the video_lookback window.

import argparse
import datetime
import os
import re
import subprocess
import urllib.error
import urllib.parse
import urllib.request

YT_DLP_NODE = "node:/home/richkirby/.nvm/versions/node/v24.18.0/bin/node"  # yt-dlp needs Node 22+; system node is 18

UA = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36"
)
BASE            = "https://www.bristoltwppa.gov"
AGENDA_URL      = BASE + "/AgendaCenter"
UPDATE_CAT_URL  = BASE + "/AgendaCenter/UpdateCategoryList"
YT_STREAMS      = "https://www.youtube.com/@bristoltownship4817/streams"

# catID → board name slug
CATEGORIES = {
    2: "township-council",
    3: "planning-commission",
    4: "zoning-hearing-board",
    6: "environmental-advisory",
    7: "civil-service",
}

REPO_DIR   = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUTPUT_DIR = os.path.join(REPO_DIR, "beat-archive", "bristol-twp-agendas")

MONTH_ABBR = {
    "Jan": 1, "Feb": 2, "Mar": 3, "Apr": 4, "May": 5, "Jun": 6,
    "Jul": 7, "Aug": 8, "Sep": 9, "Oct": 10, "Nov": 11, "Dec": 12,
}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _req(url, method="GET", data=None, extra_headers=None):
    headers = {"User-Agent": UA}
    if extra_headers:
        headers.update(extra_headers)
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    return req


def strip_abbr(text):
    """Replace <abbr title="Full">Short</abbr> with the short form, strip all tags."""
    text = re.sub(r"<abbr[^>]*>([^<]+)</abbr>", r"\1", text)
    text = re.sub(r"<[^>]+>", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def parse_amended_date(amended_text):
    """
    Parse 'Amended Jun 18, 2026 10:34 AM' → datetime.
    Returns None if unparseable.
    """
    m = re.search(
        r"Amended\s+([A-Za-z]{3})\s+(\d{1,2}),\s+(\d{4})\s+(\d{1,2}):(\d{2})\s+(AM|PM)",
        amended_text,
    )
    if not m:
        return None
    mon_abbr, day, year, hour, minute, ampm = m.groups()
    month = MONTH_ABBR.get(mon_abbr)
    if not month:
        return None
    hour = int(hour)
    if ampm == "PM" and hour != 12:
        hour += 12
    elif ampm == "AM" and hour == 12:
        hour = 0
    try:
        return datetime.datetime(int(year), month, int(day), hour, int(minute))
    except ValueError:
        return None


def subdir_for(dt):
    path = os.path.join(OUTPUT_DIR, dt.strftime("%Y-%m"))
    os.makedirs(path, exist_ok=True)
    return path


# ---------------------------------------------------------------------------
# AgendaCenter scraping
# ---------------------------------------------------------------------------

def fetch_category_html(cat_id, year):
    """POST to UpdateCategoryList to get meeting rows for a board/year."""
    payload = urllib.parse.urlencode({"year": str(year), "catID": str(cat_id)}).encode()
    req = _req(
        UPDATE_CAT_URL,
        method="POST",
        data=payload,
        extra_headers={
            "Content-Type": "application/x-www-form-urlencoded",
            "X-Requested-With": "XMLHttpRequest",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return r.read().decode("utf-8", errors="replace")
    except Exception as e:
        print(f"  Error fetching cat {cat_id} year {year}: {e}")
        return ""


def parse_meeting_rows(html, cat_id):
    """
    Parse meeting rows from the AgendaCenter HTML.
    Returns list of dicts: {id, date, board, amended_dt, agenda_url, minutes_url}.
    """
    rows = re.findall(
        r'id="_(\d{8})-(\d+)".*?</tr>',
        html, re.DOTALL
    )
    meetings = []
    for date_str, mid in rows:
        try:
            meeting_date = datetime.datetime.strptime(date_str, "%m%d%Y").date()
        except ValueError:
            continue

        # Find the table row for this meeting ID
        idx = html.find(f'id="_{ date_str }-{ mid }"')
        if idx < 0:
            continue
        # Get a generous window of HTML around this row (up to next row anchor)
        next_idx = html.find('<tr id="row', idx + 10)
        row_html = html[idx : next_idx if next_idx > 0 else idx + 3000]

        # Extract amendment timestamp
        amend_raw = re.search(
            r"Amended\s*<abbr[^>]*>[^<]*</abbr>\s*\d{1,2},\s*\d{4}\s*\d{1,2}:\d{2}\s*[AP]M",
            row_html,
        )
        amended_dt = None
        if amend_raw:
            amended_dt = parse_amended_date(strip_abbr(amend_raw.group(0)))

        # Check if agenda and minutes exist
        agenda_url = None
        minutes_url = None
        if re.search(rf'/AgendaCenter/ViewFile/Agenda/_{date_str}-{mid}', row_html):
            agenda_url = f"/AgendaCenter/ViewFile/Agenda/_{date_str}-{mid}"
        if re.search(rf'/AgendaCenter/ViewFile/Minutes/_{date_str}-{mid}', row_html):
            minutes_url = f"/AgendaCenter/ViewFile/Minutes/_{date_str}-{mid}"

        meetings.append({
            "id":          int(mid),
            "date":        meeting_date,
            "date_str":    date_str,
            "cat_id":      cat_id,
            "board":       CATEGORIES.get(cat_id, f"cat{cat_id}"),
            "amended_dt":  amended_dt,
            "agenda_url":  agenda_url,
            "minutes_url": minutes_url,
        })

    return meetings


def download_doc(url_path, meeting, doc_type, dry_run):
    """
    Try to download a document. Return True if downloaded (or already exists).
    Return False if the file doesn't exist on the server (404).
    """
    url  = BASE + url_path
    date = meeting["date"]
    board = meeting["board"]
    mid   = meeting["id"]

    fname    = f"{date.strftime('%Y%m%d')}-{board}-mtg{mid}-{doc_type.lower()}.pdf"
    out_dir  = subdir_for(datetime.datetime(date.year, date.month, date.day))
    out_path = os.path.join(out_dir, fname)

    if os.path.exists(out_path):
        print(f"    Already have: {fname}")
        return True

    # Check if the file actually exists on the server
    try:
        with urllib.request.urlopen(_req(url, "HEAD"), timeout=15) as r:
            if r.status != 200:
                return False
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return False
        raise

    print(f"    Downloading [{doc_type}]: {fname}  (meeting {date})")
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
            f"[{doc_type}]  {fname}  {url}\n"
        )
    return True


# ---------------------------------------------------------------------------
# YouTube fetching
# ---------------------------------------------------------------------------

def get_stream_video_ids(max_videos=20):
    """
    Return video IDs from the streams playlist.
    The streams playlist is strictly reverse-chronological, so position 1 = newest.
    """
    try:
        result = subprocess.run(
            ["yt-dlp", "--js-runtimes", YT_DLP_NODE, "--flat-playlist", "--no-update",
             "--playlist-items", f"1:{max_videos}",
             "--print", "%(id)s",
             YT_STREAMS],
            capture_output=True, text=True, timeout=60,
        )
        return [l.strip() for l in result.stdout.splitlines() if l.strip()]
    except Exception as e:
        print(f"  yt-dlp streams list failed: {e}")
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
        description="Download Bristol Township, PA meeting documents and videos."
    )
    parser.add_argument(
        "--lookback", type=int, default=30,
        help="Days back by meeting date to check for new documents (default 30). "
             "Because CivicPlus provides no upload timestamp, we use a broad window "
             "to catch newly-posted minutes for older meetings.",
    )
    parser.add_argument(
        "--upcoming", type=int, default=21,
        help="Days ahead to check for newly-posted agendas for upcoming meetings (default 21)",
    )
    parser.add_argument(
        "--amended-only-days", type=int, default=3,
        help="If set, also force-download any document with an 'Amended' timestamp "
             "within this many days, regardless of meeting date (default 3)",
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

    now   = datetime.datetime.now()
    today = now.date()

    past_cutoff     = today - datetime.timedelta(days=args.lookback)
    future_cutoff   = today + datetime.timedelta(days=args.upcoming)
    amended_cutoff  = datetime.datetime.combine(
        today - datetime.timedelta(days=args.amended_only_days),
        datetime.time.min,
    )
    vcutoff = datetime.datetime.combine(
        today - datetime.timedelta(days=args.video_lookback),
        datetime.time.min,
    )

    print(f"Past meeting window:   {past_cutoff} to {today}")
    print(f"Upcoming window:       today to {future_cutoff}")
    print(f"Amended-only cutoff:   {amended_cutoff.date()}")
    print(f"Video cutoff:          {vcutoff.date()}")

    # ---- Documents ----------------------------------------------------------
    years = sorted({today.year, today.year - 1}, reverse=True)
    found_any = False

    for cat_id, board_slug in CATEGORIES.items():
        print(f"\n--- {board_slug} (catID={cat_id}) ---")
        all_meetings = []
        for year in years:
            html = fetch_category_html(cat_id, year)
            if html:
                rows = parse_meeting_rows(html, cat_id)
                all_meetings.extend(rows)

        # Deduplicate and sort by meeting ID descending
        seen = {}
        for m in all_meetings:
            if m["id"] not in seen:
                seen[m["id"]] = m
        sorted_meetings = sorted(seen.values(), key=lambda x: x["id"], reverse=True)

        for m in sorted_meetings:
            d         = m["date"]
            in_past   = past_cutoff <= d <= today
            in_future = today < d <= future_cutoff
            has_recent_amended = (
                m["amended_dt"] is not None and m["amended_dt"] >= amended_cutoff
            )

            if not (in_past or in_future or has_recent_amended):
                print(f"  {d}  (outside window, stopping)")
                break

            label = ""
            if has_recent_amended:
                label = f"[amended {m['amended_dt'].strftime('%m/%d %H:%M')}]"
            elif d > today:
                label = "[upcoming]"
            else:
                label = f"[{(today - d).days}d ago]"

            print(f"  {d}  {label}")

            if m["agenda_url"]:
                if download_doc(m["agenda_url"], m, "Agenda", args.dry_run):
                    found_any = True
            if m["minutes_url"]:
                if download_doc(m["minutes_url"], m, "Minutes", args.dry_run):
                    found_any = True

            # Also speculatively try Minutes if the meeting was recent enough
            # that minutes might have been posted without a direct link yet
            if not m["minutes_url"] and in_past and d <= today:
                speculative_url = f"/AgendaCenter/ViewFile/Minutes/_{m['date_str']}-{m['id']}"
                if download_doc(speculative_url, m, "Minutes", args.dry_run):
                    found_any = True

    # ---- YouTube videos -----------------------------------------------------
    print(f"\nChecking YouTube streams for recent meeting videos ...")
    video_ids = get_stream_video_ids(max_videos=20)
    print(f"Found {len(video_ids)} stream videos to check")

    STOP_STREAK = 3  # streams are in order; stop after 3 consecutive old ones
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
                print(f"  ({STOP_STREAK} consecutive old streams — stopping)")
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
