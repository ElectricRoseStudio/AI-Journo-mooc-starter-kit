#!/usr/bin/env python3
# download-cotati-agendas.py
# Download Cotati, CA municipal agendas, minutes, and recording links
# for meetings posted in the past N days.
#
# USAGE:
#   python3 scripts/download-cotati-agendas.py [options]
#
# REQUIREMENTS:
#   - Python 3.8+ (no third-party packages required)
#
# WHAT IT DOES:
#   1. Visits the PrimeGov portal to establish a session (required for downloads)
#   2. Fetches the committee list and available archived years from the API
#   3. Downloads meeting metadata for upcoming meetings and each year in the window
#   4. Filters by date and optionally by committee name
#   5. Downloads agenda, packet, and/or minutes PDFs (Azure Blob Storage via signed URL)
#   6. Saves YouTube recording shortcuts (.url files) for meetings with video
#   7. Appends a download log to the output directory
#
# SITE NOTES:
#   Platform:    PrimeGov (cotaticity.primegov.com)
#   Portal:      https://cotaticity.primegov.com/public/portal
#   Committees:  GET /api/committee/GetCommitteeesListByShowInPublicPortal
#   Years:       GET /api/v2/PublicPortal/GetArchivedMeetingYears
#   Meetings:    GET /api/v2/PublicPortal/ListArchivedMeetings?year=YYYY
#                GET /api/v2/PublicPortal/ListUpcomingMeetings
#   Documents:   GET /Public/CompiledDocument?meetingTemplateId={id}&compileOutputType=1
#                → 302 redirect to Azure Blob Storage (time-limited SAS URL)
#                compileOutputType: 1=PDF, 3=HTML (we download PDF only)
#   Doc types:   "Agenda" (PDF, used 2020–2023) — standalone agenda
#                "Packet" (PDF, used 2020–present) — full agenda packet with attachments
#                "Minutes" (PDF) — approved meeting minutes
#                "HTML Agenda" / "HTML Packet" (type=3, skipped) — HTML versions
#                "Notice of Cancellation" (PDF) — skipped by default
#   Videos:      YouTube; videoUrl field in meeting JSON
#   Committees:  City Council (id=1), Planning Commission (id=2),
#                Measure S Citizens Oversight Committee (id=3),
#                Community-Police Advisory Commission (id=4),
#                Community Development Director Hearing (id=5)

import argparse
import datetime
import http.cookiejar
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

BASE_URL = "https://cotaticity.primegov.com"
PORTAL_URL = f"{BASE_URL}/public/portal"
COMMITTEES_URL = f"{BASE_URL}/api/committee/GetCommitteeesListByShowInPublicPortal"
YEARS_URL = f"{BASE_URL}/api/v2/PublicPortal/GetArchivedMeetingYears"
ARCHIVED_MEETINGS_URL = f"{BASE_URL}/api/v2/PublicPortal/ListArchivedMeetings"
UPCOMING_MEETINGS_URL = f"{BASE_URL}/api/v2/PublicPortal/ListUpcomingMeetings"
COMPILED_DOC_URL = f"{BASE_URL}/Public/CompiledDocument"

OUTPUT_DIR = "beat-archive/cotati-agendas"
DAYS_BACK = 4
DELAY_SECONDS = 1.0

UA = ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36")

_AGENDA_RE = re.compile(r"\bagenda\b", re.I)
_PACKET_RE = re.compile(r"\bpacket\b", re.I)
_MINUTES_RE = re.compile(r"\bminutes\b", re.I)


def make_opener():
    jar = http.cookiejar.CookieJar()
    return urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar))


def fetch_json(opener, url, *, timeout=30):
    req = urllib.request.Request(url, headers={
        "User-Agent": UA,
        "Accept": "application/json, text/javascript, */*; q=0.01",
        "X-Requested-With": "XMLHttpRequest",
    })
    try:
        with opener.open(req, timeout=timeout) as r:
            return json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        print(f"  HTTP {e.code} — {url}", file=sys.stderr)
        return None
    except Exception as e:
        print(f"  ERROR fetching {url}: {e}", file=sys.stderr)
        return None


def establish_session(opener, *, timeout=20):
    """Visit portal to get session cookies required for document downloads."""
    req = urllib.request.Request(PORTAL_URL, headers={
        "User-Agent": UA,
        "Accept": "text/html,application/xhtml+xml,*/*;q=0.8",
    })
    try:
        with opener.open(req, timeout=timeout) as r:
            r.read()
        return True
    except Exception as e:
        print(f"  ERROR: {e}", file=sys.stderr)
        return False


def download_document(opener, template_id, dest_path):
    """Download a compiled PDF by templateId; returns True on success."""
    url = f"{COMPILED_DOC_URL}?meetingTemplateId={template_id}&compileOutputType=1"
    req = urllib.request.Request(url, headers={
        "User-Agent": UA,
        "Accept": "application/pdf,application/octet-stream,*/*",
    })
    try:
        with opener.open(req, timeout=60) as r:
            data = r.read()
        if not data:
            return False
        with open(dest_path, "wb") as f:
            f.write(data)
        return True
    except urllib.error.HTTPError as e:
        print(f"  HTTP {e.code} — template {template_id}", file=sys.stderr)
        return False
    except Exception as e:
        print(f"  ERROR: {e}", file=sys.stderr)
        return False


def slugify(text):
    text = text.lower().strip()
    text = re.sub(r"[/\\]", "-", text)
    text = re.sub(r"[^\w\s-]", "", text)
    text = re.sub(r"[\s_]+", "-", text)
    text = re.sub(r"-{2,}", "-", text)
    return text.strip("-")[:60]


def save_url_shortcut(url, path):
    with open(path, "w") as f:
        f.write(f"[InternetShortcut]\nURL={url}\n")


def years_needed(cutoff):
    today = datetime.date.today()
    return list(range(today.year, cutoff.year - 1, -1))


def parse_meeting_date(meeting):
    dt_str = meeting.get("dateTime", "")
    if not dt_str:
        return None
    try:
        return datetime.date.fromisoformat(dt_str[:10])
    except ValueError:
        return None


def get_docs_for_meeting(meeting, want_agendas, want_packets, want_minutes):
    """
    Return list of (doc_type, template_id) for PDF docs only.
    doc_type is one of: "agenda", "packet", "minutes".
    Picks the first match of each type from the documentList.
    """
    agenda_found = packet_found = minutes_found = False
    docs = []
    for doc in meeting.get("documentList", []):
        if doc.get("compileOutputType") != 1:
            continue  # skip HTML documents
        name = doc.get("templateName", "")
        template_id = doc.get("templateId")
        if template_id is None:
            continue

        if want_agendas and not agenda_found and _AGENDA_RE.search(name):
            docs.append(("agenda", template_id))
            agenda_found = True
        elif want_packets and not packet_found and _PACKET_RE.search(name):
            docs.append(("packet", template_id))
            packet_found = True
        elif want_minutes and not minutes_found and _MINUTES_RE.search(name):
            docs.append(("minutes", template_id))
            minutes_found = True

        if agenda_found and packet_found and minutes_found:
            break
    return docs


def main():
    parser = argparse.ArgumentParser(
        description="Download Cotati, CA municipal agendas, packets, minutes, "
                    "and recording shortcuts posted in the past N days."
    )
    parser.add_argument("--days", type=int, default=DAYS_BACK, metavar="N",
                        help=f"Look back N days (default: {DAYS_BACK})")
    parser.add_argument("--output-dir", default=OUTPUT_DIR, metavar="DIR",
                        help=f"Destination directory (default: {OUTPUT_DIR})")
    parser.add_argument("--dry-run", action="store_true",
                        help="List matching items without downloading")
    parser.add_argument("--board", metavar="NAME",
                        help="Only process boards whose name contains NAME "
                             "(case-insensitive)")
    parser.add_argument("--no-agendas", action="store_true",
                        help="Skip standalone agenda PDFs")
    parser.add_argument("--no-packets", action="store_true",
                        help="Skip agenda packet PDFs")
    parser.add_argument("--no-minutes", action="store_true",
                        help="Skip minutes PDFs")
    parser.add_argument("--no-video", action="store_true",
                        help="Skip saving recording shortcuts")
    args = parser.parse_args()

    if datetime.date.today().weekday() in (6, 0):  # Sunday, Monday
        print("Skipping — no downloads on Sunday or Monday.")
        sys.exit(0)

    cutoff = datetime.date.today() - datetime.timedelta(days=args.days)
    needed_years = years_needed(cutoff)
    want_agendas = not args.no_agendas
    want_packets = not args.no_packets
    want_minutes = not args.no_minutes

    print(f"Cutoff date : {cutoff}  ({args.days} days back)")
    print(f"Years       : {needed_years}")
    print(f"Portal      : {BASE_URL}")
    print(f"Output dir  : {args.output_dir}")
    print()

    opener = make_opener()

    print("Establishing session...")
    if not establish_session(opener):
        print("ERROR: Could not reach PrimeGov portal.", file=sys.stderr)
        sys.exit(1)
    print("  OK")

    # Fetch committee list
    committees = fetch_json(opener, COMMITTEES_URL)
    if not committees:
        print("ERROR: Could not fetch committee list.", file=sys.stderr)
        sys.exit(1)
    committee_map = {c["id"]: c["name"] for c in committees}
    print(f"  {len(committee_map)} committee(s) found")

    if args.board:
        filter_name = args.board.lower()
        keep_ids = {cid for cid, name in committee_map.items()
                    if filter_name in name.lower()}
        print(f"Board filter: '{args.board}' — matched {len(keep_ids)} committee(s):")
        for cid in sorted(keep_ids):
            print(f"  [{cid}] {committee_map[cid]}")
    else:
        keep_ids = None
    print()

    # Available archived years
    archived_years = fetch_json(opener, YEARS_URL) or []
    fetch_years = [y for y in needed_years if y in archived_years]

    all_meetings = []   # [(date, committee_name, meeting_dict)]
    seen_ids = set()

    def add_meeting(m):
        mid = m.get("id")
        if mid in seen_ids:
            return
        d = parse_meeting_date(m)
        if d is None or d < cutoff:
            return
        cid = m.get("committeeId")
        if keep_ids is not None and cid not in keep_ids:
            return
        cname = committee_map.get(cid, f"Committee {cid}")
        seen_ids.add(mid)
        all_meetings.append((d, cname, m))

    # Upcoming meetings
    print("Fetching upcoming meetings...")
    for m in (fetch_json(opener, UPCOMING_MEETINGS_URL) or []):
        add_meeting(m)

    # Archived meetings by year
    for year in fetch_years:
        print(f"Fetching archived meetings for {year}...")
        for m in (fetch_json(opener, f"{ARCHIVED_MEETINGS_URL}?year={year}") or []):
            add_meeting(m)
        time.sleep(0.3)

    all_meetings.sort(key=lambda x: (x[0], x[1]))

    if not all_meetings:
        print("\nNo meetings found within the date window.")
        return

    if args.dry_run:
        print(f"\n{'Date':<12} {'Committee':<40} {'Docs':<30} {'Video'}")
        print("-" * 95)
        for d, cname, m in all_meetings:
            docs = get_docs_for_meeting(m, want_agendas, want_packets, want_minutes)
            doc_str = ", ".join(t for t, _ in docs) or "—"
            has_vid = ("yes" if (not args.no_video
                                 and m.get("isShowVideoIcon")
                                 and m.get("videoUrl"))
                       else "no")
            print(f"{str(d):<12} {cname[:39]:<40} {doc_str:<30} {has_vid}")
        print(f"\n{len(all_meetings)} meeting(s). Re-run without --dry-run to download.")
        return

    os.makedirs(args.output_dir, exist_ok=True)
    log_path = os.path.join(args.output_dir, "download-log.txt")
    log_lines = []
    dl_ok = dl_skip = dl_fail = 0
    rec_ok = rec_skip = 0

    for d, cname, m in all_meetings:
        date_s = str(d)
        title = m.get("title", "")
        print(f"[{date_s}] {cname}" + (f" — {title}" if title != cname else ""))

        month_dir = os.path.join(args.output_dir, d.strftime("%Y-%m"))
        os.makedirs(month_dir, exist_ok=True)

        date_str = d.strftime("%Y-%m-%d")
        board_slug = slugify(cname)

        for doc_type, template_id in get_docs_for_meeting(
                m, want_agendas, want_packets, want_minutes):
            dest = os.path.join(month_dir,
                                f"{date_str}-{board_slug}-{doc_type}.pdf")
            if os.path.exists(dest):
                print(f"  skip (exists)  {os.path.basename(dest)}")
                dl_skip += 1
                continue

            print(f"  downloading    {os.path.basename(dest)}")
            if download_document(opener, template_id, dest):
                dl_ok += 1
                log_lines.append(
                    f"{datetime.datetime.now().isoformat()}  OK      {dest}")
            else:
                dl_fail += 1
                log_lines.append(
                    f"{datetime.datetime.now().isoformat()}  FAIL    "
                    f"template:{template_id}")
                if os.path.exists(dest):
                    os.remove(dest)
            time.sleep(DELAY_SECONDS)

        # Recording shortcut (YouTube)
        if (not args.no_video
                and m.get("isShowVideoIcon")
                and m.get("videoUrl")):
            rec_dir = os.path.join(args.output_dir, "recordings")
            os.makedirs(rec_dir, exist_ok=True)
            rec_fname = f"{date_str}-{board_slug}-recording.url"
            rec_path = os.path.join(rec_dir, rec_fname)
            if os.path.exists(rec_path):
                rec_skip += 1
            else:
                save_url_shortcut(m["videoUrl"], rec_path)
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
#    python3 scripts/download-cotati-agendas.py --dry-run
#
# 2. Narrow to one board:
#    python3 scripts/download-cotati-agendas.py --board "City Council"
#    python3 scripts/download-cotati-agendas.py --board "Planning"
#
# 3. Change the lookback window:
#    python3 scripts/download-cotati-agendas.py --days 7
#
# 4. Save files elsewhere:
#    python3 scripts/download-cotati-agendas.py --output-dir ~/Downloads/cotati
#
# 5. Agendas/packets only (skip minutes):
#    python3 scripts/download-cotati-agendas.py --no-minutes
#
# 6. Skip recording shortcuts:
#    python3 scripts/download-cotati-agendas.py --no-video
#
# 7. Run on a schedule (cron — 7 AM daily):
#    0 7 * * * cd /path/to/repo && python3 scripts/download-cotati-agendas.py
#
# NOTE ON DOCUMENT TYPES:
#   Cotati publishes three kinds of PDFs per meeting:
#     Agenda  — standalone agenda (used 2020–2023; sometimes alongside Packet)
#     Packet  — full agenda packet with staff reports and attachments (2020–present)
#     Minutes — approved meeting minutes (published after the meeting)
#   In recent years (2024–present) only Packet is published, not a separate Agenda.
#   The script downloads all three when present (use --no-agendas, --no-packets,
#   or --no-minutes to skip any type).
#
# NOTE ON RECORDINGS:
#   Recordings are linked as YouTube URLs in the PrimeGov API (videoUrl field).
#   The script saves .url shortcut files pointing to each meeting's YouTube page.
#   Boards that regularly post recordings: City Council, Planning Commission.
