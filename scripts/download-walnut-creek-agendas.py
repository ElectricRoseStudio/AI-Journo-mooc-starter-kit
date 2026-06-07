#!/usr/bin/env python3
# download-walnut-creek-agendas.py
# Download Walnut Creek, CA municipal meeting agendas, minutes, and recording
# shortcuts for meetings posted in the past N days.
#
# USAGE:
#   python3 scripts/download-walnut-creek-agendas.py [options]
#
# REQUIREMENTS:
#   - Python 3.8+ (no third-party packages required)
#
# WHAT IT DOES:
#   1. Fetches the Granicus ViewPublisher listing (all boards, all meetings)
#   2. Parses each meeting row for name, date, agenda/minutes/video links
#   3. Filters by cutoff date (and optionally by board name)
#   4. Downloads agendas as HTML files (agenda text rendered by Granicus)
#   5. Downloads minutes PDFs via MetaViewer (the "minutes" document)
#   6. Saves recording shortcuts (.url files) → Granicus MediaPlayer page
#   7. Appends a download log to the output directory
#
# SITE NOTES:
#   Platform:    Granicus (walnutcreek.granicus.com, view_id=12)
#   Source:      https://walnutcreek.granicus.com/ViewPublisher.php?view_id=12
#   Agendas:     GET /AgendaViewer.php?view_id=12&(clip_id|event_id)=N
#                → 302 → /GeneratedAgendaViewer.php?... (HTML agenda text)
#                No compiled PDF — agenda content is rendered inline as HTML
#   Minutes:     GET /MinutesViewer.php?view_id=12&clip_id=N  (HTML page)
#                → Extract MetaViewer link whose label contains "minutes"
#                GET /MetaViewer.php?view_id=12&clip_id=N&meta_id=M  (PDF)
#   Video:       Saved as .url shortcut → MediaPlayer.php?view_id=12&clip_id=N
#                Direct MP4 at archive-video.granicus.com/walnutcreek/{UUID}.mp4
#                (UUID found in MediaPlayer page HTML — not extracted by default)
#   Boards:      City Council, Design Review Commission, Planning Commission,
#                PROS Commission, Transportation Commission, Arts Commission,
#                Finance Committee, Housing & Community Dev Committee, Zoning
#                Administrator, and others (~20 board types total)
#   Note:        walnutcreekca.gov/AgendaCenter is protected by Akamai WAF;
#                all documents and recordings are served via Granicus.
#   Row quirk:   Upcoming rows use headers="Date"; archive rows use
#                headers="Date MeetingTitleSlug" (same fix as Rohnert Park).

import argparse
import datetime
import html
import os
import re
import sys
import time
import urllib.error
import urllib.request

BASE_URL = "https://walnutcreek.granicus.com"
VIEW_ID = 12
PUBLISHER_URL = f"{BASE_URL}/ViewPublisher.php?view_id={VIEW_ID}"
PLAYER_URL = f"{BASE_URL}/MediaPlayer.php?view_id={VIEW_ID}&clip_id={{clip_id}}"

OUTPUT_DIR = "beat-archive/walnut-creek-agendas"
DAYS_BACK = 4
DELAY_SECONDS = 1.0

UA = ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36")

# Match AgendaViewer URLs with either clip_id or event_id
_AGENDA_HREF_RE = re.compile(
    r'//walnutcreek\.granicus\.com/(AgendaViewer\.php\?view_id=\d+&(?:clip_id|event_id)=\d+)',
    re.I
)
# Match MinutesViewer URLs (always clip_id, no doc_id needed)
_MINUTES_HREF_RE = re.compile(
    r'//walnutcreek\.granicus\.com/(MinutesViewer\.php\?view_id=\d+&clip_id=\d+)',
    re.I
)
# Match MediaPlayer clip_id in onClick handlers
_MEDIA_CLIP_RE = re.compile(
    r'MediaPlayer\.php\?view_id=\d+&clip_id=(\d+)',
    re.I
)
# Match MetaViewer URLs with their text labels in minutes/agenda HTML pages
_META_HREF_RE = re.compile(
    r'<a\s+href="(https://walnutcreek\.granicus\.com/MetaViewer\.php\?[^"]+)"[^>]*>([^<]*)</a>',
    re.S | re.I
)
_MINUTES_LABEL_RE = re.compile(r'\bminutes\b', re.I)
_DATE_RE = re.compile(r'([A-Za-z]{3,9})\s+(\d{1,2}),\s+(\d{4})')
_AMP_RE = re.compile(r'&amp;')


def fetch_html(url, *, timeout=30):
    """Fetch URL following redirects; return decoded HTML string."""
    req = urllib.request.Request(url, headers={
        "User-Agent": UA,
        "Accept": "text/html,application/xhtml+xml,*/*;q=0.8",
    })
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            raw = r.read()
            return raw.decode("utf-8", errors="replace")
    except urllib.error.HTTPError as e:
        print(f"  HTTP {e.code} — {url}", file=sys.stderr)
        return None
    except Exception as e:
        print(f"  ERROR fetching {url}: {e}", file=sys.stderr)
        return None


def download_binary(url, dest_path, *, timeout=60):
    """Download url to dest_path; return True on success."""
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
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
    Download an agenda from AgendaViewer URL, auto-detecting format.
    AgendaViewer either redirects to GeneratedAgendaViewer (HTML) or
    DocumentViewer (PDF). Saves with .html or .pdf extension as appropriate.
    Returns the path saved, or None on failure.
    """
    req = urllib.request.Request(url, headers={
        "User-Agent": UA,
        "Accept": "text/html,application/pdf,*/*;q=0.8",
    })
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            content_type = r.headers.get("Content-Type", "")
            data = r.read()
        if not data:
            return None
        if "pdf" in content_type.lower() or data[:4] == b"%PDF":
            dest = dest_stem + ".pdf"
            with open(dest, "wb") as f:
                f.write(data)
        else:
            dest = dest_stem + ".html"
            with open(dest, "w", encoding="utf-8") as f:
                f.write(data.decode("utf-8", errors="replace"))
        return dest
    except urllib.error.HTTPError as e:
        print(f"  HTTP {e.code} — {url}", file=sys.stderr)
        return None
    except Exception as e:
        print(f"  ERROR: {e}", file=sys.stderr)
        return None


def save_url_shortcut(url, path):
    with open(path, "w") as f:
        f.write(f"[InternetShortcut]\nURL={url}\n")


def slugify(text):
    text = text.lower().strip()
    text = re.sub(r"[/\\]", "-", text)
    text = re.sub(r"[^\w\s-]", "", text)
    text = re.sub(r"[\s_]+", "-", text)
    text = re.sub(r"-{2,}", "-", text)
    return text.strip("-")[:60]


def parse_date(cell_html):
    """Parse 'Month DD, YYYY' (full or abbreviated) from a table cell."""
    text = html.unescape(cell_html).replace("\xa0", " ")
    text = re.sub(r"<[^>]+>", " ", text)
    text = re.sub(r"\s+", " ", text).strip()
    m = _DATE_RE.search(text)
    if not m:
        return None
    month_str = m.group(1)
    fmt = "%B %d %Y" if len(month_str) > 3 else "%b %d %Y"
    try:
        return datetime.datetime.strptime(
            f"{month_str} {int(m.group(2)):02d} {m.group(3)}", fmt
        ).date()
    except ValueError:
        return None


def get_minutes_pdf_url(minutes_page_url):
    """
    Fetch the MinutesViewer HTML and extract the MetaViewer URL whose label
    contains 'minutes'. Returns the URL string or None.
    """
    page = fetch_html(minutes_page_url)
    if not page:
        return None
    for url, text in _META_HREF_RE.findall(page):
        if _MINUTES_LABEL_RE.search(text.strip()):
            return url
    return None


def parse_meetings(html_content):
    """
    Parse all listItem meeting rows from ViewPublisher HTML.
    Returns list of dicts: {name, date, agenda_path, minutes_path, clip_id}
    where agenda_path/minutes_path are relative Granicus paths.
    """
    rows = re.findall(
        r'<tr[^>]*>((?:(?!</tr>).)*?headers="Name"(?:(?!</tr>).)*?)</tr>',
        html_content, re.S | re.I
    )
    meetings = []
    for row in rows:
        # Meeting name
        name_m = re.search(r'headers="Name"[^>]*>(.*?)</td>', row, re.S | re.I)
        name = re.sub(r"<[^>]+>", "", name_m.group(1)).strip() if name_m else ""
        name = re.sub(r"\s+", " ", html.unescape(name))

        # Date — upcoming: headers="Date"; archive: headers="Date SomeLongSlug"
        date_m = re.search(r'headers="Date[^"]*"[^>]*>(.*?)</td>', row, re.S | re.I)
        date = parse_date(date_m.group(1)) if date_m else None
        if date is None:
            continue

        # Agenda URL
        am = _AGENDA_HREF_RE.search(row)
        agenda_path = _AMP_RE.sub("&", am.group(1)) if am else None

        # Minutes URL
        mm = _MINUTES_HREF_RE.search(row)
        minutes_path = _AMP_RE.sub("&", mm.group(1)) if mm else None

        # Video clip_id (from MediaPlayer onClick)
        vm = _MEDIA_CLIP_RE.search(row)
        clip_id = vm.group(1) if vm else None

        meetings.append({
            "name": name,
            "date": date,
            "agenda_path": agenda_path,
            "minutes_path": minutes_path,
            "clip_id": clip_id,
        })

    return meetings


def main():
    parser = argparse.ArgumentParser(
        description="Download Walnut Creek, CA municipal meeting agendas, "
                    "minutes, and recording shortcuts for the past N days."
    )
    parser.add_argument("--days", type=int, default=DAYS_BACK, metavar="N",
                        help=f"Look back N days (default: {DAYS_BACK})")
    parser.add_argument("--output-dir", default=OUTPUT_DIR, metavar="DIR",
                        help=f"Destination directory (default: {OUTPUT_DIR})")
    parser.add_argument("--dry-run", action="store_true",
                        help="List matching items without downloading")
    parser.add_argument("--board", metavar="NAME",
                        help="Only process meetings whose name contains NAME "
                             "(case-insensitive; e.g. 'City Council', 'Planning')")
    parser.add_argument("--no-agendas", action="store_true",
                        help="Skip agenda HTML files")
    parser.add_argument("--no-minutes", action="store_true",
                        help="Skip minutes PDFs")
    parser.add_argument("--no-video", action="store_true",
                        help="Skip saving recording shortcuts")
    args = parser.parse_args()

    cutoff = datetime.date.today() - datetime.timedelta(days=args.days)
    want_agendas = not args.no_agendas
    want_minutes = not args.no_minutes

    print(f"Cutoff date : {cutoff}  ({args.days} days back)")
    print(f"Source      : {PUBLISHER_URL}")
    print(f"Output dir  : {args.output_dir}")
    if args.board:
        print(f"Board filter: '{args.board}'")
    print()

    print("Fetching meeting list...")
    content = fetch_html(PUBLISHER_URL, timeout=120)
    if not content:
        print("ERROR: Could not fetch ViewPublisher.", file=sys.stderr)
        sys.exit(1)

    all_meetings = parse_meetings(content)
    print(f"  {len(all_meetings)} total meeting entries parsed")

    # Apply date window
    in_window = [m for m in all_meetings if m["date"] >= cutoff]

    # Apply board filter
    if args.board:
        filt = args.board.lower()
        in_window = [m for m in in_window if filt in m["name"].lower()]

    in_window.sort(key=lambda m: (m["date"], m["name"]))

    if not in_window:
        print("\nNo meetings found within the date window.")
        return

    if args.dry_run:
        print(f"\n{'Date':<12} {'Meeting':<50} {'Agnd':<5} {'Mins':<5} {'Vid'}")
        print("-" * 80)
        for m in in_window:
            has_a = "yes" if (m["agenda_path"] and want_agendas) else "no"
            has_m = "yes" if (m["minutes_path"] and want_minutes) else "no"
            has_v = "yes" if (m["clip_id"] and not args.no_video) else "no"
            print(f"{str(m['date']):<12} {m['name'][:49]:<50} {has_a:<5} {has_m:<5} {has_v}")
        print(f"\n{len(in_window)} meeting(s). Re-run without --dry-run to download.")
        return

    os.makedirs(args.output_dir, exist_ok=True)
    log_path = os.path.join(args.output_dir, "download-log.txt")
    log_lines = []
    dl_ok = dl_skip = dl_fail = 0
    rec_ok = rec_skip = 0

    for m in in_window:
        date_s = str(m["date"])
        print(f"[{date_s}] {m['name']}")

        month_dir = os.path.join(args.output_dir, m["date"].strftime("%Y-%m"))
        os.makedirs(month_dir, exist_ok=True)

        date_str = m["date"].strftime("%Y-%m-%d")
        board_slug = slugify(m["name"])

        # Agenda (HTML or PDF depending on meeting's Granicus storage)
        if want_agendas and m["agenda_path"]:
            dest_stem = os.path.join(month_dir, f"{date_str}-{board_slug}-agenda")
            # Check both possible extensions before downloading
            existing = next(
                (dest_stem + ext for ext in (".html", ".pdf")
                 if os.path.exists(dest_stem + ext)), None)
            if existing:
                print(f"  skip (exists)  {os.path.basename(existing)}")
                dl_skip += 1
            else:
                agenda_url = f"{BASE_URL}/{m['agenda_path']}"
                saved = download_agenda(agenda_url, dest_stem)
                if saved:
                    print(f"  downloaded     {os.path.basename(saved)}")
                    dl_ok += 1
                    log_lines.append(
                        f"{datetime.datetime.now().isoformat()}  OK      {saved}")
                else:
                    dl_fail += 1
                    log_lines.append(
                        f"{datetime.datetime.now().isoformat()}  FAIL    {agenda_url}")
                time.sleep(DELAY_SECONDS)

        # Minutes (PDF via MetaViewer)
        if want_minutes and m["minutes_path"]:
            dest = os.path.join(month_dir, f"{date_str}-{board_slug}-minutes.pdf")
            if os.path.exists(dest):
                print(f"  skip (exists)  {os.path.basename(dest)}")
                dl_skip += 1
            else:
                print(f"  downloading    {os.path.basename(dest)}")
                minutes_url = f"{BASE_URL}/{m['minutes_path']}"
                pdf_url = get_minutes_pdf_url(minutes_url)
                if pdf_url:
                    time.sleep(0.3)
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
                else:
                    print(f"  no minutes PDF found in viewer page", file=sys.stderr)
                    dl_fail += 1
                    log_lines.append(
                        f"{datetime.datetime.now().isoformat()}  FAIL    "
                        f"no-minutes-meta:{minutes_url}")
                time.sleep(DELAY_SECONDS)

        # Recording shortcut
        if not args.no_video and m["clip_id"]:
            rec_dir = os.path.join(args.output_dir, "recordings")
            os.makedirs(rec_dir, exist_ok=True)
            rec_fname = f"{date_str}-{board_slug}-recording.url"
            rec_path = os.path.join(rec_dir, rec_fname)
            if os.path.exists(rec_path):
                rec_skip += 1
            else:
                player_url = PLAYER_URL.format(clip_id=m["clip_id"])
                save_url_shortcut(player_url, rec_path)
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
#    python3 scripts/download-walnut-creek-agendas.py --dry-run
#
# 2. Narrow to one board:
#    python3 scripts/download-walnut-creek-agendas.py --board "City Council"
#    python3 scripts/download-walnut-creek-agendas.py --board "Planning"
#    python3 scripts/download-walnut-creek-agendas.py --board "Design Review"
#
# 3. Change the lookback window:
#    python3 scripts/download-walnut-creek-agendas.py --days 90
#
# 4. Save files elsewhere:
#    python3 scripts/download-walnut-creek-agendas.py --output-dir ~/Downloads/walnut-creek
#
# 5. Agendas only:
#    python3 scripts/download-walnut-creek-agendas.py --no-minutes
#
# 6. Skip recording shortcuts:
#    python3 scripts/download-walnut-creek-agendas.py --no-video
#
# 7. Run on a schedule (cron — 7 AM daily):
#    0 7 * * * cd /path/to/repo && python3 scripts/download-walnut-creek-agendas.py
#
# NOTE ON AGENDA FORMAT:
#   Walnut Creek's Granicus serves agendas as generated HTML, not compiled PDFs.
#   Each agenda HTML file contains the full agenda text with item descriptions.
#   Staff reports and attachments for each item are linked from the agenda page
#   as MetaViewer PDFs but are NOT downloaded by default to keep file counts
#   manageable. Open the saved .html file in a browser to follow those links.
#
# NOTE ON RECORDINGS:
#   Recording shortcuts (.url files) open the Granicus MediaPlayer page.
#   Direct MP4 downloads are available at:
#     https://archive-video.granicus.com/walnutcreek/walnutcreek_{UUID}.mp4
#   The UUID is embedded in the MediaPlayer page HTML (not predictable from
#   clip_id alone). Some older meetings have audio-only recordings.
#
# NOTE ON PLATFORM:
#   The city website walnutcreekca.gov is protected by Akamai WAF and blocks
#   all automated access. All meeting records live at walnutcreek.granicus.com.
#   The view_id=12 ViewPublisher is the comprehensive all-boards archive,
#   covering ~2,100 meetings from 2006 to the present.
