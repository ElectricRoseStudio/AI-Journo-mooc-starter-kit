#!/usr/bin/env python3
# download-redding-agendas.py
# Download Redding CT municipal agendas, minutes, and meeting recording links
# posted in the past N days.
#
# USAGE:
#   python3 scripts/download-redding-agendas.py [options]
#
# REQUIREMENTS:
#   - Python 3.8+
#   - pip install beautifulsoup4
#
# WHAT IT DOES:
#   1. Fetches the Redding CT Agenda Center listing page
#   2. Parses the table of meetings (date, agenda link, minutes link)
#   3. Filters rows whose meeting date falls within the lookback window
#   4. Visits each agenda/minutes sub-page to find the PDF download link
#   5. Downloads PDFs to beat-archive/redding-agendas/YYYY-MM/
#   6. Scrapes https://reddingct.gov/meeting-videos/ for Zoom recording links
#   7. Saves recording shortcuts (.url files) to beat-archive/redding-agendas/recordings/
#   8. Appends a download log to beat-archive/redding-agendas/download-log.txt
#
# SITE STRUCTURE (WordPress):
#   Hub:        https://reddingct.gov/agendas-minutes/
#   Agenda:     https://reddingct.gov/agenda/[slug]/  → PDF at /wp-content/uploads/...
#   Minutes:    https://reddingct.gov/minute/[slug]/  → PDF at /wp-content/uploads/...
#   Recordings: https://reddingct.gov/meeting-videos/ → Zoom rec/share/... links

import argparse
import datetime
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

try:
    from bs4 import BeautifulSoup
except ImportError:
    print("ERROR: beautifulsoup4 is not installed.\n  pip install beautifulsoup4",
          file=sys.stderr)
    sys.exit(1)

BASE_URL = "https://reddingct.gov"
HUB_URL = f"{BASE_URL}/agendas-minutes/"
RECORDINGS_URL = f"{BASE_URL}/meeting-videos/"
OUTPUT_DIR = "beat-archive/redding-agendas"
DAYS_BACK = 4
DELAY_SECONDS = 1.0

UA = "Redding-CT-Agendas-Downloader/1.0 (journalism research)"

DATE_PATTERNS = [
    # MM/DD/YYYY or M/D/YYYY
    (re.compile(r"\b(\d{1,2})/(\d{1,2})/(20\d{2})\b"), "MDY"),
    # Month DD, YYYY
    (re.compile(
        r"\b(January|February|March|April|May|June|July|August|September|"
        r"October|November|December)\s+(\d{1,2}),?\s+(20\d{2})\b", re.I),
     "%B %d %Y"),
    # YYYY-MM-DD
    (re.compile(r"\b(20\d{2})-(\d{2})-(\d{2})\b"), "YMD"),
]

_ZOOM_REC_RE = re.compile(r"zoom\.us/rec/(share|play)/", re.I)
_STATIC_LINK_RE = re.compile(
    r"(youtube\.com|youtu\.be|vimeo\.com|redding79\.org)", re.I)


def fetch_html(url):
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            raw = r.read()
            charset = r.headers.get_content_charset() or "utf-8"
            return raw.decode(charset, errors="replace")
    except urllib.error.HTTPError as e:
        print(f"  HTTP {e.code} — {url}", file=sys.stderr)
        return None
    except urllib.error.URLError as e:
        print(f"  ERROR fetching {url}: {e}", file=sys.stderr)
        return None


def download_binary(url, dest_path):
    full_url = url if url.startswith("http") else BASE_URL + url
    req = urllib.request.Request(full_url, headers={"User-Agent": UA})
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            with open(dest_path, "wb") as f:
                f.write(r.read())
        return True
    except Exception as e:
        print(f"  WARNING: {e} — {full_url}", file=sys.stderr)
        return False


def parse_date(text):
    """Return the first date found in text as a date object, or None."""
    for pattern, fmt in DATE_PATTERNS:
        m = pattern.search(text)
        if not m:
            continue
        try:
            if fmt == "MDY":
                return datetime.date(int(m.group(3)), int(m.group(1)), int(m.group(2)))
            elif fmt == "%B %d %Y":
                return datetime.datetime.strptime(
                    f"{m.group(1)} {int(m.group(2)):02d} {m.group(3)}", "%B %d %Y"
                ).date()
            elif fmt == "YMD":
                return datetime.date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
        except ValueError:
            continue
    return None


def slugify(text):
    text = text.lower().strip()
    text = re.sub(r"[/\\]", "-", text)
    text = re.sub(r"[^\w\s-]", "", text)
    text = re.sub(r"[\s_]+", "-", text)
    text = re.sub(r"-{2,}", "-", text)
    return text.strip("-")[:60]


def unwrap_safelinks(url):
    """Extract the real URL from a Microsoft SafeLinks-wrapped URL."""
    parsed = urllib.parse.urlparse(url)
    qs = urllib.parse.parse_qs(parsed.query)
    real = qs.get("url", [None])[0]
    if real:
        return urllib.parse.unquote(real)
    return url


def save_url_shortcut(url, path):
    """Write a Windows/Linux-compatible .url shortcut file."""
    with open(path, "w") as f:
        f.write(f"[InternetShortcut]\nURL={url}\n")


def find_pdf_on_page(html, page_url):
    """Return the first PDF URL found on an agenda or minutes sub-page."""
    if not html:
        return None
    soup = BeautifulSoup(html, "html.parser")
    for a in soup.find_all("a", href=True):
        href = a["href"]
        if re.search(r"\.pdf(\?|$)", href, re.I):
            if href.startswith("http"):
                return href
            return BASE_URL + href if href.startswith("/") else BASE_URL + "/" + href
    # Fall back: wp-content/uploads link even without .pdf extension
    for a in soup.find_all("a", href=True):
        href = a["href"]
        if "wp-content/uploads" in href:
            if href.startswith("http"):
                return href
            return BASE_URL + href if href.startswith("/") else BASE_URL + "/" + href
    return None


def parse_hub_table(html):
    """
    Parse the hub page table and return a list of dicts:
      {date, board, agenda_url, minutes_url}
    date is a datetime.date or None; URLs may be None if not present.
    """
    soup = BeautifulSoup(html, "html.parser")
    rows = []

    table = soup.find("table")
    if not table:
        print("  WARNING: no <table> found; attempting fallback row detection.",
              file=sys.stderr)
        return _fallback_parse(soup)

    for tr in table.find_all("tr"):
        cells = tr.find_all(["td", "th"])
        if len(cells) < 2:
            continue

        date_text = cells[0].get_text(" ", strip=True)
        meeting_date = parse_date(date_text)

        agenda_url = None
        agenda_board = None
        if len(cells) > 1:
            a = cells[1].find("a", href=True)
            if a:
                href = a["href"]
                agenda_url = href if href.startswith("http") else BASE_URL + href
                agenda_board = a.get_text(" ", strip=True)

        minutes_url = None
        if len(cells) > 2:
            a = cells[2].find("a", href=True)
            if a:
                href = a["href"]
                minutes_url = href if href.startswith("http") else BASE_URL + href

        board = agenda_board or _board_from_url(agenda_url or minutes_url or "")

        if agenda_url or minutes_url:
            rows.append({
                "date": meeting_date,
                "board": board,
                "agenda_url": agenda_url,
                "minutes_url": minutes_url,
            })

    return rows


def _board_from_url(url):
    """Guess board name from a /agenda/slug/ or /minute/slug/ URL."""
    slug = url.rstrip("/").split("/")[-1]
    slug = re.sub(r"-\d+$", "", slug)
    return slug.replace("-", " ").title()


def _fallback_parse(soup):
    """
    Fallback: look for /agenda/ and /minute/ links anywhere on the page
    and try to associate dates from surrounding text.
    """
    rows = []
    seen = set()
    for a in soup.find_all("a", href=True):
        href = a["href"]
        if not re.search(r"/(agenda|minute)/", href, re.I):
            continue
        full = href if href.startswith("http") else BASE_URL + href
        if full in seen:
            continue
        seen.add(full)

        link_text = a.get_text(" ", strip=True)
        parent_text = ""
        for tag in ("tr", "li", "div", "p"):
            parent = a.find_parent(tag)
            if parent:
                parent_text = parent.get_text(" ", strip=True)
                break

        meeting_date = parse_date(link_text) or parse_date(parent_text)
        board = _board_from_url(full)
        is_agenda = "/agenda/" in href.lower()

        rows.append({
            "date": meeting_date,
            "board": board,
            "agenda_url": full if is_agenda else None,
            "minutes_url": full if not is_agenda else None,
        })

    return rows


def fetch_recordings(cutoff, output_dir, board_filter=None, dry_run=False):
    """
    Scrape the meeting-videos page and save Zoom recording shortcuts (.url files)
    for meetings within the date window.  Static channel links (YouTube, Vimeo,
    Redding79) are saved unconditionally since they have no per-meeting dates.
    Returns (saved, skipped) counts.
    """
    print(f"\nFetching recordings page: {RECORDINGS_URL}")
    html = fetch_html(RECORDINGS_URL)
    if not html:
        print("  WARNING: Could not fetch recordings page.", file=sys.stderr)
        return 0, 0

    soup = BeautifulSoup(html, "html.parser")
    rec_dir = os.path.join(output_dir, "recordings")

    rec_ok = rec_skip = 0
    current_section = "Unknown"
    seen_files = set()

    for tag in soup.descendants:
        if not hasattr(tag, "name") or not tag.name:
            continue

        if tag.name in ("h2", "h3", "h4", "h5"):
            heading = tag.get_text(" ", strip=True)
            # Strip trailing " Recordings" / " Videos" from section headings
            current_section = re.sub(
                r"\s+(Recordings|Videos)$", "", heading, flags=re.I).strip()
            continue

        if tag.name != "a":
            continue

        href = tag.get("href", "").strip()
        if not href:
            continue

        # Unwrap Microsoft SafeLinks
        if "safelinks" in href.lower():
            href = unwrap_safelinks(href)

        link_text = tag.get_text(" ", strip=True)

        # Static channel shortcuts (YouTube, Vimeo, Redding79) — save once each
        if _STATIC_LINK_RE.search(href):
            fname = slugify(link_text or current_section) + ".url"
            dest = os.path.join(rec_dir, fname)
            if fname not in seen_files:
                seen_files.add(fname)
                if os.path.exists(dest):
                    rec_skip += 1
                elif dry_run:
                    print(f"  [dry] static     {fname}")
                    rec_ok += 1
                else:
                    os.makedirs(rec_dir, exist_ok=True)
                    save_url_shortcut(href, dest)
                    print(f"  static link      {fname}")
                    rec_ok += 1
            continue

        # Zoom recordings only (skip password-protected component-page URLs)
        if not _ZOOM_REC_RE.search(href):
            continue

        # Try to extract a date from link text, then from the parent element's text
        parent = tag.find_parent(["li", "p", "div", "td"])
        parent_text = parent.get_text(" ", strip=True) if parent else ""
        rec_date = parse_date(link_text) or parse_date(parent_text)

        if rec_date is None or rec_date < cutoff:
            continue

        # Board name filter
        if board_filter and board_filter.lower() not in current_section.lower():
            continue

        # Build filename; handle same-board/same-date duplicates with a counter
        date_str = rec_date.strftime("%Y-%m-%d")
        board_slug = slugify(current_section)[:40]
        fname = f"{date_str}-{board_slug}-recording.url"
        counter = 2
        while fname in seen_files:
            fname = f"{date_str}-{board_slug}-recording-{counter}.url"
            counter += 1
        seen_files.add(fname)

        dest = os.path.join(rec_dir, fname)
        if os.path.exists(dest):
            print(f"  skip (exists)    recordings/{fname}")
            rec_skip += 1
        elif dry_run:
            print(f"  [dry] recording  recordings/{fname}")
            rec_ok += 1
        else:
            os.makedirs(rec_dir, exist_ok=True)
            save_url_shortcut(href, dest)
            print(f"  recording        recordings/{fname}")
            rec_ok += 1

    return rec_ok, rec_skip


def main():
    parser = argparse.ArgumentParser(
        description="Download Redding CT municipal agendas, minutes, and recording "
                    "links posted in the past N days."
    )
    parser.add_argument("--days", type=int, default=DAYS_BACK, metavar="N",
                        help=f"Look back N days (default: {DAYS_BACK})")
    parser.add_argument("--output-dir", default=OUTPUT_DIR, metavar="DIR",
                        help=f"Destination directory (default: {OUTPUT_DIR})")
    parser.add_argument("--dry-run", action="store_true",
                        help="List matching items without downloading")
    parser.add_argument("--include-undated", action="store_true",
                        help="Also process rows where no meeting date could be parsed")
    parser.add_argument("--board", metavar="NAME",
                        help="Only process boards whose name contains NAME (case-insensitive)")
    parser.add_argument("--no-agendas", action="store_true",
                        help="Skip agenda PDFs")
    parser.add_argument("--no-minutes", action="store_true",
                        help="Skip minutes PDFs")
    parser.add_argument("--no-video", action="store_true",
                        help="Skip recording shortcuts from the meeting-videos page")
    args = parser.parse_args()

    cutoff = datetime.date.today() - datetime.timedelta(days=args.days)

    print(f"Cutoff date : {cutoff}  ({args.days} days back)")
    print(f"Hub page    : {HUB_URL}")
    print(f"Output dir  : {args.output_dir}")
    print()

    # --- PDF downloads ---
    dl_ok = dl_skip = dl_fail = 0
    log_lines = []

    if not (args.no_agendas and args.no_minutes):
        print("Fetching hub page...")
        hub_html = fetch_html(HUB_URL)
        if not hub_html:
            print("ERROR: Could not fetch the hub page.", file=sys.stderr)
            sys.exit(1)

        all_rows = parse_hub_table(hub_html)
        if not all_rows:
            print("WARNING: No meeting rows found — the page structure may have changed.",
                  file=sys.stderr)
            sys.exit(1)

        print(f"Found {len(all_rows)} meeting row(s) on hub page.")

        if args.board:
            filter_name = args.board.lower()
            all_rows = [r for r in all_rows if filter_name in r["board"].lower()]
            print(f"Filtered to {len(all_rows)} row(s) matching '{args.board}'.")

        in_window = []
        no_date_count = 0
        for row in all_rows:
            if row["date"] is None:
                no_date_count += 1
                if args.include_undated:
                    in_window.append(row)
            elif row["date"] >= cutoff:
                in_window.append(row)

        undated_note = (
            f"  (+{no_date_count} undated included via --include-undated)"
            if args.include_undated and no_date_count
            else f"  ({no_date_count} undated skipped; use --include-undated to add)"
            if no_date_count else ""
        )
        print(f"Rows in date window : {len(in_window)}{undated_note}")
        print()

        if not in_window:
            print("No meetings found within the date window.")
        elif args.dry_run:
            print(f"{'Board':<40} {'Date':<12} {'Agenda':<6} {'Minutes':<6}")
            print("-" * 70)
            for row in in_window:
                date_s = str(row["date"]) if row["date"] else "unknown"
                has_a = "yes" if (row["agenda_url"] and not args.no_agendas) else "no"
                has_m = "yes" if (row["minutes_url"] and not args.no_minutes) else "no"
                print(f"{row['board'][:39]:<40} {date_s:<12} {has_a:<6} {has_m:<6}")
            print(f"\n{len(in_window)} meeting(s). Re-run without --dry-run to download.")
        else:
            os.makedirs(args.output_dir, exist_ok=True)
            log_path = os.path.join(args.output_dir, "download-log.txt")

            for row in in_window:
                date_s = str(row["date"]) if row["date"] else "unknown"
                board = row["board"]
                print(f"[{date_s}] {board}")

                doc_types = []
                if not args.no_agendas:
                    doc_types.append(("agenda", row["agenda_url"]))
                if not args.no_minutes:
                    doc_types.append(("minutes", row["minutes_url"]))

                for doc_type, page_url in doc_types:
                    if not page_url:
                        continue

                    if row["date"]:
                        month_dir = os.path.join(
                            args.output_dir, row["date"].strftime("%Y-%m"))
                    else:
                        month_dir = os.path.join(args.output_dir, "unknown")
                    os.makedirs(month_dir, exist_ok=True)

                    date_str = (row["date"].strftime("%Y-%m-%d")
                                if row["date"] else "unknown")
                    board_slug = slugify(board)
                    dest = os.path.join(
                        month_dir, f"{date_str}-{board_slug}-{doc_type}.pdf")

                    if os.path.exists(dest):
                        print(f"  skip (exists)  {os.path.basename(dest)}")
                        dl_skip += 1
                        continue

                    sub_html = fetch_html(page_url)
                    time.sleep(0.5)
                    pdf_url = find_pdf_on_page(sub_html, page_url)

                    if not pdf_url:
                        print(f"  WARNING: no PDF found on {page_url}",
                              file=sys.stderr)
                        dl_fail += 1
                        log_lines.append(
                            f"{datetime.datetime.now().isoformat()}  NO-PDF  {page_url}")
                        continue

                    print(f"  downloading    {os.path.basename(dest)}")
                    if download_binary(pdf_url, dest):
                        dl_ok += 1
                        log_lines.append(
                            f"{datetime.datetime.now().isoformat()}  OK      {dest}")
                    else:
                        dl_fail += 1
                        log_lines.append(
                            f"{datetime.datetime.now().isoformat()}  FAIL    {pdf_url}")
                        if os.path.exists(dest):
                            os.remove(dest)

                    time.sleep(DELAY_SECONDS)

            if log_lines:
                with open(log_path, "a") as f:
                    f.write("\n".join(log_lines) + "\n")

    # --- Recording shortcuts ---
    rec_ok = rec_skip = 0
    if not args.no_video:
        rec_ok, rec_skip = fetch_recordings(
            cutoff=cutoff,
            output_dir=args.output_dir,
            board_filter=args.board,
            dry_run=args.dry_run,
        )

    # --- Summary ---
    print()
    if not (args.no_agendas and args.no_minutes):
        print(f"PDFs     — Downloaded: {dl_ok}  Skipped: {dl_skip}  Failed: {dl_fail}")
    if not args.no_video:
        print(f"Recordings — Saved: {rec_ok}  Skipped: {rec_skip}")
    if (dl_ok + dl_skip + rec_ok) and not args.dry_run:
        print(f"Files in: {args.output_dir}")
    if log_lines and not args.dry_run:
        print(f"Log:      {os.path.join(args.output_dir, 'download-log.txt')}")


if __name__ == "__main__":
    main()


# --- Tips ---
#
# 1. Preview without downloading:
#    python3 scripts/download-redding-agendas.py --dry-run
#
# 2. Narrow to one board:
#    python3 scripts/download-redding-agendas.py --board "Planning"
#
# 3. Change the lookback window:
#    python3 scripts/download-redding-agendas.py --days 7
#
# 4. Save files somewhere else:
#    python3 scripts/download-redding-agendas.py --output-dir ~/Downloads/redding
#
# 5. Recordings only (skip PDFs):
#    python3 scripts/download-redding-agendas.py --no-agendas --no-minutes
#
# 6. PDFs only (skip recordings):
#    python3 scripts/download-redding-agendas.py --no-video
#
# 7. Include rows where no date could be parsed:
#    python3 scripts/download-redding-agendas.py --include-undated
#
# 8. Run on a schedule (cron — 7 AM daily):
#    0 7 * * * cd /path/to/repo && python3 scripts/download-redding-agendas.py
