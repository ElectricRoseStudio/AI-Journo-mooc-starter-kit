#!/usr/bin/env python3
# download-plumstead-twp-agendas.py
# Download meeting agendas and minutes from Plumstead Township, PA
# (plumstead.org) for documents posted in the last N days.
#
# USAGE:
#   python3 scripts/download-plumstead-twp-agendas.py [options]
#
# REQUIREMENTS:
#   - Python 3.6+  (stdlib only — no third-party packages needed)
#   - Internet connection
#
# WHAT IT DOES:
#   Agendas:
#     1. Fetches https://www.plumstead.org/ and extracts 8 Google Drive file
#        links from the "Current Agendas" section (one per board/committee).
#     2. HEAD-checks each Drive file via the public download URL to read the
#        Last-Modified header (the timestamp of the last upload to Drive).
#     3. Downloads agendas whose Last-Modified falls within the --days window.
#
#   Minutes:
#     4. Fetches /bos-minutes.html and /plan-minutes.html, collects PDF links
#        at minutes/ and planning/ paths.
#     5. HEAD-checks each PDF for Last-Modified (server upload timestamp).
#     6. Downloads PDFs whose Last-Modified falls within the --days window.
#
# SITE STRUCTURE:
#   Base:        https://www.plumstead.org
#   Agendas:     /  (homepage — "Current Agendas" section)
#                  → href="https://drive.google.com/file/d/{ID}/view?..."
#                  → download: https://drive.google.com/uc?export=download&id={ID}
#   BOS minutes: /bos-minutes.html  → href="minutes/{filename}.pdf"
#   Plan minutes:/plan-minutes.html → href="planning/{filename}.pdf"
#
# NOTE: Agendas are single shared Drive files — the township replaces each
#   file in place when a new agenda is ready. Last-Modified reflects when the
#   current version was uploaded; there is no archive of past agenda versions
#   on the site itself.
#
# NOTE: There are no meeting video/audio recordings on this site.
#   The Vimeo embed on the homepage is a 300-year township history video.
#
# NOTE: Minutes filenames encode the meeting date as {day}{mon3}{yr2}.pdf
#   (e.g., 13may26.pdf = May 13, 2026) but the canonical "posted" date used
#   for filtering is the HTTP Last-Modified header, not the filename date.

import argparse
import datetime
import email.utils
import html as htmllib
import os
import re
import sys
import time
import urllib.error
import urllib.request

# --- Configuration ---
BASE_URL      = "https://www.plumstead.org"
HOME_PATH     = "/"
BOS_PATH      = "/bos-minutes.html"
PLAN_PATH     = "/plan-minutes.html"
OUTPUT_DIR    = "beat-archive/plumstead-twp-agendas"
DAYS_BACK     = 3

HEAD_DELAY     = 0.25
DOWNLOAD_DELAY = 0.8

UA = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36"
)

_DRIVE_RE = re.compile(
    r'href="(https://drive\.google\.com/file/d/([A-Za-z0-9_-]+)/view[^"]*)"[^>]*>([^<]+)</a>',
    re.IGNORECASE,
)
_PDF_RE = re.compile(r'href="((minutes|planning)/[^"]+\.pdf)"', re.IGNORECASE)


# --- HTTP helpers ---

def fetch_html(url):
    req = urllib.request.Request(
        url, headers={"User-Agent": UA, "Accept": "text/html,*/*"}
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            charset = r.headers.get_content_charset() or "utf-8"
            return r.read().decode(charset, errors="replace")
    except urllib.error.HTTPError as e:
        print(f"  HTTP {e.code} — {url}", file=sys.stderr)
        return None
    except urllib.error.URLError as e:
        print(f"  ERROR fetching {url}: {e}", file=sys.stderr)
        return None


def head_last_modified(url):
    req = urllib.request.Request(
        url,
        headers={"User-Agent": UA, "Accept": "*/*"},
        method="HEAD",
    )
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            lm = r.headers.get("Last-Modified")
            if lm:
                return email.utils.parsedate_to_datetime(lm).date()
    except Exception:
        pass
    return None


def download_file(url, dest_path, accept="application/pdf,*/*"):
    req = urllib.request.Request(
        url, headers={"User-Agent": UA, "Accept": accept}
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

def extract_agendas(html_text):
    """Return list of {file_id, board_label, view_url, download_url}."""
    items = []
    seen = set()
    for m in _DRIVE_RE.finditer(html_text):
        view_url   = m.group(1)
        file_id    = m.group(2)
        board_label = htmllib.unescape(m.group(3)).strip()
        if file_id in seen:
            continue
        seen.add(file_id)
        items.append({
            "file_id":      file_id,
            "board_label":  board_label,
            "view_url":     view_url,
            "download_url": f"https://drive.google.com/uc?export=download&id={file_id}",
        })
    return items


def extract_pdfs(html_text, base_url):
    """Return list of {url, path_type} from minutes/ or planning/ hrefs."""
    items = []
    seen = set()
    for m in _PDF_RE.finditer(html_text):
        rel_path   = m.group(1)
        path_type  = m.group(2).lower()
        full_url   = f"{base_url}/{rel_path}"
        if full_url in seen:
            continue
        seen.add(full_url)
        items.append({"url": full_url, "path_type": path_type})
    return items


# --- File naming ---

def slugify(text, max_len=50):
    text = text.lower().strip()
    text = re.sub(r"[/\\&]", "-", text)
    text = re.sub(r"[^\w\s-]", "", text)
    text = re.sub(r"[\s_]+", "-", text)
    text = re.sub(r"-{2,}", "-", text)
    return text.strip("-")[:max_len]


def make_dest(doc_type, board_label, date_posted, output_dir, counter=0):
    month_dir = os.path.join(output_dir, date_posted.strftime("%Y-%m"))
    os.makedirs(month_dir, exist_ok=True)
    suffix = f"-{counter}" if counter > 0 else ""
    fname = (
        f"{date_posted.strftime('%Y-%m-%d')}-{slugify(board_label)}"
        f"-{doc_type}{suffix}.pdf"
    )
    return os.path.join(month_dir, fname)


# --- Main ---

def main():
    parser = argparse.ArgumentParser(
        description=(
            "Download Plumstead Township PA meeting agendas and minutes "
            "posted in the past N days."
        )
    )
    parser.add_argument(
        "--days", type=int, default=DAYS_BACK, metavar="N",
        help=f"Look back N days for posted documents (default: {DAYS_BACK})",
    )
    parser.add_argument(
        "--output-dir", default=OUTPUT_DIR, metavar="DIR",
        help=f"Destination directory (default: {OUTPUT_DIR})",
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="List matching documents without downloading",
    )
    args = parser.parse_args()

    today  = datetime.date.today()
    cutoff = today - datetime.timedelta(days=args.days)

    print(f"Posted window : {cutoff} to {today}")
    print(f"Sources       : {BASE_URL}/ + /bos-minutes.html + /plan-minutes.html")
    if not args.dry_run:
        print(f"Output dir    : {args.output_dir}")
    print()

    # ------------------------------------------------------------------ #
    # Phase 1: collect candidates                                          #
    # ------------------------------------------------------------------ #

    # Agendas from homepage
    print(f"Fetching {BASE_URL}/ ...")
    home_html = fetch_html(BASE_URL + HOME_PATH)
    if not home_html:
        print("ERROR: Could not fetch homepage.", file=sys.stderr)
        sys.exit(1)
    agenda_cands = extract_agendas(home_html)
    print(f"  Google Drive agenda links found: {len(agenda_cands)}")

    # BOS minutes
    print(f"Fetching {BASE_URL}{BOS_PATH} ...")
    bos_html = fetch_html(BASE_URL + BOS_PATH) or ""
    bos_cands = extract_pdfs(bos_html, BASE_URL)
    print(f"  BOS minutes PDFs found: {len(bos_cands)}")

    # Planning Commission minutes
    print(f"Fetching {BASE_URL}{PLAN_PATH} ...")
    plan_html = fetch_html(BASE_URL + PLAN_PATH) or ""
    plan_cands = extract_pdfs(plan_html, BASE_URL)
    print(f"  Planning minutes PDFs found: {len(plan_cands)}")
    print()

    # ------------------------------------------------------------------ #
    # Phase 2: HEAD-check Last-Modified                                    #
    # ------------------------------------------------------------------ #

    confirmed = []
    fname_counters: dict = {}

    # Agendas
    print("Checking Last-Modified on agenda files (Google Drive)...")
    for cand in agenda_cands:
        lm = head_last_modified(cand["download_url"])
        time.sleep(HEAD_DELAY)
        if lm is None or lm < cutoff:
            continue
        key = (cand["board_label"], "agenda", lm)
        fname_counters[key] = fname_counters.get(key, 0) + 1
        confirmed.append({
            "doc_type":     "agenda",
            "board_label":  cand["board_label"],
            "download_url": cand["download_url"],
            "last_modified": lm,
            "counter":      fname_counters[key] - 1,
        })

    # BOS minutes
    print("Checking Last-Modified on BOS minutes PDFs...")
    for cand in bos_cands:
        lm = head_last_modified(cand["url"])
        time.sleep(HEAD_DELAY)
        if lm is None or lm < cutoff:
            continue
        key = ("Board of Supervisors", "minutes", lm)
        fname_counters[key] = fname_counters.get(key, 0) + 1
        confirmed.append({
            "doc_type":     "minutes",
            "board_label":  "Board of Supervisors",
            "download_url": cand["url"],
            "last_modified": lm,
            "counter":      fname_counters[key] - 1,
        })

    # Planning minutes
    print("Checking Last-Modified on Planning Commission minutes PDFs...")
    for cand in plan_cands:
        lm = head_last_modified(cand["url"])
        time.sleep(HEAD_DELAY)
        if lm is None or lm < cutoff:
            continue
        key = ("Planning Commission", "minutes", lm)
        fname_counters[key] = fname_counters.get(key, 0) + 1
        confirmed.append({
            "doc_type":     "minutes",
            "board_label":  "Planning Commission",
            "download_url": cand["url"],
            "last_modified": lm,
            "counter":      fname_counters[key] - 1,
        })

    confirmed.sort(key=lambda x: x["last_modified"], reverse=True)
    print(f"\n{len(confirmed)} document(s) posted within {args.days} day(s).")

    if not confirmed:
        print("No items found within the date window.")
        return

    # ------------------------------------------------------------------ #
    # Phase 3: report or download                                          #
    # ------------------------------------------------------------------ #

    if args.dry_run:
        print()
        print(f"{'Board/Committee':<50} {'Posted':<12} Type")
        print("-" * 74)
        for c in confirmed:
            print(
                f"{c['board_label'][:49]:<50} "
                f"{c['last_modified']!s:<12} "
                f"{c['doc_type']}"
            )
        print(f"\n{len(confirmed)} item(s). Re-run without --dry-run to download.")
        return

    os.makedirs(args.output_dir, exist_ok=True)
    log_path  = os.path.join(args.output_dir, "download-log.txt")
    log_lines = []
    downloaded = skipped = failed = 0

    for c in confirmed:
        dest  = make_dest(
            c["doc_type"], c["board_label"],
            c["last_modified"], args.output_dir, c["counter"]
        )
        label = os.path.basename(dest)
        if os.path.exists(dest):
            print(f"  skip (exists)  {label}")
            skipped += 1
            continue
        print(f"  [posted {c['last_modified']}] {c['board_label']} — {c['doc_type']}")
        print(f"  downloading    {label}")
        if download_file(c["download_url"], dest):
            downloaded += 1
            log_lines.append(f"{datetime.datetime.now().isoformat()}  OK       {dest}")
        else:
            failed += 1
            log_lines.append(f"{datetime.datetime.now().isoformat()}  FAILED   {c['download_url']}")
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
#    python3 scripts/download-plumstead-twp-agendas.py --dry-run
#
# 2. Widen the lookback window (e.g., catch anything posted in the past week):
#    python3 scripts/download-plumstead-twp-agendas.py --days 7
#
# BOARDS / COMMITTEES with current agendas (as of 2026-06):
#   Board of Supervisors
#   Planning Commission
#   Parks and Recreation Committee
#   Environmental Advisory Council
#   Veteran's Advisory AD HOC Committee
#   Land Preservation Education & Advisory Committee
#   Emergency Management Committee
#   Historic Advisory Committee
#
# MINUTES ARCHIVES:
#   Board of Supervisors:  /bos-minutes.html  → minutes/*.pdf
#   Planning Commission:   /plan-minutes.html → planning/*.pdf
