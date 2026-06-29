#!/usr/bin/env python3
# download-lyme-agendas.py
# Download municipal meeting agendas, minutes, and recording links from Lyme CT.
#
# USAGE:
#   python3 scripts/download-lyme-agendas.py [options]
#
# REQUIREMENTS:
#   - Python 3.6+  (no third-party packages needed)
#   - Internet connection
#
# WHAT IT DOES:
#   1. Queries the townlyme.org WordPress REST API for posts in the Agendas
#      (cat 9) and Minutes (cat 14) categories posted in the last N days
#   2. Parses each post's HTML content for PDF, audio/video, and recording URLs
#   3. Downloads PDFs and media files to beat-archive/lyme-agendas/YYYY-MM/
#   4. Saves Zoom/YouTube/GoToMeeting recording URLs as .url Internet Shortcut files
#   5. Appends a download log to beat-archive/lyme-agendas/download-log.txt
#
# SITE STRUCTURE (WordPress):
#   REST API : https://townlyme.org/wp-json/wp/v2/posts
#   Agendas  : category ID 9  (/category/agendas/)
#   Minutes  : category ID 14 (/category/minutes/)
#   Files    : https://townlyme.org/wp-content/uploads/YYYY/MM/filename.pdf
#
# NOTE: No Cloudflare or JS challenge — plain urllib requests work fine.

import argparse
import datetime
import html
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

# --- Configuration ---
BASE_URL = "https://townlyme.org"
API_URL = f"{BASE_URL}/wp-json/wp/v2/posts"
CATEGORY_AGENDAS = 9
CATEGORY_MINUTES = 14
OUTPUT_DIR = "beat-archive/lyme-agendas"
DAYS_BACK = 3
DOWNLOAD_DELAY = 0.8
PAGE_SIZE = 100  # WordPress max per_page

UA = "Lyme-CT-Agendas-Downloader/1.0 (journalism research)"

# File extensions to download directly
_DOWNLOAD_EXTS = re.compile(
    r"\.(pdf|docx?|mp3|mp4|m4a|wav|ogg|webm|mov|avi)(\?|$)", re.IGNORECASE
)

# Recording URLs to save as .url shortcut files (not downloadable binaries)
_RECORDING_URL_RE = re.compile(
    r'href="(https?://(?:'
    r'(?:[\w-]+\.)?zoom\.us/[^\s"<>]+'
    r'|youtu\.be/[^\s"<>]+'
    r'|(?:www\.)?youtube\.com/[^\s"<>]+'
    r'|(?:www\.)?vimeo\.com/[^\s"<>]+'
    r'|transcripts\.gotomeeting\.com/[^\s"<>]+'
    r'|(?:www\.)?gotomeeting\.com/[^\s"<>]+'
    r'))"',
    re.IGNORECASE,
)

# All hrefs (to catch local file links)
_HREF_RE = re.compile(r'href="([^"]+)"', re.IGNORECASE)


# --- HTTP helpers ---

def fetch_json(url, retries=2):
    req = urllib.request.Request(
        url,
        headers={"User-Agent": UA, "Accept": "application/json"},
    )
    for attempt in range(retries + 1):
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                return json.loads(r.read().decode("utf-8", errors="replace")), r.headers
        except urllib.error.HTTPError as e:
            print(f"  HTTP {e.code} fetching {url}", file=sys.stderr)
            if attempt < retries:
                time.sleep(1)
        except urllib.error.URLError as e:
            print(f"  ERROR fetching {url}: {e}", file=sys.stderr)
            if attempt < retries:
                time.sleep(1)
    return None, None


def download_file(url, dest_path):
    """Download a binary file (PDF, audio, video) to dest_path. Returns True on success."""
    parsed = urllib.parse.urlsplit(url)
    encoded_path = urllib.parse.quote(parsed.path, safe="/:@!$&'()*+,;=")
    safe_url = urllib.parse.urlunsplit(parsed._replace(path=encoded_path))
    req = urllib.request.Request(
        safe_url,
        headers={"User-Agent": UA, "Accept": "*/*"},
    )
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            with open(dest_path, "wb") as f:
                f.write(r.read())
        return True
    except Exception as e:
        print(f"  WARNING: {e}", file=sys.stderr)
        return False


def save_url_shortcut(recording_url, dest_path):
    """Save a web recording URL as a Windows .url Internet Shortcut (openable in any browser)."""
    try:
        with open(dest_path, "w") as f:
            f.write(f"[InternetShortcut]\nURL={recording_url}\n")
        return True
    except Exception as e:
        print(f"  WARNING: {e}", file=sys.stderr)
        return False


# --- WordPress API ---

def fetch_posts(categories, after_dt, page=1):
    """Fetch one page of posts from the WordPress REST API."""
    params = urllib.parse.urlencode({
        "categories": ",".join(str(c) for c in categories),
        "after": after_dt.isoformat() + "Z",
        "per_page": PAGE_SIZE,
        "page": page,
        "orderby": "date",
        "order": "desc",
        "_fields": "id,date,title,link,content,categories",
    })
    url = f"{API_URL}?{params}"
    data, headers = fetch_json(url)
    total_pages = 1
    if headers:
        try:
            total_pages = int(headers.get("X-WP-TotalPages", 1))
        except (ValueError, TypeError):
            pass
    return data or [], total_pages


def get_all_posts(categories, after_dt):
    """Fetch all pages of posts matching the category/date filter."""
    all_posts = []
    page = 1
    while True:
        posts, total_pages = fetch_posts(categories, after_dt, page=page)
        if not posts:
            break
        all_posts.extend(posts)
        if page >= total_pages:
            break
        page += 1
        time.sleep(0.3)
    return all_posts


# --- Content parsing ---

def classify_recording_url(url):
    """Return a short label for a recording URL based on its host."""
    low = url.lower()
    if "zoom.us" in low:
        return "zoom"
    if "youtube.com" in low or "youtu.be" in low:
        return "youtube"
    if "vimeo.com" in low:
        return "vimeo"
    if "gotomeeting.com" in low:
        return "gotomeeting"
    return "recording"


def extract_docs_from_post(post):
    """
    Parse a post's HTML content for downloadable files and recording URLs.
    Returns list of dicts: {title, post_date, doc_type, url, is_recording, category_type}
    """
    content_html = post.get("content", {}).get("rendered", "")
    post_date_str = post.get("date", "")
    post_title = html.unescape(post.get("title", {}).get("rendered", ""))
    cats = post.get("categories", [])

    try:
        post_date = datetime.datetime.fromisoformat(post_date_str).date()
    except (ValueError, TypeError):
        post_date = None

    category_type = "minutes" if CATEGORY_MINUTES in cats else "agenda"

    docs = []
    seen_urls = set()

    # Recording URLs (Zoom, YouTube, Vimeo, GoToMeeting)
    for m in _RECORDING_URL_RE.finditer(content_html):
        url = m.group(1)
        if url not in seen_urls:
            seen_urls.add(url)
            docs.append({
                "title": post_title,
                "post_date": post_date,
                "doc_type": classify_recording_url(url),
                "url": url,
                "is_recording": True,
                "category_type": category_type,
            })

    # All hrefs — pick out downloadable file types
    for m in _HREF_RE.finditer(content_html):
        url = m.group(1)

        # Resolve relative URLs
        if url.startswith("/wp-content/"):
            url = BASE_URL + url
        elif not url.startswith("http"):
            continue

        if not _DOWNLOAD_EXTS.search(url):
            continue

        if url in seen_urls:
            continue
        seen_urls.add(url)

        # Classify by extension
        ext_m = _DOWNLOAD_EXTS.search(url)
        ext = ext_m.group(1).lower() if ext_m else "pdf"
        if ext == "pdf":
            doc_type = category_type  # agenda or minutes
        elif ext in ("mp3", "mp4", "m4a", "wav", "ogg", "webm", "mov", "avi"):
            doc_type = "media"
        else:
            doc_type = "document"

        docs.append({
            "title": post_title,
            "post_date": post_date,
            "doc_type": doc_type,
            "url": url,
            "is_recording": False,
            "category_type": category_type,
        })

    return docs


# --- File naming ---

def slugify(text, max_len=60):
    text = re.sub(r"<[^>]+>", "", text)  # strip HTML tags
    text = text.lower().strip()
    text = re.sub(r"[&/\\]", "-", text)
    text = re.sub(r"[^\w\s-]", "", text)
    text = re.sub(r"[\s_]+", "-", text)
    text = re.sub(r"-{2,}", "-", text)
    return text.strip("-")[:max_len]


def make_dest_path(doc, output_dir, counter=0):
    post_date = doc["post_date"]

    if post_date:
        month_dir = os.path.join(output_dir, post_date.strftime("%Y-%m"))
        date_prefix = post_date.strftime("%Y-%m-%d")
    else:
        month_dir = os.path.join(output_dir, "undated")
        date_prefix = "undated"

    os.makedirs(month_dir, exist_ok=True)
    title_slug = slugify(doc["title"])
    suffix = f"-{counter}" if counter > 0 else ""

    if doc["is_recording"]:
        label = classify_recording_url(doc["url"])
        return os.path.join(month_dir, f"{date_prefix}-{title_slug}-{label}{suffix}.url")

    # For binary files, use the original filename from the URL
    url_path = urllib.parse.urlsplit(doc["url"]).path
    filename = os.path.basename(urllib.parse.unquote(url_path))
    if not filename:
        filename = f"{title_slug}.pdf"
    ext = os.path.splitext(filename)[1].lower() or ".pdf"

    # Use the original filename (it's already descriptive); prefix with date
    safe_filename = re.sub(r"[^\w.\-]", "-", filename)
    return os.path.join(month_dir, f"{date_prefix}-{safe_filename}{suffix}")


# --- Main ---

def main():
    parser = argparse.ArgumentParser(
        description="Download Lyme CT agendas, minutes, and recording links posted in the past N days.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--days", type=int, default=DAYS_BACK, metavar="N",
        help=f"Look back N days by post date (default: {DAYS_BACK})",
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
        "--no-minutes", action="store_true",
        help="Skip minutes posts",
    )
    parser.add_argument(
        "--no-agendas", action="store_true",
        help="Skip agenda posts",
    )
    parser.add_argument(
        "--no-recordings", action="store_true",
        help="Skip Zoom/YouTube/GoToMeeting recording links",
    )
    args = parser.parse_args()

    now = datetime.datetime.now()
    if (now.weekday() == 5 and now.hour >= 18) or (now.weekday() == 6 and now.hour < 12):
        print("Skipping — no downloads on Saturday nights or Sunday mornings.")
        sys.exit(0)

    after_dt = datetime.datetime.combine(
        datetime.date.today() - datetime.timedelta(days=args.days),
        datetime.time.min,
    )

    categories = []
    if not args.no_agendas:
        categories.append(CATEGORY_AGENDAS)
    if not args.no_minutes:
        categories.append(CATEGORY_MINUTES)

    if not categories:
        print("ERROR: --no-agendas and --no-minutes together leave nothing to fetch.", file=sys.stderr)
        sys.exit(1)

    print(f"Looking back  : {args.days} days (posts since {after_dt.date()})")
    print(f"Categories    : {', '.join(str(c) for c in categories)} (agendas=9, minutes=14)")
    print(f"API endpoint  : {API_URL}")
    if not args.dry_run:
        print(f"Output dir    : {args.output_dir}")
    print()

    print("Fetching posts from WordPress API...")
    posts = get_all_posts(categories, after_dt)

    if not posts:
        print("No posts found in the date window.")
        sys.exit(0)

    print(f"  Found {len(posts)} post(s).")
    print()

    # Extract all documents from all posts
    all_docs = []
    for post in posts:
        docs = extract_docs_from_post(post)
        for doc in docs:
            if args.no_recordings and doc["is_recording"]:
                continue
            all_docs.append(doc)

    all_docs.sort(key=lambda x: x["post_date"] or datetime.date.min, reverse=True)

    print(f"Found {len(all_docs)} document(s)/recording(s) across {len(posts)} post(s).")
    print()

    if not all_docs:
        print("No downloadable documents or recordings found.")
        sys.exit(0)

    if args.dry_run:
        print(f"{'Type':<12} {'Posted':<12} {'Doc'}")
        print("-" * 78)
        for doc in all_docs:
            dt = str(doc["post_date"]) if doc["post_date"] else "?"
            label = "RECORDING" if doc["is_recording"] else doc["doc_type"].upper()
            title_preview = re.sub(r"<[^>]+>", "", doc["title"])[:55]
            url_preview = doc["url"][-40:] if doc["is_recording"] else os.path.basename(doc["url"])[:40]
            print(f"{label:<12} {dt:<12} {title_preview}")
            print(f"{'':12} {'':12}   → {url_preview}")
        print(f"\n{len(all_docs)} item(s) matched. Re-run without --dry-run to download.")
        return

    # --- Download ---
    os.makedirs(args.output_dir, exist_ok=True)
    log_path = os.path.join(args.output_dir, "download-log.txt")
    log_lines = []
    downloaded = skipped = failed = 0
    seen_dests = {}

    for doc in all_docs:
        dest = make_dest_path(doc, args.output_dir)
        count = seen_dests.get(dest, 0)
        seen_dests[dest] = count + 1
        if count > 0:
            dest = make_dest_path(doc, args.output_dir, counter=count)

        label = os.path.basename(dest)

        if os.path.exists(dest):
            print(f"  skip (exists)  {label}")
            skipped += 1
            continue

        title_plain = re.sub(r"<[^>]+>", "", doc["title"])
        dt = str(doc["post_date"]) if doc["post_date"] else "?"
        print(f"  [{dt}] {title_plain[:60]}")

        if doc["is_recording"]:
            print(f"  saving url     {label}")
            if save_url_shortcut(doc["url"], dest):
                downloaded += 1
                log_lines.append(
                    f"{datetime.datetime.now().isoformat()}  OK (url)  {dest}"
                )
            else:
                failed += 1
                log_lines.append(
                    f"{datetime.datetime.now().isoformat()}  FAILED   {doc['url']}"
                )
        else:
            print(f"  downloading    {label}")
            if download_file(doc["url"], dest):
                downloaded += 1
                log_lines.append(
                    f"{datetime.datetime.now().isoformat()}  OK       {dest}"
                )
            else:
                failed += 1
                log_lines.append(
                    f"{datetime.datetime.now().isoformat()}  FAILED   {doc['url']}"
                )
                if os.path.exists(dest):
                    os.remove(dest)

        time.sleep(DOWNLOAD_DELAY)

    if log_lines:
        with open(log_path, "a") as f:
            f.write("\n".join(log_lines) + "\n")

    print()
    print(f"Done — downloaded: {downloaded}  skipped: {skipped}  failed: {failed}")
    if downloaded + skipped:
        print(f"Files in : {args.output_dir}")
    if log_lines:
        print(f"Log      : {log_path}")


if __name__ == "__main__":
    main()


# --- Tips ---
#
# 1. Preview without downloading (default: last 3 days):
#    python3 scripts/download-lyme-agendas.py --dry-run
#
# 2. Extend the lookback window:
#    python3 scripts/download-lyme-agendas.py --days 7 --dry-run
#
# 3. Agendas only (skip minutes):
#    python3 scripts/download-lyme-agendas.py --no-minutes
#
# 4. Skip recording URL shortcuts:
#    python3 scripts/download-lyme-agendas.py --no-recordings
#
# 5. Save files to a custom directory:
#    python3 scripts/download-lyme-agendas.py --output-dir ~/Downloads/lyme-meetings
#
# 6. Run on a schedule (cron — 8 PM daily):
#    0 20 * * * cd /path/to/repo && python3 scripts/download-lyme-agendas.py
#
# SITE NOTES:
#   - townlyme.org runs WordPress; the REST API is at /wp-json/wp/v2/posts.
#   - Agendas: category ID 9 (/category/agendas/)
#   - Minutes: category ID 14 (/category/minutes/)
#   - There is no separate recordings category; Zoom/YouTube links are embedded
#     in post content alongside PDF attachments and are saved as .url shortcuts.
#   - All files are served from /wp-content/uploads/ with no authentication required.
#   - The `after` parameter filters by post publication date (UTC), not meeting date.
