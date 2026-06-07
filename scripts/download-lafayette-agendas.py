#!/usr/bin/env python3
# download-lafayette-agendas.py
# Download Lafayette, CA municipal meeting agendas, minutes, and recording
# shortcuts for meetings posted in the past N days.
#
# USAGE:
#   python3 scripts/download-lafayette-agendas.py [options]
#
# REQUIREMENTS:
#   - Python 3.8+ (no third-party packages required)
#
# WHAT IT DOES:
#   1. Fetches the Granicus ViewPublisher listing for each active board
#   2. Parses each meeting row for name, date, agenda/minutes/audio links
#   3. Filters by cutoff date (and optionally by board name)
#   4. Downloads agenda PDFs or HTML packets (auto-detected) to
#      beat-archive/lafayette-agendas/YYYY-MM/
#   5. Downloads minutes PDFs
#   6. Saves recording shortcuts (.url files) → Granicus MediaPlayer page
#   7. Appends a download log to beat-archive/lafayette-agendas/download-log.txt
#
# SITE NOTES:
#   Platform:  Granicus (lafayette.granicus.com)
#   Boards:    21 active boards across view_id 2–25
#              (view_id 5 = training only; view_id 15 = no records; view_id 23 = combined
#               "All Meetings" stub with no archive — skip all three)
#   Agendas:   GET /AgendaViewer.php?view_id=N&clip_id=N  → 302 → S3 PDF or HTML
#              GET /AgendaViewer.php?view_id=N&event_id=N → 302 (upcoming meetings)
#              Content-type determines whether to save as .pdf or .html
#   Minutes:   GET /MinutesViewer.php?view_id=N&clip_id=N&doc_id=UUID
#              → 302 → /DocumentViewer.php → S3 PDF
#   Audio:     MediaPlayer.php?view_id=N&clip_id=N  (saved as .url shortcut)
#              The city labels recordings "Audio" (council meetings are audio-only).
#   S3 note:   Granicus redirects to s3 bucket 'granicus_production_attachments'
#              (underscore in hostname). Python's SSL rejects it. Fix: custom redirect
#              handler converts vhost-style → path-style before SSL handshakes.

import argparse
import datetime
import html
import os
import re
import sys
import time
import urllib.error
import urllib.request

BASE_URL = "https://lafayette.granicus.com"
PUBLISHER_URL = f"{BASE_URL}/ViewPublisher.php?view_id={{view_id}}"
PLAYER_URL = f"{BASE_URL}/MediaPlayer.php?view_id={{view_id}}&clip_id={{clip_id}}"

OUTPUT_DIR = "beat-archive/lafayette-agendas"
DAYS_BACK = 4
DELAY_SECONDS = 1.0

UA = ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36")

# Active boards: view_id → display name.
# Excluded: 5 (training content), 15 (no records), 23 (upcoming-only stub).
VIEWS = {
    2:  "Transportation and Circulation Commission",
    3:  "City Council",
    4:  "Banner Advisory Committee",
    6:  "Bicycle and Pedestrian Advisory Committee",
    7:  "Capital Projects Assessment Committee",
    8:  "Charter City and Communications Committee",
    9:  "Code Enforcement Appeals Board",
    10: "Community Center Foundation",
    11: "Creeks Committee",
    12: "Downtown Street Improvement Master Plan Implementation Committee",
    13: "Environmental Task Force",
    14: "Public Art Committee",
    16: "Design Review Commission",
    17: "Parks, Trails and Recreation Commission",
    18: "Planning",
    19: "Planning Commission",
    20: "Crime Prevention Commission",
    21: "Emergency Preparedness Commission",
    22: "Lafayette Oversight Board",
    24: "Senior Services Commission",
    25: "Youth Commission",
}

_ROW_RE = re.compile(
    r'<tr\s+class="(?:odd|even)"[^>]*>(.*?)</tr>', re.S | re.I
)
_NAME_RE = re.compile(
    r'<td[^>]*headers="(?:Name|EventName)"[^>]*>(.*?)</td>', re.S | re.I
)
_DATE_CELL_RE = re.compile(
    r'<td[^>]*headers="(?:Date|EventDate)[^"]*"[^>]*>(.*?)</td>', re.S | re.I
)
_DATE_RE = re.compile(r'([A-Za-z]{3,9})\s+(\d{1,2}),\s+(\d{4})')
_AGENDA_HREF_RE = re.compile(
    r'href="//lafayette\.granicus\.com/(AgendaViewer\.php\?[^"]+)"', re.I
)
_MINUTES_HREF_RE = re.compile(
    r'href="//lafayette\.granicus\.com/(MinutesViewer\.php\?[^"]+)"', re.I
)
_MEDIA_CLIP_RE = re.compile(
    r'MediaPlayer\.php\?view_id=\d+&(?:amp;)?clip_id=(\d+)', re.I
)
_AMP = re.compile(r'&amp;')


# ---------------------------------------------------------------------------
# S3 underscore-hostname fix (same pattern as Rohnert Park script)
# ---------------------------------------------------------------------------

_S3_VHOST_RE = re.compile(
    r'^https://([a-zA-Z0-9._-]+)\.s3\.amazonaws\.com/(.+)$'
)


def _s3_path_style(url):
    """Convert S3 virtual-hosted URL to path-style (avoids SSL hostname mismatch)."""
    m = _S3_VHOST_RE.match(url)
    if m:
        return f"https://s3.amazonaws.com/{m.group(1)}/{m.group(2)}"
    return url


class _S3RedirectHandler(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        newurl = _s3_path_style(newurl)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def _make_opener():
    return urllib.request.build_opener(_S3RedirectHandler())


# ---------------------------------------------------------------------------
# HTTP helpers
# ---------------------------------------------------------------------------

def fetch_html(url, *, timeout=30):
    req = urllib.request.Request(url, headers={
        "User-Agent": UA,
        "Accept": "text/html,application/xhtml+xml,*/*;q=0.8",
    })
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.read().decode("latin-1")
    except urllib.error.HTTPError as e:
        print(f"  HTTP {e.code} — {url}", file=sys.stderr)
        return None
    except Exception as e:
        print(f"  ERROR fetching {url}: {e}", file=sys.stderr)
        return None


def download_binary(url, dest_path, *, timeout=60):
    """Download url → dest_path (follows redirects, handles S3). True on success."""
    url = _s3_path_style(url)
    opener = _make_opener()
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    try:
        with opener.open(req, timeout=timeout) as r:
            data = r.read()
        if not data:
            return False
        with open(dest_path, "wb") as f:
            f.write(data)
        return True
    except urllib.error.HTTPError as e:
        print(f"  HTTP {e.code} — {url}", file=sys.stderr)
        return False
    except Exception as e:
        print(f"  ERROR: {e}", file=sys.stderr)
        return False


def download_agenda(url, dest_stem, *, timeout=60):
    """
    Download an agenda, auto-detecting PDF vs HTML.
    Returns the saved filename (with extension) or None on failure.
    """
    url = _s3_path_style(BASE_URL + "/" + _AMP.sub("&", url))
    opener = _make_opener()
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    try:
        with opener.open(req, timeout=timeout) as r:
            ct = r.headers.get("Content-Type", "")
            data = r.read()
    except urllib.error.HTTPError as e:
        print(f"  HTTP {e.code} — {url}", file=sys.stderr)
        return None
    except Exception as e:
        print(f"  ERROR: {e}", file=sys.stderr)
        return None

    if not data:
        return None

    is_pdf = "application/pdf" in ct or data[:4] == b"%PDF"
    ext = ".pdf" if is_pdf else ".html"
    dest = dest_stem + ext

    with open(dest, "wb") as f:
        f.write(data)
    return os.path.basename(dest)


def save_url_shortcut(url, path):
    with open(path, "w") as f:
        f.write(f"[InternetShortcut]\nURL={url}\n")


# ---------------------------------------------------------------------------
# Parsing
# ---------------------------------------------------------------------------

def slugify(text):
    text = text.lower().strip()
    text = re.sub(r"[/\\]", "-", text)
    text = re.sub(r"[^\w\s-]", "", text)
    text = re.sub(r"[\s_]+", "-", text)
    text = re.sub(r"-{2,}", "-", text)
    return text.strip("-")[:60]


def parse_date(cell_html):
    text = html.unescape(cell_html).replace("\xa0", " ")
    m = _DATE_RE.search(text)
    if not m:
        return None
    try:
        return datetime.datetime.strptime(
            f"{m.group(1)} {int(m.group(2)):02d} {m.group(3)}", "%b %d %Y"
        ).date()
    except ValueError:
        return None


def fix_url(path):
    """Turn a relative Granicus path into an absolute HTTPS URL."""
    return BASE_URL + "/" + _AMP.sub("&", path)


def parse_meetings(view_id, html_content, cutoff, board_name):
    """
    Parse all TR rows from a ViewPublisher page for one board.
    Returns list of dicts: {name, board, date, agenda_url, minutes_url, video_url}
    """
    meetings = []
    for row in _ROW_RE.finditer(html_content):
        row_html = row.group(1)

        # Name
        nm = _NAME_RE.search(row_html)
        if not nm:
            continue
        name = html.unescape(re.sub(r"<[^>]+>", "", nm.group(1))).strip()

        # Date
        dm = _DATE_CELL_RE.search(row_html)
        if not dm:
            continue
        date = parse_date(dm.group(1))
        if date is None or date < cutoff:
            continue

        # Agenda URL (relative path)
        am = _AGENDA_HREF_RE.search(row_html)
        agenda_url = am.group(1) if am else None

        # Minutes URL (relative path)
        mm = _MINUTES_HREF_RE.search(row_html)
        minutes_url = mm.group(1) if mm else None

        # Video/audio — extract clip_id from onClick MediaPlayer reference
        cid_m = _MEDIA_CLIP_RE.search(row_html)
        video_url = (PLAYER_URL.format(view_id=view_id, clip_id=cid_m.group(1))
                     if cid_m else None)

        meetings.append({
            "name": name,
            "board": board_name,
            "date": date,
            "agenda_url": agenda_url,
            "minutes_url": minutes_url,
            "video_url": video_url,
        })

    return meetings


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        description="Download Lafayette, CA municipal meeting agendas, "
                    "minutes, and recording shortcuts for the past N days."
    )
    parser.add_argument("--days", type=int, default=DAYS_BACK, metavar="N",
                        help=f"Look back N days (default: {DAYS_BACK})")
    parser.add_argument("--output-dir", default=OUTPUT_DIR, metavar="DIR",
                        help=f"Destination directory (default: {OUTPUT_DIR})")
    parser.add_argument("--dry-run", action="store_true",
                        help="List matching items without downloading")
    parser.add_argument("--board", metavar="NAME",
                        help="Only process boards whose name contains NAME "
                             "(case-insensitive; e.g. 'City Council', 'Planning')")
    parser.add_argument("--no-agendas", action="store_true",
                        help="Skip agenda PDFs/HTML")
    parser.add_argument("--no-minutes", action="store_true",
                        help="Skip minutes PDFs")
    parser.add_argument("--no-video", action="store_true",
                        help="Skip saving recording shortcuts")
    args = parser.parse_args()

    cutoff = datetime.date.today() - datetime.timedelta(days=args.days)
    want_agendas = not args.no_agendas
    want_minutes = not args.no_minutes

    # Determine which views to fetch
    if args.board:
        filt = args.board.lower()
        active_views = {vid: name for vid, name in VIEWS.items()
                        if filt in name.lower()}
        if not active_views:
            print(f"No boards match '{args.board}'. Available boards:")
            for vid, name in sorted(VIEWS.items(), key=lambda x: x[1]):
                print(f"  view_id={vid:2d}  {name}")
            sys.exit(1)
    else:
        active_views = VIEWS

    print(f"Cutoff date : {cutoff}  ({args.days} days back)")
    print(f"Source      : {BASE_URL} ({len(active_views)} board(s))")
    print(f"Output dir  : {args.output_dir}")
    if args.board:
        print(f"Board filter: '{args.board}'")
    print()

    all_meetings = []

    for view_id, board_name in sorted(active_views.items()):
        url = PUBLISHER_URL.format(view_id=view_id)
        print(f"Fetching {board_name} (view_id={view_id})...")
        content = fetch_html(url, timeout=60)
        if not content:
            print(f"  WARNING: Could not fetch view_id={view_id}, skipping.",
                  file=sys.stderr)
            continue
        meetings = parse_meetings(view_id, content, cutoff, board_name)
        print(f"  {len(meetings)} meeting(s) in date window")
        all_meetings.extend(meetings)
        time.sleep(0.3)

    all_meetings.sort(key=lambda m: (m["date"], m["board"], m["name"]))

    if not all_meetings:
        print("\nNo meetings found within the date window.")
        return

    if args.dry_run:
        print(f"\n{'Date':<12} {'Board':<45} {'Agnd':<5} {'Mins':<5} {'Aud'}")
        print("-" * 90)
        for m in all_meetings:
            has_a = "yes" if (m["agenda_url"] and want_agendas) else "no"
            has_m = "yes" if (m["minutes_url"] and want_minutes) else "no"
            has_v = "yes" if (m["video_url"] and not args.no_video) else "no"
            board_label = m["board"][:44]
            print(f"{str(m['date']):<12} {board_label:<45} {has_a:<5} {has_m:<5} {has_v}")
        print(f"\n{len(all_meetings)} meeting(s). Re-run without --dry-run to download.")
        return

    os.makedirs(args.output_dir, exist_ok=True)
    log_path = os.path.join(args.output_dir, "download-log.txt")
    log_lines = []
    dl_ok = dl_skip = dl_fail = 0
    rec_ok = rec_skip = 0

    for m in all_meetings:
        date_s = str(m["date"])
        print(f"[{date_s}] {m['board']} — {m['name']}")

        month_dir = os.path.join(args.output_dir, m["date"].strftime("%Y-%m"))
        os.makedirs(month_dir, exist_ok=True)

        date_str = m["date"].strftime("%Y-%m-%d")
        board_slug = slugify(m["board"])

        # Agenda
        if want_agendas and m["agenda_url"]:
            dest_stem = os.path.join(month_dir, f"{date_str}-{board_slug}-agenda")
            exists = os.path.exists(dest_stem + ".pdf") or os.path.exists(dest_stem + ".html")
            if exists:
                print(f"  skip (exists)  {os.path.basename(dest_stem)}.*")
                dl_skip += 1
            else:
                saved = download_agenda(m["agenda_url"], dest_stem)
                if saved:
                    dl_ok += 1
                    dest_full = os.path.join(month_dir, saved)
                    print(f"  downloaded     {saved}")
                    log_lines.append(
                        f"{datetime.datetime.now().isoformat()}  OK      {dest_full}")
                else:
                    dl_fail += 1
                    log_lines.append(
                        f"{datetime.datetime.now().isoformat()}  FAIL    {m['agenda_url']}")
                time.sleep(DELAY_SECONDS)

        # Minutes
        if want_minutes and m["minutes_url"]:
            dest = os.path.join(month_dir, f"{date_str}-{board_slug}-minutes.pdf")
            if os.path.exists(dest):
                print(f"  skip (exists)  {os.path.basename(dest)}")
                dl_skip += 1
            else:
                print(f"  downloading    {os.path.basename(dest)}")
                if download_binary(fix_url(m["minutes_url"]), dest):
                    dl_ok += 1
                    log_lines.append(
                        f"{datetime.datetime.now().isoformat()}  OK      {dest}")
                else:
                    dl_fail += 1
                    log_lines.append(
                        f"{datetime.datetime.now().isoformat()}  FAIL    {m['minutes_url']}")
                    if os.path.exists(dest):
                        os.remove(dest)
                time.sleep(DELAY_SECONDS)

        # Recording shortcut
        if not args.no_video and m["video_url"]:
            rec_dir = os.path.join(args.output_dir, "recordings")
            os.makedirs(rec_dir, exist_ok=True)
            rec_fname = f"{date_str}-{board_slug}-recording.url"
            rec_path = os.path.join(rec_dir, rec_fname)
            if os.path.exists(rec_path):
                rec_skip += 1
            else:
                save_url_shortcut(m["video_url"], rec_path)
                print(f"  saved          recordings/{rec_fname}")
                rec_ok += 1
                log_lines.append(
                    f"{datetime.datetime.now().isoformat()}  URL     {rec_path}")

    if log_lines:
        with open(log_path, "a") as f:
            f.write("\n".join(log_lines) + "\n")

    print()
    print(f"Documents:  Downloaded {dl_ok}  Skipped {dl_skip}  Failed {dl_fail}")
    print(f"Recordings: Saved {rec_ok}  Skipped {rec_skip}")
    if dl_ok + dl_skip + rec_ok:
        print(f"Files in:   {args.output_dir}")
    if log_lines:
        print(f"Log:        {log_path}")


if __name__ == "__main__":
    main()


# --- Tips ---
#
# 1. Preview without downloading:
#    python3 scripts/download-lafayette-agendas.py --dry-run
#
# 2. Narrow to one board:
#    python3 scripts/download-lafayette-agendas.py --board "City Council"
#    python3 scripts/download-lafayette-agendas.py --board "Planning Commission"
#    python3 scripts/download-lafayette-agendas.py --board "Design Review"
#
# 3. List all available boards:
#    python3 scripts/download-lafayette-agendas.py --board "XXXXXX"
#    (No match triggers the board list)
#
# 4. Change the lookback window:
#    python3 scripts/download-lafayette-agendas.py --days 7
#
# 5. Save files elsewhere:
#    python3 scripts/download-lafayette-agendas.py --output-dir ~/Downloads/lafayette
#
# 6. Agendas only (skip minutes):
#    python3 scripts/download-lafayette-agendas.py --no-minutes
#
# 7. Skip recording shortcuts:
#    python3 scripts/download-lafayette-agendas.py --no-video
#
# 8. Run on a schedule (cron — 7 AM daily):
#    0 7 * * * cd /path/to/repo && python3 scripts/download-lafayette-agendas.py
#
# NOTE ON RECORDINGS:
#   Lafayette city council meetings are audio-only (no video). The Granicus
#   "Audio" link in each archive row opens MediaPlayer.php. The script saves
#   .url shortcuts pointing to that player page. Direct audio file URLs are
#   not predictable from clip_id alone (UUID required, found inside player HTML).
#
# NOTE ON AGENDAS:
#   Agendas are auto-detected as PDF or HTML. Most recent meetings save as .pdf.
#   Cancelled or older meetings may save as .html (GeneratedAgendaViewer format).
#   Upcoming meetings use event_id (not clip_id) in the AgendaViewer URL.
#
# NOTE ON BOARDS:
#   21 active boards are scraped by default. The "All Meetings View" (view_id=23)
#   is a stub with only upcoming rows and no archive — it is intentionally skipped.
#   The full board list is defined in the VIEWS dict at the top of this script.
