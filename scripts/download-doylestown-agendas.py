#!/usr/bin/env python3
# download-doylestown-agendas.py
# Download municipal meeting agendas and minutes from the Borough of Doylestown,
# PA website (doylestownborough.net) for documents posted in the last N days.
#
# USAGE:
#   python3 scripts/download-doylestown-agendas.py [options]
#
# REQUIREMENTS:
#   - Python 3.6+  (no third-party packages needed)
#   - Internet connection
#
# WHAT IT DOES:
#   1. Fetches the upcoming meetings page (doylestownborough.net/meetings) and
#      the archive page (doylestownborough.net/archive), paginating through both
#      until no more pages exist.
#   2. Collects all document links (PDFs on storage.googleapis.com/dtown/...)
#      labeled as agenda or minutes.
#   3. Issues a HEAD request for each PDF to read its Last-Modified header —
#      Google Cloud Storage returns this as the upload timestamp.
#   4. Downloads PDFs whose Last-Modified falls within the date window to
#      beat-archive/doylestown-agendas/YYYY-MM/
#   5. Appends a download log to beat-archive/doylestown-agendas/download-log.txt
#
# SITE STRUCTURE (custom CMS, PDFs on Google Cloud Storage):
#   Base:     https://www.doylestownborough.net
#   Upcoming: /meetings  (paginated: /meetings/p2/, /meetings/p3/, ...)
#   Archive:  /archive   (paginated: /archive/p2/, /archive/p3/, ...)
#   PDFs:     https://storage.googleapis.com/dtown/...
#
#   Each page lists meeting cards with <time datetime="YYYY-MM-DDTHH:MM:SS...">
#   and document links labeled with <h5 class="agenda"> or <h5 class="minutes">.
#
# NOTE: The site has no published video or audio recordings. The h5 class label
# ("agenda" or "minutes") is used to classify each downloaded document.
#
# NOTE: The Last-Modified header on GCS PDFs reflects the upload timestamp,
# which is the authoritative "posted" date. Meeting date is not used for
# filtering — a document uploaded today for a past meeting is included.
#
# NOTE: Each archive page holds roughly 20–30 meetings. With a 3-day posted
# window, scanning 4 archive pages plus the full meetings page is conservative
# coverage for any normal posting cadence.

import argparse
import datetime
import email.utils
import html
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

# --- Configuration ---
BASE_URL   = "https://www.doylestownborough.net"
GCS_PREFIX = "https://storage.googleapis.com/dtown/"
OUTPUT_DIR = "beat-archive/doylestown-agendas"
DAYS_BACK  = 3
PAGE_DELAY    = 0.5
HEAD_DELAY    = 0.25
DOWNLOAD_DELAY = 0.8
MAX_ARCHIVE_PAGES = 4   # 3-day window rarely needs more

UA = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36"
)

# Matches GCS PDF links in meeting/archive cards
_PDF_LINK_RE = re.compile(
    r'<a\s+href="(https://storage\.googleapis\.com/dtown/[^"]+\.pdf)"[^>]*>'
    r'\s*<h5\s+class="(agenda|minutes)"',
    re.IGNORECASE | re.DOTALL,
)

# Extracts meeting body/name from h3 in a card (strip HTML tags)
_H3_RE = re.compile(r"<h3[^>]*>(.*?)</h3>", re.DOTALL | re.IGNORECASE)

# Detects "Next Page" link to handle pagination
_NEXT_PAGE_RE = re.compile(
    r'href="(/(?:meetings|archive)/p(\d+)/)"',
    re.IGNORECASE,
)


# --- HTTP helpers ---

def fetch_html(url):
    req = urllib.request.Request(
        url,
        headers={"User-Agent": UA, "Accept": "text/html,*/*"},
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            if r.status != 200:
                return None
            return r.read().decode(r.headers.get_content_charset() or "utf-8", errors="replace")
    except urllib.error.HTTPError as e:
        if e.code != 404:
            print(f"  HTTP {e.code} — {url}", file=sys.stderr)
        return None
    except urllib.error.URLError as e:
        print(f"  ERROR fetching {url}: {e}", file=sys.stderr)
        return None


def head_last_modified(url):
    """Return the Last-Modified date of a GCS URL as datetime.date, or None."""
    req = urllib.request.Request(
        url,
        headers={"User-Agent": UA, "Accept": "application/pdf,*/*"},
        method="HEAD",
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            lm = r.headers.get("Last-Modified")
            if lm:
                return email.utils.parsedate_to_datetime(lm).date()
    except Exception:
        pass
    return None


def download_pdf(url, dest_path):
    req = urllib.request.Request(
        url,
        headers={"User-Agent": UA, "Accept": "application/pdf,*/*"},
    )
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            with open(dest_path, "wb") as f:
                f.write(r.read())
        return True
    except Exception as e:
        print(f"  WARNING: {e}", file=sys.stderr)
        return False


# --- Parsing ---

def parse_cards(html_text):
    """
    Extract (pdf_url, doc_type, meeting_label) tuples from meeting/archive cards.
    doc_type is 'agenda' or 'minutes'.
    """
    results = []
    seen_urls = set()

    for m in _PDF_LINK_RE.finditer(html_text):
        pdf_url  = m.group(1)
        doc_type = m.group(2).lower()

        if pdf_url in seen_urls:
            continue
        seen_urls.add(pdf_url)

        # Find the nearest h3 before this link to label the card
        text_before = html_text[:m.start()]
        h3_matches = list(_H3_RE.finditer(text_before))
        if h3_matches:
            raw = h3_matches[-1].group(1)
            label = re.sub(r"<[^>]+>", "", raw).strip()
            label = html.unescape(re.sub(r"\s+", " ", label).strip())
        else:
            label = "Unknown"

        results.append((pdf_url, doc_type, label))

    return results


def slugify(text, max_len=50):
    text = text.lower().strip()
    text = re.sub(r"[/\\&]", "-", text)
    text = re.sub(r"[^\w\s-]", "", text)
    text = re.sub(r"[\s_]+", "-", text)
    text = re.sub(r"-{2,}", "-", text)
    return text.strip("-")[:max_len]


def make_dest(doc_type, meeting_label, date_posted, output_dir, counter=0):
    month_dir = os.path.join(output_dir, date_posted.strftime("%Y-%m"))
    os.makedirs(month_dir, exist_ok=True)
    suffix = f"-{counter}" if counter > 0 else ""
    slug = slugify(meeting_label)
    fname = f"{date_posted.strftime('%Y-%m-%d')}-{slug}-{doc_type}{suffix}.pdf"
    return os.path.join(month_dir, fname)


# --- Page scraping ---

def collect_from_section(section_path, max_pages, label):
    """Fetch all pages of a section (meetings or archive) and return candidate list."""
    candidates = []
    page = 1
    while page <= max_pages:
        if page == 1:
            url = f"{BASE_URL}{section_path}"
        else:
            url = f"{BASE_URL}{section_path}/p{page}/"
        print(f"  Fetching {url} ...", end=" ", flush=True)
        html = fetch_html(url)
        if not html:
            print("(no content, stopping)")
            break

        cards = parse_cards(html)
        print(f"{len(cards)} document link(s)")
        candidates.extend(cards)

        # Check if there is a next page
        next_links = _NEXT_PAGE_RE.findall(html)
        if not next_links:
            break
        page += 1
        time.sleep(PAGE_DELAY)

    return candidates


# --- Main ---

def main():
    parser = argparse.ArgumentParser(
        description=(
            "Download Doylestown PA Borough meeting agendas and minutes "
            "posted in the past N days."
        )
    )
    parser.add_argument(
        "--days", type=int, default=DAYS_BACK, metavar="N",
        help=f"Look back N days for documents (default: {DAYS_BACK})",
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
        "--max-pages", type=int, default=MAX_ARCHIVE_PAGES, metavar="N",
        help=f"Maximum archive pages to scan (default: {MAX_ARCHIVE_PAGES})",
    )
    args = parser.parse_args()

    today  = datetime.date.today()
    cutoff = today - datetime.timedelta(days=args.days)

    print(f"Date window : {cutoff} to {today}")
    print(f"Source      : {BASE_URL}")
    if not args.dry_run:
        print(f"Output dir  : {args.output_dir}")
    print()

    # ------------------------------------------------------------------ #
    # Phase 1: collect all document links                                  #
    # ------------------------------------------------------------------ #

    all_candidates = []

    print("=== Upcoming meetings ===")
    all_candidates.extend(collect_from_section("/meetings", max_pages=5, label="meetings"))
    print()

    print("=== Archive ===")
    all_candidates.extend(collect_from_section("/archive", max_pages=args.max_pages, label="archive"))
    print()

    # Deduplicate by URL
    seen = set()
    deduped = []
    for item in all_candidates:
        if item[0] not in seen:
            seen.add(item[0])
            deduped.append(item)
    all_candidates = deduped

    print(f"Total unique document links found: {len(all_candidates)}")
    print()

    # ------------------------------------------------------------------ #
    # Phase 2: HEAD requests to get Last-Modified dates                    #
    # ------------------------------------------------------------------ #

    print("Checking Last-Modified dates...")
    confirmed = []
    fname_counters: dict = {}

    for pdf_url, doc_type, meeting_label in all_candidates:
        lm = head_last_modified(pdf_url)
        time.sleep(HEAD_DELAY)
        if lm is None or lm < cutoff:
            continue

        key = (meeting_label, doc_type, lm)
        fname_counters[key] = fname_counters.get(key, 0) + 1
        counter = fname_counters[key] - 1

        confirmed.append({
            "url":           pdf_url,
            "doc_type":      doc_type,
            "meeting_label": meeting_label,
            "last_modified": lm,
            "counter":       counter,
        })

    confirmed.sort(key=lambda x: x["last_modified"], reverse=True)
    print(f"  {len(confirmed)} document(s) posted within {args.days} day(s).")
    print()

    if not confirmed:
        print("No documents found within the date window.")
        return

    # ------------------------------------------------------------------ #
    # Phase 3: report or download                                          #
    # ------------------------------------------------------------------ #

    if args.dry_run:
        print(f"{'Meeting':<45} {'Posted':<12} Type")
        print("-" * 68)
        for item in confirmed:
            label = item["meeting_label"][:44]
            print(f"{label:<45} {item['last_modified']!s:<12} {item['doc_type']}")
        print(f"\n{len(confirmed)} document(s). Re-run without --dry-run to download.")
        return

    os.makedirs(args.output_dir, exist_ok=True)
    log_path  = os.path.join(args.output_dir, "download-log.txt")
    log_lines = []
    downloaded = skipped = failed = 0

    for item in confirmed:
        dest  = make_dest(
            item["doc_type"], item["meeting_label"],
            item["last_modified"], args.output_dir, item["counter"]
        )
        label = os.path.basename(dest)

        if os.path.exists(dest):
            print(f"  skip (exists)  {label}")
            skipped += 1
            continue

        print(f"  [posted {item['last_modified']}] {item['meeting_label']} — {item['doc_type']}")
        print(f"  downloading    {label}")

        if download_pdf(item["url"], dest):
            downloaded += 1
            log_lines.append(f"{datetime.datetime.now().isoformat()}  OK       {dest}")
        else:
            failed += 1
            log_lines.append(f"{datetime.datetime.now().isoformat()}  FAILED   {item['url']}")
            if os.path.exists(dest):
                os.remove(dest)

        time.sleep(DOWNLOAD_DELAY)

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
#    python3 scripts/download-doylestown-agendas.py --dry-run
#
# 2. Widen the lookback window:
#    python3 scripts/download-doylestown-agendas.py --days 7
#
# 3. Scan more archive pages (e.g. for a wider window):
#    python3 scripts/download-doylestown-agendas.py --days 14 --max-pages 8
#
# 4. Save files somewhere else:
#    python3 scripts/download-doylestown-agendas.py --output-dir ~/Downloads/doylestown
#
# 5. Run on a schedule (cron — nightly at 8:14 PM):
#    14 20 * * 0-5 cd /path/to/repo && python3 scripts/download-doylestown-agendas.py
#
# BOARDS / COMMITTEES (as of 2026):
#   Borough Council, Planning Commission, Zoning Hearing Board,
#   Historical & Architectural Review Board, Park & Recreation Board,
#   Finance & Pension Committee, Public Safety Committee, Public Works & Admin,
#   Water Utility Committee, Community & Government Affairs,
#   Zoning & Planning Committee, Environmental Advisory Council,
#   Environmental & Recreation Committee, Human Relations Commission,
#   Emergency Preparedness & Communications Board, Fanny Chapman Pool Board,
#   Personnel Committee, Shade Tree Commission, and others.
