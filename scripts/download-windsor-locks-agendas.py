#!/usr/bin/env python3
# download-windsor-locks-agendas.py
# Download Windsor Locks CT municipal meeting agendas, minutes, and Dropbox
# audio/video recordings for meetings within the past N days (and up to 7 days
# ahead).
#
# USAGE:
#   python3 scripts/download-windsor-locks-agendas.py [options]
#
# REQUIREMENTS:
#   - Python 3.6+
#   - cloudscraper (pip3 install cloudscraper) — bypasses Cloudflare protection
#   - yt-dlp (pip3 install yt-dlp OR apt install yt-dlp) — for Dropbox downloads
#   - Internet connection
#
# WHAT IT DOES:
#   1. Fetches all board/committee categories from the TownWeb REST API
#   2. Fetches meeting repository posts modified within the lookback window
#      (using the WordPress REST API ?after= parameter as a proxy for meeting date,
#      with a 60-day buffer to catch documents uploaded before or after meetings)
#   3. Filters posts by meeting_date to the configured date window
#   4. Downloads PDFs (agenda, minutes, agenda packet, additional documents)
#      from windsorlocksct.org wp-content/uploads via cloudscraper
#   5. Downloads audio recordings (M4A, MP3, WAV) and video recordings (MP4)
#      from Dropbox shared links via yt-dlp
#   6. Appends a download log to beat-archive/windsor-locks-agendas/download-log.txt
#
# SITE STRUCTURE:
#   CMS: WordPress with TownWeb (townweb.com) theme and tw-meeting-repository plugin
#   Site: https://windsorlocksct.org (Cloudflare-protected)
#
#   REST API base:
#     https://windsorlocksct.org/wp-json/wp/v2/twd_repository
#     Supports: ?_embed=1 (resolves media IDs to objects)
#               ?after=YYYY-MM-DDTHH:MM:SS (post_modified filter)
#               ?per_page=N&page=N (pagination)
#               ?orderby=modified&order=desc
#
#   Category endpoint:
#     https://windsorlocksct.org/wp-json/wp/v2/twd_repository_cat?per_page=100
#
#   Per-post fields:
#     meeting_date    — YYYY-MM-DD string
#     agenda          — WP attachment object {guid: URL} or ""
#     meeting_minutes — WP attachment object {guid: URL} or ""
#     agenda_pack     — WP attachment object {guid: URL} or ""
#     additional_file — WP attachment object {guid: URL} or ""
#     additional_url  — external URL string or ""
#     sound           — Dropbox shared link (M4A/MP3/WAV) or ""
#     video           — Dropbox shared link (MP4) or ""
#
#   Dropbox links use ?dl=0 (preview). yt-dlp handles these natively.
#   PDF files are served from wp-content/uploads; Cloudflare bypass required.
#
# BOARDS (23, as of 2026-05):
#   ARPA, Board of Assessment Appeals, Board of Finance,
#   Board of Selectmen, Capital Improvements Advisory Committee,
#   Charter Revision Commission, Commission On the Needs of the Aging,
#   Conservation Commission, Economic & Industrial Development,
#   Fire Commission, Historical Commission, Housing Authority,
#   Inland Wetlands & Watercourses Commission, OPEB Board of Trustees,
#   Park Commission, Planning & Zoning Commission, Police Commission,
#   Public Safety Building Committee, Senior Center Study Committee,
#   Town Meetings, Water Pollution Control Authority,
#   Windsor Locks Arts, Zoning Board of Appeals

import argparse
import datetime
import os
import re
import subprocess
import sys
import time

YT_DLP_NODE = "node:/home/richkirby/.nvm/versions/node/v20.20.2/bin/node"  # yt-dlp needs Node 20+; system node is 18

try:
    import cloudscraper
except ImportError:
    sys.exit(
        "ERROR: cloudscraper is required.\n"
        "Install it with: pip3 install cloudscraper"
    )

# --- Configuration ---
BASE_URL = "https://windsorlocksct.org"
API_URL = f"{BASE_URL}/wp-json/wp/v2/twd_repository"
CATS_URL = f"{BASE_URL}/wp-json/wp/v2/twd_repository_cat"
OUTPUT_DIR = "beat-archive/windsor-locks-agendas"
DAYS_BACK = 4
DAYS_AHEAD = 7
DELAY_SECONDS = 0.5

# Extra days before the lookback cutoff to use as the API ?after= filter.
# Documents (especially agendas) are often uploaded a week or more before the
# meeting date, so we need a buffer beyond the meeting_date cutoff to ensure
# we fetch them.
API_BUFFER_DAYS = 60


# --- HTTP / scraper ---

def make_scraper():
    return cloudscraper.create_scraper(
        browser={"browser": "chrome", "platform": "linux", "mobile": False}
    )


def api_get(scraper, url, params=None, retries=3):
    """GET a WordPress REST API URL. Returns parsed JSON or None."""
    for attempt in range(retries + 1):
        try:
            r = scraper.get(url, params=params, timeout=30)
            if r.status_code == 200:
                return r.json(), r.headers
            print(f"  HTTP {r.status_code} — {url}", file=sys.stderr)
            return None, {}
        except Exception as e:
            if attempt < retries:
                time.sleep(2 * (attempt + 1))
            else:
                print(f"  ERROR: {url}: {e}", file=sys.stderr)
    return None, {}


def download_pdf(scraper, url, dest_path, retries=3):
    """Download a PDF from url via cloudscraper. Returns True on success."""
    for attempt in range(retries + 1):
        try:
            r = scraper.get(url, timeout=60)
            if r.status_code != 200:
                print(f"  HTTP {r.status_code} — {url}", file=sys.stderr)
                return False
            ct = r.headers.get("content-type", "")
            if "pdf" not in ct.lower() and "octet-stream" not in ct.lower():
                print(f"  WARNING: unexpected content-type {ct!r}", file=sys.stderr)
                return False
            with open(dest_path, "wb") as f:
                f.write(r.content)
            return True
        except Exception as e:
            if attempt < retries:
                time.sleep(2 * (attempt + 1))
            else:
                print(f"  WARNING: {e}", file=sys.stderr)
    return False


# --- API data fetching ---

def fetch_categories(scraper):
    """Return {cat_id: cat_name} for all board categories."""
    data, _ = api_get(scraper, CATS_URL, params={"per_page": 100})
    if not data:
        return {}
    return {str(c["id"]): c["name"] for c in data}


def fetch_posts_in_window(scraper, cutoff, future_limit):
    """
    Fetch all twd_repository posts whose meeting_date falls within
    [cutoff, future_limit].

    Uses ?after= (post_modified date) as a coarse filter with a
    API_BUFFER_DAYS buffer, then filters client-side by meeting_date.
    Paginates until all pages are fetched.
    """
    after_dt = cutoff - datetime.timedelta(days=API_BUFFER_DAYS)
    after_str = after_dt.isoformat() + "T00:00:00"

    params = {
        "_embed": "1",
        "per_page": "100",
        "orderby": "modified",
        "order": "desc",
        "after": after_str,
    }

    posts = []
    page = 1
    while True:
        params["page"] = str(page)
        data, headers = api_get(scraper, API_URL, params=params)
        if not data:
            break
        posts.extend(data)
        total_pages = int(headers.get("X-WP-TotalPages", 1))
        if page >= total_pages:
            break
        page += 1
        time.sleep(DELAY_SECONDS)

    # Filter by meeting_date
    in_window = []
    for post in posts:
        raw_date = post.get("meeting_date", "")
        if not raw_date:
            continue
        try:
            meeting_date = datetime.date.fromisoformat(raw_date)
        except ValueError:
            continue
        if cutoff <= meeting_date <= future_limit:
            in_window.append(post)

    return in_window


# --- Document extraction ---

def get_attachment_url(field):
    """
    Extract a URL from a twd_repository document field.
    With _embed=1, integer IDs are resolved to WP post objects
    with a 'guid' key containing the direct URL.
    Returns the URL string or None.
    """
    if not field:
        return None
    if isinstance(field, dict):
        guid = field.get("guid", "")
        if isinstance(guid, dict):
            guid = guid.get("rendered", "")
        if guid and field.get("post_mime_type", "").startswith("application/"):
            return guid
    return None


def extract_docs(post, cat_map):
    """
    Extract all downloadable items from a twd_repository post.

    Returns a list of dicts:
      PDF items: {type: 'pdf', doc_type, url, ext}
      Media items: {type: 'media', media_type ('audio'|'video'), url}
    """
    items = []

    agenda_url = get_attachment_url(post.get("agenda"))
    if agenda_url:
        items.append({"type": "pdf", "doc_type": "agenda", "url": agenda_url})

    minutes_url = get_attachment_url(post.get("meeting_minutes"))
    if minutes_url:
        items.append({"type": "pdf", "doc_type": "minutes", "url": minutes_url})

    packet_url = get_attachment_url(post.get("agenda_pack"))
    if packet_url:
        items.append({"type": "pdf", "doc_type": "packet", "url": packet_url})

    addl_url = get_attachment_url(post.get("additional_file"))
    if addl_url:
        items.append({"type": "pdf", "doc_type": "additional", "url": addl_url})

    # additional_url is a direct external link (may be PDF or other)
    extra_url = (post.get("additional_url") or "").strip()
    if extra_url:
        items.append({"type": "pdf", "doc_type": "additional", "url": extra_url})

    sound_url = (post.get("sound") or "").strip()
    if sound_url:
        items.append({"type": "media", "media_type": "audio", "url": sound_url})

    video_url = (post.get("video") or "").strip()
    if video_url:
        items.append({"type": "media", "media_type": "video", "url": video_url})

    return items


# --- Utilities ---

def slugify(text, max_len=50):
    text = str(text).lower().strip()
    text = re.sub(r"[/\\&]", "-", text)
    text = re.sub(r"[^\w\s-]", "", text)
    text = re.sub(r"[\s_]+", "-", text)
    text = re.sub(r"-{2,}", "-", text)
    return text.strip("-")[:max_len]


def make_pdf_path(board, meeting_date, doc_type, post_id, url, output_dir):
    month_dir = os.path.join(output_dir, meeting_date.strftime("%Y-%m"))
    os.makedirs(month_dir, exist_ok=True)
    date_str = meeting_date.strftime("%Y-%m-%d")
    board_slug = slugify(board, max_len=35)
    # Use the original filename from the URL for readability
    orig = url.rstrip("/").split("/")[-1].split("?")[0][:40]
    return os.path.join(month_dir, f"{date_str}-{board_slug}-{doc_type}-{orig}")


def make_media_path(board, meeting_date, media_type, post_id, output_dir):
    month_dir = os.path.join(output_dir, meeting_date.strftime("%Y-%m"))
    os.makedirs(month_dir, exist_ok=True)
    date_str = meeting_date.strftime("%Y-%m-%d")
    board_slug = slugify(board, max_len=35)
    return os.path.join(month_dir, f"{date_str}-{board_slug}-{media_type}-{post_id}.%(ext)s")


# --- Media archive ---

def is_in_archive(archive_path, key):
    if not os.path.exists(archive_path):
        return False
    with open(archive_path) as f:
        return str(key) in {line.strip() for line in f}


def add_to_archive(archive_path, key):
    with open(archive_path, "a") as f:
        f.write(f"{key}\n")


# --- yt-dlp download ---

def download_media_ytdlp(url, outtmpl, retries=2):
    """Download audio/video from url using yt-dlp. Returns True on success."""
    for attempt in range(retries + 1):
        cmd = [
            "yt-dlp", "--js-runtimes", YT_DLP_NODE,
            "--no-playlist",
            "--quiet",
            "--no-warnings",
            "-o", outtmpl,
            url,
        ]
        result = subprocess.run(cmd, capture_output=True, text=True)
        if result.returncode == 0:
            return True
        if attempt < retries:
            time.sleep(3)
    err = result.stderr.strip()[:120] if result.stderr else "unknown error"
    print(f"  WARNING: yt-dlp failed: {err}", file=sys.stderr)
    return False


# --- Main ---

def main():
    parser = argparse.ArgumentParser(
        description=(
            "Download Windsor Locks CT municipal agendas, minutes, and Dropbox "
            "audio/video recordings for meetings within the past N days "
            "(and up to 7 days ahead)."
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
        help="List matching items without downloading",
    )
    parser.add_argument(
        "--board", metavar="NAME",
        help="Only include boards whose name contains NAME (case-insensitive)",
    )
    parser.add_argument(
        "--no-minutes", action="store_true",
        help="Skip minutes, download agendas only",
    )
    parser.add_argument(
        "--no-agendas", action="store_true",
        help="Skip agendas, download minutes only",
    )
    parser.add_argument(
        "--docs-only", action="store_true",
        help="Download PDFs only, skip audio/video recordings",
    )
    parser.add_argument(
        "--recordings-only", action="store_true",
        help="Download recordings only, skip PDFs",
    )
    args = parser.parse_args()

    now = datetime.datetime.now()
    if (now.weekday() == 5 and now.hour >= 18) or (now.weekday() == 6 and now.hour < 12):  # Saturday night, Sunday morning
        print("Skipping — no downloads on Saturday nights or Sunday mornings.")
        sys.exit(0)

    today = datetime.date.today()
    cutoff = today - datetime.timedelta(days=args.days)
    future_limit = today + datetime.timedelta(days=args.ahead)
    board_filter = args.board.lower() if args.board else None

    print(f"Date window : {cutoff} to {future_limit}")
    print(f"Site        : {BASE_URL}")
    if not args.dry_run:
        print(f"Output dir  : {args.output_dir}")
    print()

    scraper = make_scraper()

    # --- Fetch category map ---
    print("Fetching board categories...")
    cat_map = fetch_categories(scraper)
    if not cat_map:
        print("FATAL: Could not fetch board categories.", file=sys.stderr)
        sys.exit(1)
    print(f"  Found {len(cat_map)} board(s).\n")

    # --- Fetch posts in window ---
    print("Fetching meeting repository posts...")
    posts = fetch_posts_in_window(scraper, cutoff, future_limit)
    print(f"  Found {len(posts)} meeting(s) in date window.\n")

    if not posts:
        print("No meetings found in the date window.")
        return

    # --- Apply board filter ---
    if board_filter:
        def board_name_for(post):
            for cid in post.get("twd_repository_cat", []):
                name = cat_map.get(str(cid), "")
                if board_filter in name.lower():
                    return True
            return False
        posts = [p for p in posts if board_name_for(p)]
        print(f"  Board filter '{args.board}' → {len(posts)} meeting(s).\n")

    # --- Build flat item list ---
    all_items = []
    for post in posts:
        post_id = post["id"]
        raw_date = post.get("meeting_date", "")
        try:
            meeting_date = datetime.date.fromisoformat(raw_date)
        except ValueError:
            continue

        title = post["title"]["rendered"]
        cat_ids = post.get("twd_repository_cat", [])
        board = cat_map.get(str(cat_ids[0]), "Unknown") if cat_ids else "Unknown"

        doc_items = extract_docs(post, cat_map)
        for item in doc_items:
            # Apply doc-type filters
            if item["type"] == "pdf":
                doc_type = item["doc_type"]
                if args.no_agendas and doc_type == "agenda":
                    continue
                if args.no_minutes and doc_type == "minutes":
                    continue
                if args.recordings_only:
                    continue
            else:
                if args.docs_only:
                    continue

            all_items.append({
                "post_id": post_id,
                "meeting_date": meeting_date,
                "board": board,
                "title": title,
                **item,
            })

    all_items.sort(key=lambda x: (x["meeting_date"], x["board"]))

    if not all_items:
        print("No items found after applying filters.")
        return

    # --- Dry-run listing ---
    if args.dry_run:
        pdf_items = [i for i in all_items if i["type"] == "pdf"]
        media_items = [i for i in all_items if i["type"] == "media"]

        if pdf_items:
            print(f"{'Board':<38} {'Date':<12} {'Type':<12} URL")
            print("-" * 100)
            for d in pdf_items:
                url_short = d["url"].split("/")[-1].split("?")[0][:40]
                print(
                    f"{d['board'][:37]:<38} "
                    f"{d['meeting_date']!s:<12} "
                    f"{d['doc_type']:<12} "
                    f"{url_short}"
                )
            print()

        if media_items:
            print(f"{'Board':<38} {'Date':<12} {'MediaType':<10} URL")
            print("-" * 100)
            for d in media_items:
                url_short = d["url"][:60]
                print(
                    f"{d['board'][:37]:<38} "
                    f"{d['meeting_date']!s:<12} "
                    f"{d['media_type']:<10} "
                    f"{url_short}"
                )
            print()

        total = len(all_items)
        print(f"{total} item(s) matched ({len(pdf_items)} PDFs, {len(media_items)} recordings).")
        print("Re-run without --dry-run to download.")
        return

    # --- Download ---
    os.makedirs(args.output_dir, exist_ok=True)
    log_path = os.path.join(args.output_dir, "download-log.txt")
    archive_path = os.path.join(args.output_dir, "media-archive.txt")
    log_lines = []
    downloaded = skipped = failed = 0

    pdf_items = [i for i in all_items if i["type"] == "pdf"]
    media_items = [i for i in all_items if i["type"] == "media"]

    # Download PDFs
    if pdf_items:
        print(f"Downloading {len(pdf_items)} PDF(s)...")
        for d in pdf_items:
            dest = make_pdf_path(
                d["board"], d["meeting_date"], d["doc_type"], d["post_id"],
                d["url"], args.output_dir,
            )
            label = os.path.basename(dest)

            if os.path.exists(dest):
                print(f"  skip (exists)  {label}")
                skipped += 1
                continue

            print(f"  [{d['meeting_date']}] {d['board'][:40]} — {d['doc_type']}")
            print(f"  downloading    {label}")

            if download_pdf(scraper, d["url"], dest):
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
            time.sleep(DELAY_SECONDS)
        print()

    # Download recordings via yt-dlp
    if media_items:
        print(f"Downloading {len(media_items)} recording(s) via yt-dlp...")
        for d in media_items:
            archive_key = f"{d['post_id']}-{d['media_type']}"

            if is_in_archive(archive_path, archive_key):
                print(f"  skip (archive) {d['board'][:40]} {d['media_type']} (post {d['post_id']})")
                skipped += 1
                continue

            outtmpl = make_media_path(
                d["board"], d["meeting_date"], d["media_type"],
                d["post_id"], args.output_dir,
            )
            label = os.path.basename(outtmpl.replace("%(ext)s", d["media_type"]))

            print(f"  [{d['meeting_date']}] {d['board'][:40]} — {d['media_type']}")
            print(f"  downloading    {label}")

            if download_media_ytdlp(d["url"], outtmpl):
                downloaded += 1
                add_to_archive(archive_path, archive_key)
                log_lines.append(
                    f"{datetime.datetime.now().isoformat()}  OK       {outtmpl}"
                )
            else:
                failed += 1
                log_lines.append(
                    f"{datetime.datetime.now().isoformat()}  FAILED   {d['url']}"
                )
            time.sleep(DELAY_SECONDS)
        print()

    if log_lines:
        with open(log_path, "a") as f:
            f.write("\n".join(log_lines) + "\n")

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
#    python3 scripts/download-windsor-locks-agendas.py --dry-run
#
# 2. Narrow to one board:
#    python3 scripts/download-windsor-locks-agendas.py --board "Board of Selectmen"
#
# 3. PDFs only (no audio/video downloads):
#    python3 scripts/download-windsor-locks-agendas.py --docs-only
#
# 4. Recordings only:
#    python3 scripts/download-windsor-locks-agendas.py --recordings-only
#
# 5. Agendas only (skip minutes):
#    python3 scripts/download-windsor-locks-agendas.py --no-minutes
#
# 6. Change the lookback window:
#    python3 scripts/download-windsor-locks-agendas.py --days 60
#
# 7. Run on a schedule (cron — 7 AM daily):
#    0 7 * * * cd /path/to/repo && python3 scripts/download-windsor-locks-agendas.py
#
# NOTES:
#   - Windsor Locks uses a custom WordPress theme and plugin (tw-meeting-repository
#     by TownWeb). Documents and recordings are managed through a custom post type
#     (twd_repository) exposed via the WordPress REST API — there is no CivicPlus
#     AgendaCenter or Granicus portal.
#   - The main site (windsorlocksct.org) is protected by Cloudflare bot detection.
#     cloudscraper is required for API calls and PDF downloads. The WordPress REST
#     API itself is accessible because it returns JSON (not subject to the JS
#     challenge), but PDF download URLs on the same domain require the bypass.
#   - Audio/video recordings are hosted as Dropbox shared links (?dl=0 format).
#     yt-dlp handles Dropbox natively. A media-archive.txt file tracks downloaded
#     recordings by post-ID + media type to prevent re-downloads.
#   - The REST API ?after= parameter filters by post_modified date, not meeting_date.
#     A 60-day buffer (API_BUFFER_DAYS) is added before the lookback cutoff so that
#     agendas uploaded weeks before their meeting date are still captured.
#   - Some posts have 'meeting_minutes' or 'additional_file' stored as integer
#     post IDs rather than resolved objects. Using _embed=1 causes WordPress to
#     resolve these to full attachment objects before returning, giving direct URLs.
#   - Not all boards record every meeting. As of 2026, Board of Selectmen posts
#     MP4 videos and Board of Finance posts M4A/MP3 audio; other boards may vary.
