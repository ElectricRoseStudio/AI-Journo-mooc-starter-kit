#!/usr/bin/env python3
# download-agendas.py
# Download municipal meeting agendas and minutes from Clinton CT Agenda Center
# for documents posted in the past N days.
#
# USAGE:
#   python3 scripts/download-agendas.py [options]
#
# REQUIREMENTS:
#   - Python 3.6+
#   - Internet connection
#
# WHAT IT DOES:
#   1. Fetches the Clinton CT Agenda Center listing page
#   2. Finds all agendas/minutes posted within the lookback window
#   3. Downloads them to beat-archive/agendas/YYYY-MM/
#   4. Appends a download log to beat-archive/agendas/download-log.txt

import argparse
import datetime
import html.parser
import os
import re
import sys
import time
import urllib.error
import urllib.request

# --- Configuration ---
BASE_URL = "https://clintonct.org"
AGENDA_CENTER_URL = "https://clintonct.org/AgendaCenter/"
OUTPUT_DIR = "beat-archive/agendas"
DAYS_BACK = 4
DELAY_SECONDS = 1  # pause between downloads

USER_AGENT = "Clinton-Agendas-Downloader/1.0 (journalism research)"


# --- HTML Parser ---

class AgendaParser(html.parser.HTMLParser):
    """Parse AgendaCenter HTML to extract meeting items with dates and document URLs."""

    def __init__(self):
        super().__init__()
        self.items = []

        self._in_heading = False
        self._heading_text = ""

        self._current_board = "Unknown Board"
        self._current_meeting_date = None
        self._current_posted_date = None
        self._current_title = ""
        self._current_agenda_url = None
        self._current_minutes_url = None

        self._capture_link_text = False
        self._link_text = ""

    def handle_starttag(self, tag, attrs):
        attrs_dict = dict(attrs)

        if tag in ("h1", "h2", "h3", "h4", "h5", "h6"):
            self._flush_item()
            self._in_heading = True
            self._heading_text = ""

        elif tag == "a" and attrs_dict.get("href"):
            href = attrs_dict["href"]
            if "/AgendaCenter/ViewFile/Agenda/" in href:
                base_href = href.split("?")[0]
                if self._current_agenda_url is None:
                    self._current_agenda_url = base_href
                    self._capture_link_text = True
                    self._link_text = ""
            elif "/AgendaCenter/ViewFile/Minutes/" in href:
                base_href = href.split("?")[0]
                if self._current_minutes_url is None:
                    self._current_minutes_url = base_href

    def handle_data(self, data):
        if self._in_heading:
            self._heading_text += data
        if self._capture_link_text:
            self._link_text += data

    def handle_endtag(self, tag):
        if tag in ("h1", "h2", "h3", "h4", "h5", "h6") and self._in_heading:
            self._in_heading = False
            self._process_heading(self._heading_text.strip())
        elif tag == "a":
            if self._capture_link_text and self._link_text.strip():
                self._current_title = self._link_text.strip()
            self._capture_link_text = False

    def _process_heading(self, text):
        posted_match = re.search(r"Posted\s+(\w+\s+\d+,\s+\d{4})", text)

        if posted_match:
            # Meeting-level heading: extract both dates
            try:
                posted_str = " ".join(posted_match.group(1).split())
                self._current_posted_date = datetime.datetime.strptime(
                    posted_str, "%b %d, %Y"
                ).date()
            except ValueError:
                self._current_posted_date = None

            before_posted = text[: text.index("Posted")]
            date_match = re.search(r"(\w+\s+\d+,\s+\d{4})", before_posted)
            if date_match:
                try:
                    date_str = " ".join(date_match.group(1).split())
                    self._current_meeting_date = datetime.datetime.strptime(
                        date_str, "%b %d, %Y"
                    ).date()
                except ValueError:
                    self._current_meeting_date = None
        elif text and not re.match(r"^\w+\s+\d+,\s+\d{4}", text.strip()):
            # Board/section heading — skip headings that look like bare dates
            self._current_board = text

    def _flush_item(self):
        if self._current_posted_date and (
            self._current_agenda_url or self._current_minutes_url
        ):
            self.items.append(
                {
                    "board": self._current_board,
                    "title": self._current_title,
                    "meeting_date": self._current_meeting_date,
                    "posted_date": self._current_posted_date,
                    "agenda_url": self._current_agenda_url,
                    "minutes_url": self._current_minutes_url,
                }
            )
        self._current_posted_date = None
        self._current_meeting_date = None
        self._current_title = ""
        self._current_agenda_url = None
        self._current_minutes_url = None

    def get_items(self):
        self._flush_item()
        return self.items


# --- Helpers ---

def fetch_page(url):
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(req, timeout=30) as response:
            return response.read().decode("utf-8", errors="replace")
    except urllib.error.URLError as e:
        print(f"Error fetching {url}: {e}", file=sys.stderr)
        sys.exit(1)


def download_file(url, dest_path):
    """Download url to dest_path. Returns True on success."""
    full_url = BASE_URL + url if url.startswith("/") else url
    req = urllib.request.Request(full_url, headers={"User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(req, timeout=30) as response:
            if response.status != 200:
                print(f"  WARNING: HTTP {response.status} — {full_url}", file=sys.stderr)
                return False
            with open(dest_path, "wb") as f:
                f.write(response.read())
        return True
    except Exception as e:
        print(f"  WARNING: {e} — {full_url}", file=sys.stderr)
        return False


def slugify(text):
    text = text.lower().strip()
    text = re.sub(r"[^\w\s-]", "", text)
    text = re.sub(r"[\s_]+", "-", text)
    return text.strip("-")[:60]


def make_dest_path(item, doc_type, output_dir):
    if item["meeting_date"]:
        date_prefix = item["meeting_date"].strftime("%Y-%m-%d")
        month_dir = item["meeting_date"].strftime("%Y-%m")
    else:
        date_prefix = "unknown-date"
        month_dir = "unknown"

    board_slug = slugify(item["board"])
    month_path = os.path.join(output_dir, month_dir)
    os.makedirs(month_path, exist_ok=True)
    return os.path.join(month_path, f"{date_prefix}-{board_slug}-{doc_type}.pdf")


# --- Main ---

def main():
    parser = argparse.ArgumentParser(
        description="Download Clinton CT municipal agendas and minutes posted in the past N days."
    )
    parser.add_argument(
        "--days",
        type=int,
        default=DAYS_BACK,
        metavar="N",
        help=f"Look back N days (default: {DAYS_BACK})",
    )
    parser.add_argument(
        "--output-dir",
        default=OUTPUT_DIR,
        metavar="DIR",
        help=f"Destination directory (default: {OUTPUT_DIR})",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="List matching items without downloading",
    )
    args = parser.parse_args()

    cutoff = datetime.date.today() - datetime.timedelta(days=args.days)
    print(f"Cutoff date : {cutoff}  ({args.days} days back)")
    print(f"Fetching    : {AGENDA_CENTER_URL}")
    print()

    page_html = fetch_page(AGENDA_CENTER_URL)

    agenda_parser = AgendaParser()
    agenda_parser.feed(page_html)
    all_items = agenda_parser.get_items()

    if not all_items:
        print("WARNING: No agenda items found — the page structure may have changed.", file=sys.stderr)
        sys.exit(1)

    recent = [i for i in all_items if i["posted_date"] and i["posted_date"] >= cutoff]
    recent.sort(key=lambda x: x["posted_date"], reverse=True)

    print(f"Total items on page : {len(all_items)}")
    print(f"Within past {args.days} days : {len(recent)}")
    print()

    if not recent:
        print("No items found within the date window.")
        sys.exit(0)

    if args.dry_run:
        print(f"{'Board':<42} {'Meeting':<12} {'Posted':<12} Docs")
        print("-" * 78)
        for item in recent:
            docs = []
            if item["agenda_url"]:
                docs.append("agenda")
            if item["minutes_url"]:
                docs.append("minutes")
            meet = item["meeting_date"].strftime("%Y-%m-%d") if item["meeting_date"] else "unknown"
            print(
                f"{item['board'][:41]:<42} {meet:<12} {str(item['posted_date']):<12} {', '.join(docs)}"
            )
        print(f"\n{len(recent)} items matched. Re-run without --dry-run to download.")
        return

    os.makedirs(args.output_dir, exist_ok=True)
    log_path = os.path.join(args.output_dir, "download-log.txt")
    log_lines = []
    downloaded = skipped = failed = 0

    for item in recent:
        board = item["board"]
        meet = item["meeting_date"].strftime("%Y-%m-%d") if item["meeting_date"] else "unknown"
        print(f"[posted {item['posted_date']}] {board} — meeting {meet}")

        for doc_type, url in (("agenda", item["agenda_url"]), ("minutes", item["minutes_url"])):
            if not url:
                continue

            dest = make_dest_path(item, doc_type, args.output_dir)

            if os.path.exists(dest):
                print(f"  skip (exists)  {os.path.basename(dest)}")
                skipped += 1
                continue

            print(f"  downloading    {os.path.basename(dest)}")
            if download_file(url, dest):
                downloaded += 1
                log_lines.append(f"{datetime.datetime.now().isoformat()}  OK       {dest}")
            else:
                failed += 1
                log_lines.append(f"{datetime.datetime.now().isoformat()}  FAILED   {BASE_URL + url}")
                if os.path.exists(dest):
                    os.remove(dest)  # remove partial file

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


# --- Tips for customizing this script ---
#
# 1. Preview before downloading:
#    python3 scripts/download-agendas.py --dry-run
#
# 2. Change the lookback window:
#    python3 scripts/download-agendas.py --days 7
#
# 3. Save files somewhere else:
#    python3 scripts/download-agendas.py --output-dir ~/Downloads/clinton-meetings
#
# 4. Run on a schedule (cron example — 8 AM daily):
#    0 8 * * * cd /path/to/repo && python3 scripts/download-agendas.py
#
# 5. Process downloaded files with Claude afterward:
#    python3 scripts/download-agendas.py && bash scripts/batch-process.sh beat-archive/agendas/
#
# 6. Fetch HTML versions instead of PDFs:
#    In download_file(), change the URL construction to append "?html=true"
#    to any /AgendaCenter/ViewFile/Agenda/ path before downloading.
