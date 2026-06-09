#!/usr/bin/env python3
# download-new-canaan-agendas.py
# Download municipal meeting agendas and minutes from New Canaan CT
# Documents-On-Demand for meetings within the past N days (and up to 7 days
# ahead, to catch agendas posted early for upcoming meetings).
#
# USAGE:
#   python3 scripts/download-new-canaan-agendas.py [options]
#
# REQUIREMENTS:
#   - Python 3.6+  (no third-party packages needed)
#   - Internet connection
#
# WHAT IT DOES:
#   1. Fetches the full folder tree from the Documents-On-Demand API
#   2. Walks Category → Board → Subfolder (Agendas/Minutes/Meeting Videos)
#   3. Parses the meeting date from each document's title
#   4. Downloads documents whose meeting date falls within the date window
#      to beat-archive/new-canaan-agendas/YYYY-MM/
#      PDF documents are downloaded directly. Meeting Video entries are hyperlinks
#      to YouTube — saved as Windows Internet Shortcut (.url) files.
#   5. Appends a download log to beat-archive/new-canaan-agendas/download-log.txt
#
# SITE STRUCTURE:
#   New Canaan CT uses Ameriscan Imaging Services Documents-On-Demand at
#   https://newcanaantownct.documents-on-demand.com/
#   The platform exposes a public REST API — no browser or authentication needed.
#
#   Key endpoints (all under https://newcanaantownct.documents-on-demand.com/):
#     GET /meta/rootfolder
#         Returns the full folder tree as JSON. Tree depth is:
#           New Canaan → Category → Board → Subfolder
#         Categories: Boards & Councils, Commissions, Committees, Task Forces,
#                     Assessor Department
#         Each node has: key (GUID), title, folder (bool), children array.
#         Subfolder types: Agendas, Minutes, Notices, Meeting Videos, etc.
#     GET /meta/docfolder?containerId={subfolder-guid}
#         Returns all documents in a subfolder, organized in year-group nodes.
#         Document nodes have: key (GUID), title (includes meeting date),
#         fileType ("PDF"), fileName (same as title).
#     GET /document/{doc-guid}/{fileName}.{fileType}
#         Downloads the PDF for a given document.
#
#   Meeting dates are embedded in document titles in varying formats, e.g.:
#     "Board of Assessment Appeals Agenda March 14, 2023"
#     "2025 BAA AGENDA-MARCH 11 2025"
#     "Town Council Minutes February 03, 2026"
#   The date parser handles both comma and no-comma formats, case-insensitively.
#
#   The API is public and requires no authentication.

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
API_BASE = "https://newcanaantownct.documents-on-demand.com"
OUTPUT_DIR = "beat-archive/new-canaan-agendas"
DAYS_BACK = 4
DAYS_AHEAD = 7   # capture agendas posted early for upcoming meetings
DELAY_SECONDS = 1

# Subfolder title prefixes to download (lowercased, case-insensitive startswith match)
DEFAULT_TYPES = {"agenda", "minutes", "meeting video", "video"}

UA = "NewCanaan-Agendas-Downloader/1.0 (journalism research)"

MONTHS = {
    "january": 1, "february": 2, "march": 3, "april": 4,
    "may": 5, "june": 6, "july": 7, "august": 8,
    "september": 9, "october": 10, "november": 11, "december": 12,
}

# Handles: "March 14, 2023"  "MARCH 11 2025"  "March 6 2024"
DATE_RE = re.compile(
    r"(january|february|march|april|may|june|july|august"
    r"|september|october|november|december)"
    r"\s+(\d{1,2}),?\s+(\d{4})",
    re.IGNORECASE,
)


# --- HTTP helpers ---

def fetch_json(url):
    """GET url and return parsed JSON, or None on error."""
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return json.loads(r.read().decode("utf-8"))
    except urllib.error.URLError as e:
        print(f"  ERROR fetching {url}: {e}", file=sys.stderr)
        return None


def download_file(doc_key, file_name, file_type, dest_path):
    """Download a Documents-On-Demand file to dest_path. Returns True on success."""
    encoded = urllib.parse.quote(file_name)
    url = f"{API_BASE}/document/{doc_key}/{encoded}.{file_type}"
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
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


# --- Utilities ---

def slugify(text, max_len=60):
    text = text.lower().strip()
    text = re.sub(r"[/\\]", "-", text)
    text = re.sub(r"\s+-\s+", "-", text)
    text = re.sub(r"[^\w\s-]", "", text)
    text = re.sub(r"[\s_]+", "-", text)
    text = re.sub(r"-{2,}", "-", text)
    return text.strip("-")[:max_len]


def parse_date_from_title(title):
    """Extract a meeting date from a document title. Returns a date or None."""
    m = DATE_RE.search(title)
    if not m:
        return None
    try:
        return datetime.date(int(m.group(3)), MONTHS[m.group(1).lower()], int(m.group(2)))
    except ValueError:
        return None


def make_dest_path(category, board_name, subfolder_type, meeting_date, file_name, output_dir, ext=".pdf"):
    date_prefix = meeting_date.strftime("%Y-%m-%d")
    month_dir = meeting_date.strftime("%Y-%m")
    board_slug = slugify(board_name, max_len=35)
    type_slug = slugify(subfolder_type, max_len=10)
    month_path = os.path.join(output_dir, month_dir)
    os.makedirs(month_path, exist_ok=True)
    fname = f"{date_prefix}-{board_slug}-{type_slug}{ext}"
    return os.path.join(month_path, fname)


def walk_docs(nodes, results):
    """Recursively collect document nodes from a docfolder response tree."""
    for node in nodes:
        if node.get("folder") is False and node.get("fileType"):
            results.append(node)
        children = node.get("children") or []
        if children:
            walk_docs(children, results)


# --- Main ---

def main():
    parser = argparse.ArgumentParser(
        description=(
            "Download New Canaan CT municipal agendas and minutes via the "
            "Documents-On-Demand public API."
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
        help="Only process boards whose name contains NAME (case-insensitive)",
    )
    parser.add_argument(
        "--include-notices", action="store_true",
        help="Also download Notices subfolders",
    )
    parser.add_argument(
        "--no-videos", action="store_true",
        help="Skip Meeting Videos and Videos subfolders",
    )
    args = parser.parse_args()

    now = datetime.datetime.now()
    if (now.weekday() == 5 and now.hour >= 18) or (now.weekday() == 6 and now.hour < 12):  # Saturday night, Sunday morning
        print("Skipping — no downloads on Saturday nights or Sunday mornings.")
        sys.exit(0)

    today = datetime.date.today()
    cutoff = today - datetime.timedelta(days=args.days)
    future_limit = today + datetime.timedelta(days=args.ahead)

    doc_types = set(DEFAULT_TYPES)
    if args.include_notices:
        doc_types.add("notice")
    if args.no_videos:
        doc_types.discard("meeting video")
        doc_types.discard("video")

    print(f"Date window : {cutoff} to {future_limit}")
    print(f"Portal      : {API_BASE}")
    if not args.dry_run:
        print(f"Output dir  : {args.output_dir}")
    print()

    # --- Step 1: fetch the folder tree ---
    print("Fetching folder tree from Documents-On-Demand API...")
    tree = fetch_json(f"{API_BASE}/meta/rootfolder")
    if not tree:
        print("ERROR: Could not fetch folder tree.", file=sys.stderr)
        sys.exit(1)

    # Root: [New Canaan node] → children are categories
    root = tree[0]
    categories = root.get("children", [])
    print(f"Found {len(categories)} top-level category/department folder(s).\n")

    # --- Step 2: collect matching documents ---
    matches = []
    board_filter = args.board.lower() if args.board else None

    for category in categories:
        cat_name = category.get("title", "")
        boards = category.get("children", [])

        for board in boards:
            board_name = board.get("title", "Unknown")

            if board_filter and board_filter not in board_name.lower():
                continue

            subfolders = board.get("children", [])

            for subfolder in subfolders:
                sf_title = subfolder.get("title", "")
                sf_lower = sf_title.lower()

                if not any(sf_lower.startswith(t) for t in doc_types):
                    continue

                sf_key = subfolder.get("key")
                if not sf_key:
                    continue

                url = f"{API_BASE}/meta/docfolder?containerId={sf_key}"
                folder_data = fetch_json(url)
                if not folder_data:
                    continue

                docs = []
                walk_docs(folder_data, docs)

                for doc in docs:
                    title = doc.get("title", "")
                    meeting_date = parse_date_from_title(title)
                    if not meeting_date:
                        continue
                    if meeting_date < cutoff or meeting_date > future_limit:
                        continue

                    ft = doc.get("fileType", "PDF")
                    is_hyperlink = ft.lower() == "hyperlink"
                    matches.append({
                        "category": cat_name,
                        "board": board_name,
                        "subfolder": sf_title,
                        "meeting_date": meeting_date,
                        "doc_key": doc.get("key"),
                        "file_name": doc.get("fileName") or title,
                        "file_type": ft,
                        "is_hyperlink": is_hyperlink,
                        "title": title,
                    })

                time.sleep(0.2)

    matches.sort(key=lambda x: (x["meeting_date"], x["board"]), reverse=True)

    print(
        f"Found {len(matches)} document(s) across "
        f"{len({m['board'] for m in matches})} board(s)."
    )
    print()

    if not matches:
        return

    if args.dry_run:
        print(f"{'Board':<38} {'Date':<12} {'Type':<9} {'Format':<10} Title")
        print("-" * 95)
        for m in matches:
            fmt = "hyperlink" if m["is_hyperlink"] else m["file_type"].lower()
            print(
                f"{m['board'][:37]:<38} {m['meeting_date']!s:<12} "
                f"{m['subfolder'][:8]:<9} {fmt:<10} {m['title'][:35]}"
            )
        print(f"\n{len(matches)} document(s). Re-run without --dry-run to download.")
        return

    # --- Step 3: download ---
    os.makedirs(args.output_dir, exist_ok=True)
    log_path = os.path.join(args.output_dir, "download-log.txt")
    log_lines = []
    downloaded = skipped = failed = 0

    for m in matches:
        ext = ".url" if m["is_hyperlink"] else f".{m['file_type'].lower()}"
        dest = make_dest_path(
            m["category"], m["board"], m["subfolder"], m["meeting_date"],
            m["file_name"], args.output_dir, ext=ext,
        )
        label = os.path.basename(dest)

        if os.path.exists(dest):
            print(f"  skip (exists)  {label}")
            skipped += 1
            continue

        print(f"  [{m['meeting_date']}] {m['board']} — {m['subfolder']}")
        print(f"  saving         {label}")

        if m["is_hyperlink"]:
            ok = save_url_shortcut(m["file_name"], dest)
        else:
            ok = download_file(m["doc_key"], m["file_name"], m["file_type"], dest)

        if ok:
            downloaded += 1
            log_lines.append(
                f"{datetime.datetime.now().isoformat()}  OK       {dest}"
            )
        else:
            failed += 1
            log_lines.append(
                f"{datetime.datetime.now().isoformat()}  FAILED   "
                f"{m['doc_key']}/{m['file_name']}.{m['file_type']}"
            )
            if os.path.exists(dest):
                os.remove(dest)

        if not m["is_hyperlink"]:
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
#    python3 scripts/download-new-canaan-agendas.py --dry-run
#
# 2. Narrow to one board:
#    python3 scripts/download-new-canaan-agendas.py --board "Town Council"
#
# 3. Change the lookback window:
#    python3 scripts/download-new-canaan-agendas.py --days 7
#
# 4. Also include legal notices:
#    python3 scripts/download-new-canaan-agendas.py --include-notices
#
# 5. Skip meeting video links (PDFs only):
#    python3 scripts/download-new-canaan-agendas.py --no-videos
#
# 5. Save files somewhere else:
#    python3 scripts/download-new-canaan-agendas.py --output-dir ~/Downloads/new-canaan
#
# 6. Run on a schedule (cron — 8 AM daily):
#    0 8 * * * cd /path/to/repo && python3 scripts/download-new-canaan-agendas.py
#
# 7. Process downloaded files with Claude afterward:
#    python3 scripts/download-new-canaan-agendas.py && bash scripts/batch-process.sh beat-archive/new-canaan-agendas/
#
# NOTE: The --ahead flag (default: 7 days) captures agendas for upcoming meetings
# that have already been published. Run daily to stay current.
#
# NOTE: Meeting dates are parsed from document titles. New Canaan titles use two
# formats: "Board Name DocType Month DD, YYYY" and "YYYY ABBR DOCTYPE-MONTH DD YYYY".
# The date parser handles both, case-insensitively. Documents without a recognizable
# date in the title are skipped.
#
# NOTE: The Documents-On-Demand API is public and requires no authentication.
# No browser or Playwright needed — all endpoints accept plain HTTP GET requests.
#
# NOTE: New Canaan's folder tree is one level deeper than most Documents-On-Demand
# sites: New Canaan → Category (Boards & Councils / Commissions / Committees /
# Task Forces / Assessor Department) → Board → Subfolder (Agendas/Minutes/Notices).
