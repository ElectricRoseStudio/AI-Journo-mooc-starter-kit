#!/usr/bin/env python3
# download-brookfield-agendas.py
# Download municipal meeting agendas and minutes from Brookfield CT for meetings
# held in the past N days.
#
# USAGE:
#   python3 scripts/download-brookfield-agendas.py [options]
#
# REQUIREMENTS:
#   - Python 3.6+  (no third-party packages needed)
#   - Internet connection
#
# WHAT IT DOES:
#   1. Queries the CHAMPDS public API (no auth required) to discover all boards
#      and find meetings within the lookback window
#   2. For each meeting, fetches the complete event detail: agenda items and minutes
#   3. Saves each meeting as both a structured JSON file and a readable HTML file
#      under beat-archive/brookfield-agendas/YYYY-MM/
#   4. Appends a download log to beat-archive/brookfield-agendas/download-log.txt
#
# DOCUMENT FORMAT:
#   Brookfield CT uses CHAMPDS (champds.com) as its meeting management system.
#   Agendas and minutes are stored as structured text items, not as PDF attachments.
#   The script saves each meeting as:
#     - .json   Full raw API response (agenda items + minutes with HTML descriptions)
#     - .html   Human-readable formatted view (agenda first, minutes below)
#
# ABOUT THE OFFICIAL ARCHIVE:
#   The Town Clerk's official PDF archive lives at https://ecode360.com/BR0697/documents/Agendas
#   That site is protected by Cloudflare and requires manual browser access.
#   CHAMPDS covers the 9 active boards and is the source embedded in the town's own
#   Board Meetings page at https://www.brookfieldct.gov/168/Board-Meetings.
#
# BOARDS COVERED BY CHAMPDS:
#   Aquifer Protection Agency, Blight Prevention Panel, Board of Selectmen,
#   Conservation Commission, Economic Development Commission, Inland Wetlands Commission,
#   Planning and Zoning Commission, Water Pollution Control Authority,
#   Zoning Board of Appeals

import argparse
import datetime
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request

# --- Configuration ---
CHAMPDS_BASE = "https://playapi.champds.com/brookfieldct"
ARCHIVE_ID = 2          # Archive 2 = current board meetings
OUTPUT_DIR = "beat-archive/brookfield-agendas"
DAYS_BACK = 4
DELAY_SECONDS = 0.5     # polite pause between API calls

UA = "Brookfield-Agendas-Downloader/1.0 (journalism research)"


# --- API helpers ---

def fetch_json(url):
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return json.loads(r.read())
    except urllib.error.URLError as e:
        print(f"  ERROR fetching {url}: {e}", file=sys.stderr)
        return None


def get_boards():
    """Return list of {group_id, group_name} from the CHAMPDS archive."""
    data = fetch_json(f"{CHAMPDS_BASE}/archive/{ARCHIVE_ID}")
    if not data:
        return []
    return [
        {"group_id": g["CustomerArchiveGroupID"], "name": g["GroupName"]}
        for g in data.get("ArchiveGroups", [])
    ]


def get_events_for_group(group_id, start_date, end_date):
    """Return list of event dicts for a board within a date range."""
    start = start_date.strftime("%Y-%m-%dT00:00:00")
    end = end_date.strftime("%Y-%m-%dT23:59:59")
    url = f"{CHAMPDS_BASE}/archiveGroupDate/{group_id}/LOCAL/{start}/{end}"
    data = fetch_json(url)
    return data or []


def get_event_detail(event_id):
    """Return full event detail including agenda items and minutes."""
    data = fetch_json(f"{CHAMPDS_BASE}/event/{event_id}")
    return data


# --- Formatting ---

def _html_to_text(html_str):
    """Strip HTML tags for plain-text rendering in the summary."""
    if not html_str:
        return ""
    text = re.sub(r"<br\s*/?>", "\n", html_str, flags=re.I)
    text = re.sub(r"<p[^>]*>", "\n", text, flags=re.I)
    text = re.sub(r"</p>", "", text, flags=re.I)
    text = re.sub(r"<[^>]+>", "", text)
    text = re.sub(r"&amp;", "&", text)
    text = re.sub(r"&nbsp;", " ", text)
    text = re.sub(r"&lt;", "<", text)
    text = re.sub(r"&gt;", ">", text)
    text = re.sub(r"&#39;", "'", text)
    return text.strip()


def build_html(event_detail, board_name):
    """Render a readable HTML document from a CHAMPDS event detail response."""
    ev = event_detail.get("Event", {})
    agenda = event_detail.get("Agenda", {})
    minutes = event_detail.get("Minutes", {})

    title = ev.get("EventTitle", "Meeting")
    date_local = ev.get("EventDateTimeCustomerLocal", ev.get("EventDateTimeUTC", ""))[:10]
    description = ev.get("EventDescription", "")

    def render_items(items, level=0):
        if not items:
            return ""
        indent = "&nbsp;" * (level * 4)
        html = "<ul>\n"
        for item in items:
            item_title = item.get("Title", "")
            item_desc = item.get("Description", "")
            children = item.get("Children", [])
            html += f"<li>{indent}<strong>{item_title}</strong>"
            if item_desc:
                html += f"<br><span class='desc'>{item_desc}</span>"
            if children:
                html += render_items(children, level + 1)
            html += "</li>\n"
        html += "</ul>\n"
        return html

    # Agenda section
    agenda_html = ""
    agenda_items = agenda.get("AgendaItems", [])
    if agenda_items:
        agenda_html = render_items(agenda_items)
    else:
        agenda_html = "<p><em>No agenda items recorded.</em></p>"

    # Minutes section
    minutes_html = ""
    minutes_items = minutes.get("Items", [])
    if minutes_items:
        minutes_html = "<ul>\n"
        for item in minutes_items:
            item_title = item.get("Title", "")
            item_desc = item.get("Description", "")
            children = item.get("Children", [])
            minutes_html += f"<li><strong>{item_title}</strong>"
            if item_desc:
                minutes_html += f"<br><div class='desc'>{item_desc}</div>"
            minutes_html += "</li>\n"
        minutes_html += "</ul>\n"
    else:
        minutes_html = "<p><em>No minutes recorded yet.</em></p>"

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<title>{board_name} — {title} — {date_local}</title>
<style>
  body {{ font-family: Georgia, serif; max-width: 860px; margin: 2em auto; padding: 0 1em; line-height: 1.5; color: #222; }}
  h1 {{ font-size: 1.4em; border-bottom: 2px solid #333; padding-bottom: .3em; }}
  h2 {{ font-size: 1.1em; margin-top: 1.5em; color: #444; }}
  ul {{ padding-left: 1.5em; }}
  li {{ margin: .4em 0; }}
  .desc {{ color: #555; font-size: .95em; margin: .25em 0 .5em 0; }}
  .meta {{ color: #666; font-size: .9em; margin-bottom: 1em; }}
  .section {{ margin-top: 2em; border-top: 1px solid #ccc; padding-top: 1em; }}
  .source {{ font-size: .8em; color: #999; margin-top: 3em; }}
</style>
</head>
<body>
<h1>{board_name}: {title}</h1>
<p class="meta">Meeting date: {date_local}{' &mdash; ' + _html_to_text(description) if description else ''}</p>

<div class="section">
<h2>Agenda</h2>
{agenda_html}
</div>

<div class="section">
<h2>Minutes</h2>
{minutes_html}
</div>

<p class="source">Source: Brookfield CT CHAMPDS meeting management system
&mdash; <a href="https://www.brookfieldct.gov/168/Board-Meetings">Board Meetings page</a>
&mdash; Downloaded {datetime.date.today().isoformat()}</p>
</body>
</html>
"""


# --- Utilities ---

def slugify(text):
    text = text.lower().strip()
    text = re.sub(r"[^\w\s-]", "", text)
    text = re.sub(r"[\s_]+", "-", text)
    return text.strip("-")[:60]


def make_dest_base(board_name, event_title, meeting_date, output_dir):
    date_prefix = meeting_date.strftime("%Y-%m-%d")
    month_dir = meeting_date.strftime("%Y-%m")
    board_slug = slugify(board_name)
    title_slug = slugify(event_title)
    month_path = os.path.join(output_dir, month_dir)
    os.makedirs(month_path, exist_ok=True)
    return os.path.join(month_path, f"{date_prefix}-{board_slug}-{title_slug}")


# --- Main ---

def main():
    parser = argparse.ArgumentParser(
        description="Download Brookfield CT municipal meeting agendas and minutes posted in the past N days."
    )
    parser.add_argument("--days", type=int, default=DAYS_BACK, metavar="N",
                        help=f"Look back N days (default: {DAYS_BACK})")
    parser.add_argument("--output-dir", default=OUTPUT_DIR, metavar="DIR",
                        help=f"Destination directory (default: {OUTPUT_DIR})")
    parser.add_argument("--dry-run", action="store_true",
                        help="List matching meetings without downloading")
    parser.add_argument("--board", metavar="NAME",
                        help="Only process boards whose name contains NAME (case-insensitive)")
    parser.add_argument("--json-only", action="store_true",
                        help="Save only JSON files, skip HTML rendering")
    args = parser.parse_args()

    today = datetime.date.today()
    cutoff = today - datetime.timedelta(days=args.days)

    print(f"Cutoff date : {cutoff}  ({args.days} days back)")
    print(f"Source      : CHAMPDS API ({CHAMPDS_BASE})")
    if not args.dry_run:
        print(f"Output dir  : {args.output_dir}")
    print()

    # --- Step 1: get board list ---
    print("Fetching board list...")
    boards = get_boards()
    if not boards:
        print("ERROR: Could not retrieve board list.", file=sys.stderr)
        sys.exit(1)

    print(f"Found {len(boards)} board(s).\n")

    if args.board:
        filter_name = args.board.lower()
        boards = [b for b in boards if filter_name in b["name"].lower()]
        print(f"Filtered to {len(boards)} board(s) matching '{args.board}'.\n")

    # --- Step 2: find matching events ---
    matches = []  # list of {board_name, event, meeting_date}

    for board in boards:
        events = get_events_for_group(board["group_id"], cutoff, today)
        for ev in events:
            date_str = ev.get("EventDateTimeLocal", ev.get("EventDateTimeUTC", ""))[:10]
            try:
                meeting_date = datetime.date.fromisoformat(date_str)
            except ValueError:
                continue
            if meeting_date < cutoff:
                continue
            matches.append({
                "board_name": board["name"],
                "event_id": ev["CustomerEventID"],
                "event_title": ev.get("EventTitle", "Meeting"),
                "meeting_date": meeting_date,
            })
        time.sleep(DELAY_SECONDS)

    matches.sort(key=lambda x: x["meeting_date"], reverse=True)

    print(f"Found {len(matches)} meeting(s) within the past {args.days} days.")
    print()

    if not matches:
        sys.exit(0)

    if args.dry_run:
        print(f"{'Board':<42} {'Date':<12} Title")
        print("-" * 75)
        for m in matches:
            print(f"{m['board_name'][:41]:<42} {m['meeting_date']!s:<12} {m['event_title']}")
        print(f"\n{len(matches)} meeting(s). Re-run without --dry-run to download.")
        return

    # --- Step 3: fetch full details and save ---
    os.makedirs(args.output_dir, exist_ok=True)
    log_path = os.path.join(args.output_dir, "download-log.txt")
    log_lines = []
    saved = skipped = failed = 0

    for m in matches:
        board = m["board_name"]
        date = m["meeting_date"]
        title = m["event_title"]
        event_id = m["event_id"]

        dest_base = make_dest_base(board, title, date, args.output_dir)
        json_path = dest_base + ".json"
        html_path = dest_base + ".html"

        label = os.path.basename(dest_base)
        print(f"[{date}] {board} — {title}")

        # Skip if both files exist
        if os.path.exists(json_path) and (args.json_only or os.path.exists(html_path)):
            print(f"  skip (exists)  {label}")
            skipped += 1
            continue

        detail = get_event_detail(event_id)
        if not detail:
            print(f"  FAILED         {label}", file=sys.stderr)
            failed += 1
            log_lines.append(f"{datetime.datetime.now().isoformat()}  FAILED   event/{event_id}  {board} {date}")
            continue

        # Save JSON
        with open(json_path, "w", encoding="utf-8") as f:
            json.dump(detail, f, indent=2, ensure_ascii=False)
        print(f"  saved JSON     {os.path.basename(json_path)}")

        # Save HTML
        if not args.json_only:
            html_content = build_html(detail, board)
            with open(html_path, "w", encoding="utf-8") as f:
                f.write(html_content)
            print(f"  saved HTML     {os.path.basename(html_path)}")

        saved += 1
        log_lines.append(f"{datetime.datetime.now().isoformat()}  OK       {dest_base}  {board} {date}")
        time.sleep(DELAY_SECONDS)

    if log_lines:
        with open(log_path, "a") as f:
            f.write("\n".join(log_lines) + "\n")

    print()
    print(f"Done — saved: {saved}  skipped: {skipped}  failed: {failed}")
    if saved + skipped:
        print(f"Files in: {args.output_dir}")
    if log_lines:
        print(f"Log:      {log_path}")
    if saved or skipped:
        print()
        print("NOTE: Files contain meeting agendas and minutes from CHAMPDS (text format).")
        print("      For official PDFs, see https://ecode360.com/BR0697/documents/Agendas")
        print("      (requires a web browser — the eCode360 archive is not machine-accessible).")


if __name__ == "__main__":
    main()


# --- Tips ---
#
# 1. Preview without downloading:
#    python3 scripts/download-brookfield-agendas.py --dry-run
#
# 2. Narrow to one board:
#    python3 scripts/download-brookfield-agendas.py --board "Board of Selectmen"
#
# 3. Extend the lookback window:
#    python3 scripts/download-brookfield-agendas.py --days 60
#
# 4. JSON only (no HTML):
#    python3 scripts/download-brookfield-agendas.py --json-only
#
# 5. Save files somewhere else:
#    python3 scripts/download-brookfield-agendas.py --output-dir ~/Downloads/brookfield
#
# 6. Run on a schedule (cron — 8 AM daily):
#    0 8 * * * cd /path/to/repo && python3 scripts/download-brookfield-agendas.py
#
# 7. Process downloaded HTML files with Claude afterward:
#    python3 scripts/download-brookfield-agendas.py && bash scripts/batch-process.sh beat-archive/brookfield-agendas/
