#!/usr/bin/env python3
"""Download municipal meeting agendas and minutes from Southington, CT.

Southington uses an Ameriscan Documents-On-Demand (DOD) portal:
  https://southingtontownct.documents-on-demand.com/

API endpoints (no auth required):
  GET /meta/rootfolder              → JSON folder tree (boards → Agendas/Minutes/Notices)
  GET /meta/docfolder?containerId=GUID  → JSON doc tree for a folder (year groups → docs)
  GET /document/GUID                → PDF download

Meeting recording links (YouTube) are saved to recording-index.csv when --recordings
is passed. Southington's YouTube channel is:
  https://www.youtube.com/@townofsouthington7646
"""

import argparse
import csv
import datetime
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

DOD_BASE = "https://southingtontownct.documents-on-demand.com"
ROOT_FOLDER_URL = f"{DOD_BASE}/meta/rootfolder"
DOC_FOLDER_URL = f"{DOD_BASE}/meta/docfolder"
DOC_DOWNLOAD_URL = f"{DOD_BASE}/document"
YOUTUBE_CHANNEL = "https://www.youtube.com/@townofsouthington7646"
OUTPUT_DIR = "beat-archive/southington-agendas"
DAYS_BACK = 4
MIN_DATE = datetime.date(2020, 1, 1)

_UA = "Southington-Agendas-Downloader/1.0 (journalism research)"

_HEADERS = {
    "User-Agent": _UA,
    "Accept": "application/json, text/javascript, */*",
    "Accept-Language": "en-US,en;q=0.9",
    "Referer": DOD_BASE,
}

_MONTHS = {
    "January": 1, "February": 2, "March": 3, "April": 4,
    "May": 5, "June": 6, "July": 7, "August": 8,
    "September": 9, "October": 10, "November": 11, "December": 12,
}
_DATE_RE = re.compile(
    r"\b(January|February|March|April|May|June|July|August|September|"
    r"October|November|December)\s+(\d{1,2}),\s+(\d{4})\b"
)


# --- HTTP helpers ---

def fetch_json(url, retries=3):
    req = urllib.request.Request(url, headers=_HEADERS)
    for attempt in range(retries + 1):
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                return json.loads(r.read().decode("utf-8", errors="replace"))
        except (urllib.error.URLError, TimeoutError, OSError) as e:
            if attempt < retries:
                time.sleep(3 * (attempt + 1))
            else:
                print(f"  Error fetching {url}: {e}", file=sys.stderr)
        except json.JSONDecodeError as e:
            print(f"  JSON error for {url}: {e}", file=sys.stderr)
            return None
    return None


def download_file(doc_key, dest_path, retries=3):
    url = f"{DOC_DOWNLOAD_URL}/{doc_key}"
    req = urllib.request.Request(url, headers={"User-Agent": _UA})
    for attempt in range(retries + 1):
        try:
            with urllib.request.urlopen(req, timeout=60) as r:
                data = r.read()
            os.makedirs(os.path.dirname(dest_path), exist_ok=True)
            with open(dest_path, "wb") as f:
                f.write(data)
            return True
        except (urllib.error.URLError, TimeoutError, OSError) as e:
            if attempt < retries:
                time.sleep(3 * (attempt + 1))
            else:
                print(f"  Error downloading {url}: {e}", file=sys.stderr)
    return False


# --- Folder tree parsing ---

def parse_folder_tree(nodes):
    """Walk rootfolder JSON. Returns [(board_name, doc_type, folder_guid)].

    doc_type is one of: 'agendas', 'minutes', 'notices'
    Skips non-board top-level folders (e.g. 'Bid List', '2026 Meeting Schedules').
    """
    results = []
    _skip = {"Bid List", "Board & Commission Yearly Meeting Dates", "2026 Meeting Schedules"}
    for board_node in nodes:
        board_name = board_node.get("title", "").strip()
        if not board_name or board_name in _skip:
            continue
        children = board_node.get("children") or []
        for sub in children:
            sub_title = sub.get("title", "").strip().lower()
            sub_key = sub.get("key", "")
            if not sub_key:
                continue
            if "agenda" in sub_title:
                results.append((board_name, "agenda", sub_key))
            elif "minute" in sub_title:
                results.append((board_name, "minutes", sub_key))
            elif "notice" in sub_title:
                results.append((board_name, "notices", sub_key))
    return results


def collect_docs_from_folder(folder_data):
    """Walk docfolder JSON. Returns [(title, doc_key, file_type)] for all leaf docs."""
    docs = []

    def walk(nodes):
        for node in nodes:
            children = node.get("children")
            if node.get("folder", True) is False and node.get("fileType"):
                docs.append((
                    node.get("title", ""),
                    node.get("key", ""),
                    node.get("fileType", "").upper(),
                ))
            if children:
                walk(children)

    walk(folder_data)
    return docs


# --- Date parsing ---

def parse_date_from_title(title):
    """Extract meeting date from title string. Returns datetime.date or None."""
    matches = _DATE_RE.findall(title)
    if not matches:
        return None
    month_str, day_str, year_str = matches[-1]
    try:
        return datetime.date(int(year_str), _MONTHS[month_str], int(day_str))
    except ValueError:
        return None


# --- Filename helpers ---

def slugify(text):
    text = text.lower().strip()
    text = re.sub(r"[/\\]", "-", text)
    text = re.sub(r"\s+-\s+", "-", text)
    text = re.sub(r"[^\w\s-]", "", text)
    text = re.sub(r"[\s_]+", "-", text)
    text = re.sub(r"-{2,}", "-", text)
    return text.strip("-")[:60]


def build_dest_path(board_name, doc_type, meeting_date, doc_key, output_dir):
    month_dir = os.path.join(output_dir, meeting_date.strftime("%Y-%m"))
    short_key = doc_key.replace("-", "")[:12]
    filename = f"{meeting_date.isoformat()}_{slugify(board_name)}_{doc_type}_{short_key}.pdf"
    return os.path.join(month_dir, filename)


# --- Main ---

def main():
    ap = argparse.ArgumentParser(
        description="Download Southington CT municipal meeting agendas and minutes (DOD portal)"
    )
    ap.add_argument("--days", type=int, default=DAYS_BACK,
                    help=f"Days back to fetch (default: {DAYS_BACK})")
    ap.add_argument("--ahead", type=int, default=90,
                    help="Days ahead for upcoming agendas (default: 90)")
    ap.add_argument("--all", action="store_true",
                    help=f"Fetch all docs back to {MIN_DATE}")
    ap.add_argument("--dry-run", action="store_true",
                    help="List what would be downloaded without downloading")
    ap.add_argument("--board", metavar="NAME",
                    help="Only process boards whose name contains NAME (case-insensitive)")
    ap.add_argument("--no-agendas", action="store_true", help="Skip agenda PDFs")
    ap.add_argument("--no-minutes", action="store_true", help="Skip minutes PDFs")
    ap.add_argument("--notices", action="store_true",
                    help="Also download notices (default: skip)")
    ap.add_argument("--recordings", action="store_true",
                    help="Print YouTube channel URL for recording index")
    ap.add_argument("--verbose", "-v", action="store_true")
    ap.add_argument("--output-dir", default=OUTPUT_DIR,
                    help=f"Output directory (default: {OUTPUT_DIR})")
    args = ap.parse_args()

    if datetime.date.today().weekday() in (6, 0):  # Sunday, Monday
        print("Skipping — no downloads on Sunday or Monday.")
        sys.exit(0)

    today = datetime.date.today()
    cutoff_back = MIN_DATE if args.all else today - datetime.timedelta(days=args.days)
    cutoff_ahead = today + datetime.timedelta(days=args.ahead)

    os.makedirs(args.output_dir, exist_ok=True)

    print("Fetching folder tree...")
    root = fetch_json(ROOT_FOLDER_URL)
    if not root:
        print("ERROR: Could not fetch folder tree.", file=sys.stderr)
        sys.exit(1)

    # rootfolder returns a list; top-level is ["Town of Southington" node]
    top_children = root[0].get("children", []) if root else []
    folder_entries = parse_folder_tree(top_children)
    if not folder_entries:
        print("ERROR: No folders found — API structure may have changed.", file=sys.stderr)
        sys.exit(1)

    # Filter by board name
    if args.board:
        filt = args.board.lower()
        board_names = sorted({name for name, _, _ in folder_entries})
        folder_entries = [(n, t, g) for n, t, g in folder_entries if filt in n.lower()]
        if not folder_entries:
            print(f"No boards match '{args.board}'. Available boards:")
            for name in sorted(board_names):
                print(f"  {name}")
            return
        print(f"Filtered to boards matching '{args.board}'.")

    # Filter by doc type
    skip_types = set()
    if args.no_agendas:
        skip_types.add("agenda")
    if args.no_minutes:
        skip_types.add("minutes")
    if not args.notices:
        skip_types.add("notices")
    folder_entries = [(n, t, g) for n, t, g in folder_entries if t not in skip_types]

    # Group by board for cleaner output
    from collections import defaultdict
    by_board = defaultdict(list)
    for board_name, doc_type, folder_guid in folder_entries:
        by_board[board_name].append((doc_type, folder_guid))

    total_dl = total_skip = total_fail = 0

    for board_name in sorted(by_board):
        if args.verbose:
            print(f"\n{board_name}")
        else:
            print(f"{board_name}...")

        board_dl = board_skip = 0

        for doc_type, folder_guid in sorted(by_board[board_name]):
            url = f"{DOC_FOLDER_URL}?containerId={folder_guid}"
            folder_data = fetch_json(url)
            if not folder_data:
                if args.verbose:
                    print(f"  (no data for {doc_type})")
                continue
            time.sleep(0.2)

            docs = collect_docs_from_folder(folder_data)

            for title, doc_key, file_type in docs:
                if file_type != "PDF":
                    continue
                mtg_date = parse_date_from_title(title)
                if not mtg_date:
                    if args.verbose:
                        print(f"  (no date in: {title!r})")
                    continue
                if not (cutoff_back <= mtg_date <= cutoff_ahead):
                    continue

                out = build_dest_path(board_name, doc_type, mtg_date, doc_key, args.output_dir)
                if os.path.exists(out):
                    total_skip += 1
                    board_skip += 1
                    if args.verbose:
                        print(f"  skip  {os.path.basename(out)}")
                elif args.dry_run:
                    print(f"  [dry] {os.path.basename(out)}")
                    total_dl += 1
                    board_dl += 1
                else:
                    if args.verbose:
                        print(f"  dl    {os.path.basename(out)}")
                    if download_file(doc_key, out):
                        total_dl += 1
                        board_dl += 1
                        time.sleep(0.5)
                    else:
                        total_fail += 1

        if not args.dry_run and not args.verbose:
            print(f"  downloaded: {board_dl}  skipped: {board_skip}")

    if args.recordings:
        print(f"\nSouthington meeting recordings (YouTube):")
        print(f"  {YOUTUBE_CHANNEL}")
        print(f"  Playlists: https://www.youtube.com/channel/UC59RScd50ReAqz-PnbXUSSQ/playlists")

    label = "Would download" if args.dry_run else "Downloaded"
    print(f"\n{label}: {total_dl}  skipped: {total_skip}  failed: {total_fail}")
    if not args.dry_run and total_dl:
        print(f"Files in: {args.output_dir}")


if __name__ == "__main__":
    main()
