#!/usr/bin/env python3
# download-derby-agendas.py
# Download municipal meeting agendas and minutes from Derby CT for meetings
# whose date falls within the past N days (and up to 7 days ahead, to catch
# agendas posted early for upcoming meetings).
#
# USAGE:
#   python3 scripts/download-derby-agendas.py [options]
#
# REQUIREMENTS:
#   - Python 3.6+  (no third-party packages needed)
#   - Internet connection
#
# WHAT IT DOES:
#   1. Queries the Derby CT EvoGov meetings API for events in the date window
#   2. For each event, extracts links from agenda, minutes, attachment, video,
#      and audio fields
#   3. Downloads PDFs, MP4 videos, and M4A/MP3 audio directly from AWS S3
#   4. Saves to beat-archive/derby-agendas/YYYY-MM/
#   5. Appends a download log to beat-archive/derby-agendas/download-log.txt
#
# SITE STRUCTURE (EvoGov Municipal CMS):
#   Site:    https://www.derbyct.gov
#   Dashboard: https://www.derbyct.gov/meetingdashboard
#
#   Meetings API:
#     GET /meetings/get_list
#     Params:
#       selected_calendar_ids = 308   (All Meetings Calendar; covers all boards)
#       start_date             = M/D/YYYY
#       end_date               = M/D/YYYY
#       search                 = ""
#       sort_order             = "date_start"
#       current_webpage        = "meetingdashboard"
#     Returns: JSON array of event objects, each with:
#       title             — board name and meeting type
#       start_date_short  — "YYYY-MM-DD"
#       agenda_links      — list of HTML strings with <a href="S3_URL">filename</a>
#       minute_links      — same format
#       attachment_links  — same format (packets, supplemental materials)
#       ordinance_links, resolution_links, staff_report_links, bill_list_links
#       video_links       — MP4 recording of the meeting
#       audio_links       — M4A or MP3 audio recording
#   Each event also has:
#       video_meeting_link — Zoom room join URL (not a recording); optionally
#                            saved as a .url shortcut with --include-meeting-links
#
#   Documents, videos, and audio are on public AWS S3:
#     https://evogov.s3.us-west-2.amazonaws.com/meetings/{org_id}/{type}/{id}
#   No authentication required.
#
#   Anchor text in each link HTML carries the human-readable filename:
#     "BOA - 2026-04-09 - Agenda.pdf"
#     "BOA - 2026-04-09 - Video.mp4"
#     "BOA - 2026-04-09 - Audio.m4a"
#   Some older audio S3 keys have no file extension; the extension is inferred
#   from the anchor text (.m4a) and appended on save.
#
#   Typical file sizes: PDFs 100-500 KB; MP4 videos 100-350 MB; M4A audio 15-50 MB.
#   Use --no-video / --no-audio to skip large files.

import argparse
import datetime
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

# --- Configuration ---
BASE_URL = "https://www.derbyct.gov"
API_URL = f"{BASE_URL}/meetings/get_list"
CALENDAR_ID = "308"          # "All Meetings Calendar" — aggregates all boards
CURRENT_WEBPAGE = "meetingdashboard"
OUTPUT_DIR = "beat-archive/derby-agendas"
DAYS_BACK = 4
DAYS_AHEAD = 7
DELAY_SECONDS = 0.8

# Document link fields to download (by key in API response).
WANTED_LINK_FIELDS = {
    "agenda_links":       "agenda",
    "minute_links":       "minutes",
    "attachment_links":   "attachment",
    "ordinance_links":    "ordinance",
    "resolution_links":   "resolution",
    "staff_report_links": "staff-report",
    "bill_list_links":    "bill-list",
    "video_links":        "video",
    "audio_links":        "audio",
}

# File extensions handled (lowercase)
MEDIA_EXTS = {".pdf", ".mp4", ".m4a", ".mp3"}

UA = "Mozilla/5.0"

# Regex to pull the S3 href and human-readable anchor text from a link HTML string
_HREF_RE = re.compile(r'href="(https://[^"]+)"')
_TEXT_RE = re.compile(r'>([^<]+\.(pdf|mp4|m4a|mp3))</a>', re.IGNORECASE)


# --- HTTP helpers ---

def api_get(params):
    """GET the meetings API and return parsed JSON, or None on error."""
    url = API_URL + "?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(
        url,
        headers={"User-Agent": UA, "Accept": "application/json, */*"},
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            raw = r.read()
            return json.loads(raw.decode("utf-8", errors="replace"))
    except urllib.error.HTTPError as e:
        print(f"  HTTP {e.code} — {url}", file=sys.stderr)
        return None
    except Exception as e:
        print(f"  ERROR: {e} — {url}", file=sys.stderr)
        return None


def download_file(url, dest_path):
    """Download a URL to dest_path. Returns True on success."""
    req = urllib.request.Request(
        url,
        headers={"User-Agent": UA, "Accept": "*/*"},
    )
    try:
        with urllib.request.urlopen(req, timeout=120) as r:
            with open(dest_path, "wb") as f:
                f.write(r.read())
        return True
    except Exception as e:
        print(f"  WARNING: {e}", file=sys.stderr)
        return False


def save_url_shortcut(url, dest_path):
    """Save a URL as a Windows Internet Shortcut (.url) file. Returns True."""
    with open(dest_path, "w") as f:
        f.write(f"[InternetShortcut]\nURL={url}\n")
    return True


# --- Helpers ---

def sanitize_filename(text, max_len=80):
    """Turn an anchor-text filename into a safe filesystem name."""
    ext_m = re.search(r"\.(pdf|mp4|m4a|mp3)$", text, re.IGNORECASE)
    ext = ext_m.group(0).lower() if ext_m else ".bin"
    base = re.sub(r"\.(pdf|mp4|m4a|mp3)$", "", text, flags=re.IGNORECASE).strip()
    base = re.sub(r"[/\\|:*?\"<>]", "-", base)
    base = re.sub(r"\s+", "-", base)
    base = re.sub(r"-{2,}", "-", base)
    return base.strip("-")[:max_len] + ext


def extract_links(event, include_video=True, include_audio=True):
    """
    Return a list of {url, filename, doc_type} dicts from an event's link fields.
    Accepts PDFs, MP4 videos, and M4A/MP3 audio. Some older audio S3 keys have
    no extension; the extension is inferred from the anchor text.
    """
    links = []
    seen_urls = set()
    for field, doc_type in WANTED_LINK_FIELDS.items():
        if field == "video_links" and not include_video:
            continue
        if field == "audio_links" and not include_audio:
            continue
        for link_html in event.get(field) or []:
            href_m = _HREF_RE.search(link_html)
            text_m = _TEXT_RE.search(link_html)
            if not href_m:
                continue
            url = href_m.group(1)
            if url in seen_urls:
                continue

            url_ext = os.path.splitext(url.lower())[1]
            anchor_text = text_m.group(1) if text_m else ""
            anchor_ext = os.path.splitext(anchor_text.lower())[1] if anchor_text else ""

            # Accept URLs whose extension (or anchor text extension) is a known media type.
            # This also catches extensionless S3 audio keys via their anchor text.
            if url_ext not in MEDIA_EXTS and anchor_ext not in MEDIA_EXTS:
                continue

            seen_urls.add(url)
            raw_name = anchor_text if anchor_text else os.path.basename(url)
            links.append({
                "url": url,
                "filename": sanitize_filename(raw_name),
                "doc_type": doc_type,
            })
    return links


def make_dest_path(filename, meeting_date, output_dir):
    month_dir = os.path.join(output_dir, meeting_date.strftime("%Y-%m"))
    os.makedirs(month_dir, exist_ok=True)
    return os.path.join(month_dir, filename)


# --- Main ---

def main():
    parser = argparse.ArgumentParser(
        description=(
            "Download Derby CT municipal agendas and minutes "
            "for meetings within the past N days."
        )
    )
    parser.add_argument(
        "--days", type=int, default=DAYS_BACK, metavar="N",
        help=f"Look back N days by meeting date (default: {DAYS_BACK})",
    )
    parser.add_argument(
        "--ahead", type=int, default=DAYS_AHEAD, metavar="N",
        help=f"Also include meetings up to N days ahead (default: {DAYS_AHEAD})",
    )
    parser.add_argument(
        "--output-dir", default=OUTPUT_DIR, metavar="DIR",
        help=f"Destination directory (default: {OUTPUT_DIR})",
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="List matching documents without downloading",
    )
    parser.add_argument(
        "--board", metavar="NAME",
        help="Only include events whose title contains NAME (case-insensitive)",
    )
    parser.add_argument(
        "--doc-type", metavar="TYPE",
        help=(
            "Only include documents of this type "
            "(agenda, minutes, attachment, ordinance, resolution, "
            "staff-report, bill-list, video, audio)"
        ),
    )
    parser.add_argument(
        "--no-video", action="store_true",
        help="Skip MP4 meeting recordings (video_links)",
    )
    parser.add_argument(
        "--no-audio", action="store_true",
        help="Skip M4A/MP3 meeting recordings (audio_links)",
    )
    parser.add_argument(
        "--include-meeting-links", action="store_true",
        help="Save each event's Zoom join URL as a .url shortcut file",
    )
    args = parser.parse_args()

    now = datetime.datetime.now()
    if (now.weekday() == 5 and now.hour >= 18) or (now.weekday() == 6 and now.hour < 12):  # Saturday night, Sunday morning
        print("Skipping — no downloads on Saturday nights or Sunday mornings.")
        sys.exit(0)

    today = datetime.date.today()
    cutoff = today - datetime.timedelta(days=args.days)
    future_limit = today + datetime.timedelta(days=args.ahead)

    # API uses M/D/YYYY format
    start_str = f"{cutoff.month}/{cutoff.day}/{cutoff.year}"
    end_str = f"{future_limit.month}/{future_limit.day}/{future_limit.year}"

    print(f"Date window : {cutoff} to {future_limit}")
    print(f"API         : {API_URL}")
    if not args.dry_run:
        print(f"Output dir  : {args.output_dir}")
    print()

    # --- Step 1: fetch events ---
    params = {
        "selected_calendar_ids": CALENDAR_ID,
        "start_date": start_str,
        "end_date": end_str,
        "search": "",
        "sort_order": "date_start",
        "current_webpage": CURRENT_WEBPAGE,
    }

    print("Fetching events from Derby CT meetings API...")
    events = api_get(params)
    if events is None:
        print("ERROR: Could not fetch events from the API.", file=sys.stderr)
        sys.exit(1)

    print(f"  API returned {len(events)} event(s) in date window.")

    if not events:
        print("No events found in the date window.")
        return

    # --- Step 2: extract documents ---
    docs = []
    seen_urls = set()
    seen_zoom_keys = set()

    for event in events:
        title = event.get("title", "Unknown").strip()
        date_str = event.get("start_date_short", "")
        try:
            meeting_date = datetime.datetime.strptime(date_str, "%Y-%m-%d").date()
        except ValueError:
            continue

        # Board filter
        if args.board and args.board.lower() not in title.lower():
            continue

        for link in extract_links(
            event,
            include_video=not args.no_video,
            include_audio=not args.no_audio,
        ):
            if link["url"] in seen_urls:
                continue
            if args.doc_type and args.doc_type.lower() != link["doc_type"].lower():
                continue
            seen_urls.add(link["url"])
            docs.append({
                "title": title,
                "meeting_date": meeting_date,
                "doc_type": link["doc_type"],
                "filename": link["filename"],
                "url": link["url"],
                "is_shortcut": False,
            })

        # Optionally save Zoom join links as .url shortcuts
        if args.include_meeting_links:
            zoom_url = (event.get("video_meeting_link") or "").strip()
            zoom_key = (title, meeting_date)
            if zoom_url and "zoom.us" in zoom_url and zoom_key not in seen_zoom_keys:
                seen_zoom_keys.add(zoom_key)
                date_prefix = meeting_date.strftime("%Y-%m-%d")
                title_slug = re.sub(r"[/\\|:*?\"<>\s]+", "-", title[:55]).strip("-")
                zoom_fname = f"{date_prefix}-{title_slug}-zoom-join.url"
                docs.append({
                    "title": title,
                    "meeting_date": meeting_date,
                    "doc_type": "zoom-link",
                    "filename": zoom_fname,
                    "url": zoom_url,
                    "is_shortcut": True,
                })

    docs.sort(key=lambda x: (x["meeting_date"], x["title"]), reverse=True)

    print(
        f"Found {len(docs)} document(s) across "
        f"{len({d['title'] for d in docs})} unique event(s)."
    )
    print()

    if not docs:
        return

    if args.dry_run:
        print(f"{'Board / Event':<52} {'Date':<12} {'Type':<12} {'Filename'}")
        print("-" * 110)
        for d in docs:
            print(
                f"{d['title'][:51]:<52} "
                f"{d['meeting_date']!s:<12} "
                f"{d['doc_type']:<12} "
                f"{d['filename'][:35]}"
            )
        print(f"\n{len(docs)} document(s). Re-run without --dry-run to download.")
        return

    # --- Step 3: download ---
    os.makedirs(args.output_dir, exist_ok=True)
    log_path = os.path.join(args.output_dir, "download-log.txt")
    log_lines = []
    downloaded = skipped = failed = 0

    for d in docs:
        dest = make_dest_path(d["filename"], d["meeting_date"], args.output_dir)
        label = os.path.basename(dest)

        if os.path.exists(dest):
            print(f"  skip (exists)  {label}")
            skipped += 1
            continue

        print(f"  [{d['meeting_date']}] {d['title'][:55]} — {d['doc_type']}")
        print(f"  saving         {label}")
        if d["doc_type"] in ("video", "audio"):
            print(f"  source URL:    {d['url']}")

        if d["is_shortcut"]:
            ok = save_url_shortcut(d["url"], dest)
        else:
            ok = download_file(d["url"], dest)

        if ok:
            downloaded += 1
            log_lines.append(
                f"{datetime.datetime.now().isoformat()}  OK       {dest}"
            )
        else:
            failed += 1
            log_lines.append(
                f"{datetime.datetime.now().isoformat()}  FAILED   {d['url']}"
            )
            if os.path.exists(dest):
                os.remove(dest)

        if not d["is_shortcut"]:
            time.sleep(DELAY_SECONDS)

    if log_lines:
        with open(log_path, "a") as f:
            f.write("\n".join(log_lines) + "\n")

    print()
    print(f"Done — downloaded: {downloaded}  skipped: {skipped}  failed: {failed}")
    if downloaded + skipped:
        print(f"Files in: {args.output_dir}")
    if log_lines:
        print(f"Log:      {log_path}")


if __name__ == "__main__":
    main()


# --- Tips ---
#
# 1. Preview without downloading:
#    python3 scripts/download-derby-agendas.py --dry-run
#
# 2. Narrow to one board:
#    python3 scripts/download-derby-agendas.py --board "Aldermen"
#
# 3. Agendas only (no minutes, attachments, or recordings):
#    python3 scripts/download-derby-agendas.py --doc-type agenda
#
# 4. Skip large video files (PDFs + audio only):
#    python3 scripts/download-derby-agendas.py --no-video
#
# 5. Documents only (no recordings):
#    python3 scripts/download-derby-agendas.py --no-video --no-audio
#
# 6. Also save Zoom join room links as .url shortcuts:
#    python3 scripts/download-derby-agendas.py --include-meeting-links
#
# 7. Change the lookback window:
#    python3 scripts/download-derby-agendas.py --days 7
#
# 8. Save files somewhere else:
#    python3 scripts/download-derby-agendas.py --output-dir ~/Downloads/derby
#
# 9. Run on a schedule (cron — 7 AM daily):
#    0 7 * * * cd /path/to/repo && python3 scripts/download-derby-agendas.py
#
# 10. Process downloaded files with Claude afterward:
#    python3 scripts/download-derby-agendas.py && bash scripts/batch-process.sh beat-archive/derby-agendas/
#
# NOTES:
#   - Derby uses EvoGov Municipal CMS. The meeting dashboard at
#     /meetingdashboard aggregates all boards into a single JSON API.
#   - Calendar ID 308 is "All Meetings Calendar" — it covers every board.
#     Individual board calendar IDs are also available (311=Board of Aldermen,
#     312=Board of Apportionment & Taxation, 313=Commission for Elderly, etc.)
#     but 308 is sufficient to get everything.
#   - All files (PDFs, MP4s, M4As, MP3s) are served from public AWS S3
#     (evogov.s3.us-west-2.amazonaws.com). No authentication required.
#   - Filenames come from the anchor text in each link (e.g.
#     "BOA - 2026-04-09 - Agenda.pdf"), which is already descriptive.
#   - Some older audio S3 keys have no file extension in the URL. The script
#     infers the extension (.m4a) from the anchor text.
#   - MP4 videos are 100-350 MB each; M4A/MP3 audio is 15-50 MB each.
#     Use --no-video or --no-audio to limit storage use.
#   - The --ahead flag (default: 7 days) captures agendas for upcoming meetings
#     that have already been posted. Run daily to stay current.
#   - Pre-December 2019 agendas and minutes are on individual board pages at
#     https://www.derbyct.gov/meeting-agendas-minutes (static PDFs, not covered
#     by this script).
