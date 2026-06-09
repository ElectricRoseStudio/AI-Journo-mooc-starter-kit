#!/usr/bin/env python3
# download-rohnert-park-agendas.py
# Download Rohnert Park, CA municipal meeting agendas, minutes, and recording
# shortcuts for meetings posted in the past N days.
#
# USAGE:
#   python3 scripts/download-rohnert-park-agendas.py [options]
#
# REQUIREMENTS:
#   - Python 3.8+ (no third-party packages required)
#
# WHAT IT DOES:
#   1. Fetches the Granicus ViewPublisher listing (all meetings, all boards)
#   2. Parses each meeting row for name, date, agenda/minutes/video links
#   3. Filters by cutoff date (and optionally by board name)
#   4. Downloads agenda and/or minutes PDFs to beat-archive/rohnert-park-agendas/YYYY-MM/
#   5. Saves recording shortcuts (.url files) → Granicus player page
#   6. Appends a download log to beat-archive/rohnert-park-agendas/download-log.txt
#
# SITE NOTES:
#   Platform:  Granicus (rpcity.granicus.com, view_id=4)
#   Source:    https://rpcity.granicus.com/ViewPublisher.php?view_id=4
#              Embedded on https://www.rpcity.org/city_hall/city_council/meeting_central
#   Agendas:   GET /AgendaViewer.php?view_id=4&clip_id=NNNN  → 302 → S3 PDF
#              GET /AgendaViewer.php?view_id=4&event_id=NNNN → 302 → S3 PDF
#   Minutes:   GET /MinutesViewer.php?view_id=4&clip_id=NNNN&doc_id=UUID
#              → 302 → /DocumentViewer.php?file=rpcity_{hash}.pdf → PDF
#   Video:     Saved as .url shortcut → player/clip/{clip_id}?view_id=4
#              Direct MP4 at archive-video.granicus.com/rpcity/rpcity_{UUID}.mp4
#   NOTE:      rpcity.org/agendacenter has no documents; rpcity.granicus.com is the
#              actual meeting archive. There are 1,200+ clips going back to 2005.

import argparse
import datetime
import html
import os
import re
import sys
import time
import urllib.error
import urllib.request

BASE_URL = "https://rpcity.granicus.com"
VIEW_ID = 4
PUBLISHER_URL = f"{BASE_URL}/ViewPublisher.php?view_id={VIEW_ID}"
PLAYER_URL = f"{BASE_URL}/player/clip/{{clip_id}}?view_id={VIEW_ID}"

OUTPUT_DIR = "beat-archive/rohnert-park-agendas"
DAYS_BACK = 4
DELAY_SECONDS = 1.0

UA = ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36")

_AGENDA_HREF_RE = re.compile(
    r'href=["\'](?:https?:)?//rpcity\.granicus\.com(/AgendaViewer\.php\?view_id=4&(?:amp;)?(?:clip_id|event_id)=\d+)["\']',
    re.I
)
_MINUTES_HREF_RE = re.compile(
    r'href=["\'](?:https?:)?//rpcity\.granicus\.com(/MinutesViewer\.php\?view_id=4&(?:amp;)?clip_id=\d+&(?:amp;)?doc_id=[a-f0-9-]+)["\']',
    re.I
)
_CLIP_ID_RE = re.compile(r'clip_id=(\d+)')
_DATE_RE = re.compile(r'([A-Za-z]+)\s+(\d+),\s+(\d{4})')
_AMP = re.compile(r'&amp;')


def fetch_html(url, *, timeout=30):
    """Fetch URL and return decoded HTML string."""
    req = urllib.request.Request(url, headers={
        "User-Agent": UA,
        "Accept": "text/html,application/xhtml+xml,*/*;q=0.8",
    })
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            raw = r.read()
            return raw.decode("latin-1")
    except urllib.error.HTTPError as e:
        print(f"  HTTP {e.code} — {url}", file=sys.stderr)
        return None
    except Exception as e:
        print(f"  ERROR fetching {url}: {e}", file=sys.stderr)
        return None


_S3_VHOST_RE = re.compile(
    r'^https://([a-zA-Z0-9._-]+)\.s3\.amazonaws\.com/(.+)$'
)


def _s3_path_style(url):
    """
    Convert an S3 virtual-hosted URL to path-style to avoid Python's strict
    hostname validation (which rejects underscores — AWS bucket name issue).
    e.g. https://granicus_production_attachments.s3.amazonaws.com/key
      →  https://s3.amazonaws.com/granicus_production_attachments/key
    """
    m = _S3_VHOST_RE.match(url)
    if m:
        return f"https://s3.amazonaws.com/{m.group(1)}/{m.group(2)}"
    return url


class _S3RedirectHandler(urllib.request.HTTPRedirectHandler):
    """Intercept redirects before SSL connects, converting S3 vhost → path-style."""
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        newurl = _s3_path_style(newurl)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def _make_opener():
    return urllib.request.build_opener(_S3RedirectHandler())


def download_binary(url, dest_path, *, timeout=60):
    """Download url to dest_path, following redirects; return True on success."""
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


def parse_date(cell_text):
    """Parse 'May 26, 2026' style date from a table cell."""
    text = html.unescape(cell_text).replace("\xa0", " ")
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
    clean = _AMP.sub("&", path)
    return BASE_URL + clean


def parse_meetings(html_content):
    """
    Parse all listingRow elements from ViewPublisher HTML.
    Returns list of dicts: {name, date, agenda_url, minutes_url, video_url}
    """
    rows = re.findall(
        r'<tr[^>]*class=["\']listingRow["\'][^>]*>(.*?)</tr>',
        html_content, re.S | re.I
    )
    meetings = []
    for row in rows:
        # Meeting name
        name_m = re.search(
            r'<td[^>]*headers=["\']Name["\'][^>]*>(.*?)</td>', row, re.S | re.I)
        name = re.sub(r"<[^>]+>", "", name_m.group(1)).strip() if name_m else ""
        name = html.unescape(name)

        # Date — upcoming rows: headers="Date"; archive rows: headers="Date MeetingSlug"
        date_m = re.search(
            r'<td[^>]*headers="Date[^"]*"[^>]*>(.*?)</td>', row, re.S | re.I)
        date = parse_date(date_m.group(1)) if date_m else None
        if date is None:
            continue

        # Agenda URL
        am = _AGENDA_HREF_RE.search(row)
        agenda_url = fix_url(am.group(1)) if am else None

        # Minutes URL
        mm = _MINUTES_HREF_RE.search(row)
        minutes_url = fix_url(mm.group(1)) if mm else None

        # Video — detect by javascript:void(0) link; get clip_id for player URL
        has_video = bool(re.search(r'javascript:void\(0\)', row, re.I))
        video_url = None
        if has_video:
            cid_m = _CLIP_ID_RE.search(row)
            if cid_m:
                video_url = PLAYER_URL.format(clip_id=cid_m.group(1))

        meetings.append({
            "name": name,
            "date": date,
            "agenda_url": agenda_url,
            "minutes_url": minutes_url,
            "video_url": video_url,
        })

    return meetings


def main():
    parser = argparse.ArgumentParser(
        description="Download Rohnert Park, CA municipal meeting agendas, "
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
                        help="Skip agenda PDFs")
    parser.add_argument("--no-minutes", action="store_true",
                        help="Skip minutes PDFs")
    parser.add_argument("--no-video", action="store_true",
                        help="Skip saving recording shortcuts")
    args = parser.parse_args()

    now = datetime.datetime.now()
    if (now.weekday() == 5 and now.hour >= 18) or (now.weekday() == 6 and now.hour < 12):  # Saturday night, Sunday morning
        print("Skipping — no downloads on Saturday nights or Sunday mornings.")
        sys.exit(0)

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
    content = fetch_html(PUBLISHER_URL, timeout=60)
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
        print(f"\n{'Date':<12} {'Meeting':<55} {'Agnd':<5} {'Mins':<5} {'Vid'}")
        print("-" * 90)
        for m in in_window:
            has_a = "yes" if (m["agenda_url"] and want_agendas) else "no"
            has_m = "yes" if (m["minutes_url"] and want_minutes) else "no"
            has_v = "yes" if (m["video_url"] and not args.no_video) else "no"
            print(f"{str(m['date']):<12} {m['name'][:54]:<55} {has_a:<5} {has_m:<5} {has_v}")
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

        doc_types = []
        if want_agendas and m["agenda_url"]:
            doc_types.append(("agenda", m["agenda_url"]))
        if want_minutes and m["minutes_url"]:
            doc_types.append(("minutes", m["minutes_url"]))

        for doc_type, url in doc_types:
            dest = os.path.join(month_dir,
                                f"{date_str}-{board_slug}-{doc_type}.pdf")
            if os.path.exists(dest):
                print(f"  skip (exists)  {os.path.basename(dest)}")
                dl_skip += 1
                continue
            print(f"  downloading    {os.path.basename(dest)}")
            if download_binary(url, dest):
                dl_ok += 1
                log_lines.append(
                    f"{datetime.datetime.now().isoformat()}  OK      {dest}")
            else:
                dl_fail += 1
                log_lines.append(
                    f"{datetime.datetime.now().isoformat()}  FAIL    {url}")
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
#    python3 scripts/download-rohnert-park-agendas.py --dry-run
#
# 2. Narrow to one board:
#    python3 scripts/download-rohnert-park-agendas.py --board "City Council"
#    python3 scripts/download-rohnert-park-agendas.py --board "Planning"
#
# 3. Change the lookback window:
#    python3 scripts/download-rohnert-park-agendas.py --days 7
#
# 4. Save files elsewhere:
#    python3 scripts/download-rohnert-park-agendas.py --output-dir ~/Downloads/rohnert-park
#
# 5. Agendas only:
#    python3 scripts/download-rohnert-park-agendas.py --no-minutes
#
# 6. Skip recording shortcuts:
#    python3 scripts/download-rohnert-park-agendas.py --no-video
#
# 7. Run on a schedule (cron — 7 AM daily):
#    0 7 * * * cd /path/to/repo && python3 scripts/download-rohnert-park-agendas.py
#
# NOTE ON RECORDINGS:
#   Recording shortcuts open the Granicus player page for each meeting.
#   Direct MP4 downloads are available at:
#     https://archive-video.granicus.com/rpcity/rpcity_{UUID}.mp4
#   The UUID is embedded in the player page HTML (not predictable from clip_id alone).
#
# NOTE ON PLATFORM:
#   rpcity.org hosts a CivicPlus CMS with an AgendaCenter module, but that module
#   has no documents. All meeting records are on Granicus at rpcity.granicus.com.
#   The meeting_central page on rpcity.org embeds the Granicus portal in an iframe.
