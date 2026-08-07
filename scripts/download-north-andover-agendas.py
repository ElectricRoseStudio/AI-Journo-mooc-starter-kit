#!/usr/bin/env python3
# download-north-andover-agendas.py
# Download municipal meeting agendas, minutes, and video recordings for
# North Andover MA for meetings within the past N days (and up to 7 days
# ahead).
#
# NOTE: This is North Andover, MA — not to be confused with Andover MA, a
# separate town with its own separate government/schools.
#
# USAGE:
#   python3 scripts/download-north-andover-agendas.py [options]
#
# REQUIREMENTS:
#   - Python 3.6+, requests, beautifulsoup4
#   - Internet connection
#
# SITE STRUCTURE — North Andover does NOT use CivicPlus's Agenda Center
# widget like every other MA town in this repo. That widget is present in
# the page chrome (/AgendaCenter) but is genuinely broken/abandoned: its
# category-selection list is empty and its Search endpoint returns nothing
# useful even with a session cookie (confirmed directly, including full
# browser rendering). The town's REAL, currently-used systems, found via
# https://www.northandoverma.gov/129/Agendas-Minutes, are three separate
# platforms:
#
#   1. AGENDAS (all town boards except School Committee) — a public Google
#      Drive folder tree ("NA Public Web Folder/Meeting Agendas"), one
#      subfolder per board/committee, root:
#        https://drive.google.com/drive/folders/0BzHl-H9MrNbFSDhiVEtEN182QXM
#      Listed via Google's lightweight unauthenticated
#      `embeddedfolderview` endpoint (no API key, no login — the same
#      mechanism the `gdown` library uses). Folder layout is inconsistent
#      across boards but Select Board (the highest-volume board) follows
#      Committee -> YYYY -> MM-DD -> files, with per-item attachment
#      bundles (donation memos, license applications, etc.) sometimes
#      nested another level down inside the MM-DD folder — those are
#      deliberately NOT collected, only files whose name contains
#      "agenda" or "minutes". Other boards go Committee -> YYYY -> files
#      directly, with the date in the filename instead of a subfolder.
#
#   2. MINUTES (all town boards except School Committee) — a Laserfiche
#      WebLink repository, root folder id 27989 ("Minutes and Agendas"):
#        https://records.northandoverma.gov/WebLink/Browse.aspx?id=27989
#      Same Committee -> decade (e.g. "2020-2029") -> year -> files
#      layout for every board (confirmed for Select Board, Planning
#      Board, Board of Health, Zoning Board of Appeals). File names are
#      "YYYY-MM-DD {Board} {Agenda|Minutes}[-NOTE]" — most boards store
#      Minutes only here (Agendas live on Drive instead), but some
#      (confirmed: Planning Board) store both, so doc_type is read from
#      the name rather than assumed.
#      IMPORTANT: this WebLink deployment has no PDF/download button
#      anywhere in its UI (GetBasicDocumentInfo returns edocUrl: null —
#      these are scanned page images with no exported original on file).
#      What IS available to any anonymous visitor is the same OCR text
#      the viewer itself displays, via
#      DocumentService.aspx/GetTextHtmlForPage (one call per page). This
#      script fetches that text for every page and saves it as .txt —
#      the fullest faithful copy obtainable through the public site.
#      Reverse-engineered via Playwright network inspection of the public
#      document viewer (JSON POST endpoints, session cookie only, no
#      login).
#
#   3. SCHOOL COMMITTEE (Board of Education) — a third, entirely separate
#      system run by North Andover Public Schools, NOT by the town:
#      https://www.northandoverpublicschools.com/school-committee/agendas-documents
#      links to a Google Doc per school year containing a table of
#      meeting dates, each row linking out to: an "Agenda and Documents"
#      Google Doc (the agenda itself, native to Docs — not a PDF upload),
#      a "Recording" YouTube link, and an "Approved Minutes" Google Doc.
#      Current-year index doc (2026-2027; the district issues a new one
#      each school year — this ID will need updating around September):
#        https://docs.google.com/document/d/1MZA4zPaLLaDte5yk57TrAIBnVaVNIMjl-J6BYIMxRsY
#      Parsed via Docs' plain `export?format=html` endpoint (no auth);
#      each linked Agenda/Minutes doc is then downloaded as a real PDF
#      via `export?format=pdf` on its own id.
#
#   PARKS & RECREATION — confirmed there is no active public Parks &
#   Recreation board. "Youth and Recreation Council" is the closest
#   match in both the Drive and Laserfiche committee lists, but it is
#   dormant: last Drive posting 2022, last Laserfiche folder entry
#   undated/stale, and its last recorded Cablecast meeting was 2021-05-27
#   (confirmed via the Cablecast API's own search). "North Andover Youth
#   and Recreation Services" (the Drive folder some pages link to
#   instead) is similarly dead since 2022. This matches the same pattern
#   already confirmed for Andover MA in this repo: recreation is run as a
#   staff department (registrations, programming) with no regular public
#   board meetings of its own.
#
# VIDEO — North Andover Community Access & Media (NACAM) runs on
# Cablecast (Tightrope Media Systems), a different platform from every
# other town in this repo (which use Castus, PrimeGov, or Granicus). Shows
# are queried from the public JSON API:
#   GET https://ncam.northandoverma.gov/cablecastapi/v1/shows?pageSize=N&sort=-eventDate
# Titles follow "{Board Name} - MM.DD.YYYY" (confirmed for Select Board,
# School Committee, Planning Board, Conservation Commission, Zoning Board
# of Appeals). The API's own `category`/`search` query params were tested
# and don't reliably filter server-side, so matching is done client-side
# against the same board-name list built from the Laserfiche committee
# folders (plus "School Committee", hardcoded since it isn't a Laserfiche
# board). NACAM pre-schedules future cablecasts before they happen, so
# entries with an empty "vods" list (no recording yet) are skipped.
# Each matched show's video file is fetched via:
#   GET https://ncam.northandoverma.gov/cablecastapi/v1/vods/{vod_id}
# which returns a direct, unauthenticated CloudFront MP4 URL (no HLS
# reconstruction needed, unlike the Castus towns in this repo) —
# confirmed with a live HEAD request (video/mp4, ~330MB, 200 OK).

import argparse
import datetime
import os
import re
import sys
import time
import urllib.parse
import urllib.request

import requests
from bs4 import BeautifulSoup

# --- Configuration ---
UA = "NorthAndover-MA-Agendas-Downloader/1.0 (journalism research)"

GDRIVE_AGENDAS_ROOT = "0BzHl-H9MrNbFSDhiVEtEN182QXM"  # "Meeting Agendas" public folder

LASERFICHE_BASE = "https://records.northandoverma.gov/WebLink"
LASERFICHE_REPO = "TownOfNorthAndover"
LASERFICHE_ROOT_FOLDER = 27989  # "Minutes and Agendas"

# The school district issues a new index doc each school year (found via
# northandoverpublicschools.com/school-committee/agendas-documents) — this
# is the 2026-2027 doc and will need updating around September each year.
SCHOOL_COMMITTEE_INDEX_DOC = "1MZA4zPaLLaDte5yk57TrAIBnVaVNIMjl-J6BYIMxRsY"
SCHOOL_COMMITTEE_BOARD_NAME = "School Committee"

CABLECAST_BASE = "https://ncam.northandoverma.gov/cablecastapi/v1"

OUTPUT_DIR = "beat-archive/north-andover-agendas"
DAYS_BACK = 4
DAYS_AHEAD = 7
DELAY_SECONDS = 0.5
MAX_WALK_DEPTH = 4

_YEAR_RE = re.compile(r"^(\d{4})$")
_DECADE_RE = re.compile(r"^(\d{4})-(\d{4})$")
_MMDD_RE = re.compile(r"^(\d{1,2})-(\d{1,2})$")
_LEADING_DATE_RE = re.compile(r"^_?(\d{4})-(\d{1,2})-(\d{1,2})")
_LOOSE_DATE_RE = re.compile(r"(\d{1,2})[-/](\d{1,2})[-/](\d{2,4})")
_MEETING_ROW_DATE_RE = re.compile(
    r"\b(Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)\w*\s+(\d{1,2}),?\s*(\d{4})\b"
)
_MONTH_ABBR = {
    "Jan": 1, "Feb": 2, "Mar": 3, "Apr": 4, "May": 5, "Jun": 6,
    "Jul": 7, "Aug": 8, "Sep": 9, "Oct": 10, "Nov": 11, "Dec": 12,
}
_VIDEO_TITLE_RE = re.compile(r"^(.*?)\s*-\s*(\d{1,2})\.(\d{1,2})\.(\d{4})\s*$")


def _get(url, **kwargs):
    kwargs.setdefault("headers", {})["User-Agent"] = UA
    kwargs.setdefault("timeout", 30)
    return requests.get(url, **kwargs)


def _post_json(url, payload, session=None):
    sess = session or requests
    try:
        r = sess.post(
            url, json=payload,
            headers={"User-Agent": UA, "Content-Type": "application/json"},
            timeout=30,
        )
        r.raise_for_status()
        return r.json().get("data")
    except Exception as e:
        print(f"  WARNING: POST {url} failed: {e}", file=sys.stderr)
        return None


def download_url_to_file(url, dest_path, headers=None):
    # Written to a .part temp file and only renamed to dest_path once the
    # whole transfer succeeds. Large videos can run well past the outer
    # cron timeout wrapper, which kills this process with SIGTERM rather
    # than a catchable exception — without this, a killed mid-download
    # would leave a truncated file sitting at dest_path, and every future
    # run's "if os.path.exists(dest): skip" check would then silently and
    # permanently treat that corrupt file as already downloaded.
    tmp_path = dest_path + ".part"
    req = urllib.request.Request(url, headers={"User-Agent": UA, **(headers or {})})
    try:
        with urllib.request.urlopen(req, timeout=120) as r, open(tmp_path, "wb") as f:
            while True:
                chunk = r.read(1024 * 256)
                if not chunk:
                    break
                f.write(chunk)
        os.replace(tmp_path, dest_path)
        return True
    except Exception as e:
        print(f"  WARNING: {e}", file=sys.stderr)
        if os.path.exists(tmp_path):
            os.remove(tmp_path)
        return False


# --- (1) Google Drive agendas ---

def list_gdrive_folder(folder_id):
    """Non-recursive listing via Google's unauthenticated embeddedfolderview."""
    url = f"https://drive.google.com/embeddedfolderview?id={folder_id}"
    try:
        r = _get(url)
        r.raise_for_status()
    except Exception as e:
        print(f"  WARNING: Drive folder {folder_id} failed: {e}", file=sys.stderr)
        return []
    soup = BeautifulSoup(r.text, "html.parser")
    items = []
    for a in soup.find_all("a"):
        href = a.get("href", "")
        name = a.get_text(strip=True)
        if not href or not name:
            continue
        m = re.search(r"/folders/([-\w]{10,})", href)
        if m:
            items.append(("folder", m.group(1), name))
            continue
        m = re.search(r"/file/d/([-\w]{10,})", href)
        if m:
            items.append(("file", m.group(1), name))
    return items


def _extract_date_loose(name, fallback_year=None):
    m = _LEADING_DATE_RE.search(name)
    if m:
        try:
            return datetime.date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
        except ValueError:
            return None
    m = _LOOSE_DATE_RE.search(name)
    if m:
        mo, day, yr = int(m.group(1)), int(m.group(2)), int(m.group(3))
        if yr < 100:
            yr += 2000
        try:
            return datetime.date(yr, mo, day)
        except ValueError:
            return None
    return None


def _doc_type_from_name(name):
    lower = name.lower()
    if "agenda" in lower:
        return "agenda"
    if "minutes" in lower:
        return "minutes"
    return None


def walk_gdrive_committee(folder_id, board_name, cutoff, future_limit, year_ctx=None, depth=0):
    results = []
    if depth > MAX_WALK_DEPTH:
        return results
    for typ, item_id, name in list_gdrive_folder(folder_id):
        if typ == "folder":
            m = _YEAR_RE.match(name)
            if m:
                yr = int(m.group(1))
                if cutoff.year <= yr <= future_limit.year:
                    results += walk_gdrive_committee(
                        item_id, board_name, cutoff, future_limit, year_ctx=yr, depth=depth + 1
                    )
                continue
            m = _MMDD_RE.match(name)
            if m and year_ctx:
                try:
                    d = datetime.date(year_ctx, int(m.group(1)), int(m.group(2)))
                except ValueError:
                    continue
                if not (cutoff <= d <= future_limit):
                    continue
                for st, sid, sname in list_gdrive_folder(item_id):
                    if st != "file":
                        continue
                    doc_type = _doc_type_from_name(sname)
                    if not doc_type:
                        continue
                    # A file's own name may embed a different date than the
                    # MM-DD folder it sits in (e.g. prior meetings' minutes
                    # attached as backup for a later meeting's consent
                    # agenda) — prefer that date, falling back to the
                    # folder's date if the name has none.
                    file_date = _extract_date_loose(sname) or d
                    if not (cutoff <= file_date <= future_limit):
                        continue
                    results.append({
                        "board": board_name, "meeting_date": file_date, "doc_type": doc_type,
                        "source": "gdrive", "file_id": sid, "name": sname,
                    })
                continue
            # unrecognized subfolder (per-item attachment bundle) — skip
            continue
        else:
            if year_ctx is None:
                continue
            doc_type = _doc_type_from_name(name)
            if not doc_type:
                continue
            d = _extract_date_loose(name, fallback_year=year_ctx)
            if d and cutoff <= d <= future_limit:
                results.append({
                    "board": board_name, "meeting_date": d, "doc_type": doc_type,
                    "source": "gdrive", "file_id": item_id, "name": name,
                })
    return results


def collect_gdrive_agendas(cutoff, future_limit, board_filter):
    docs = []
    committees = [
        (fid, name) for typ, fid, name in list_gdrive_folder(GDRIVE_AGENDAS_ROOT)
        if typ == "folder"
    ]
    for fid, name in committees:
        if board_filter and board_filter not in name.lower():
            continue
        docs += walk_gdrive_committee(fid, name, cutoff, future_limit)
    return docs


def download_gdrive_file(file_id, dest_path):
    url = f"https://drive.google.com/uc?export=download&id={file_id}"
    return download_url_to_file(url, dest_path)


# --- (2) Laserfiche minutes/agendas ---

def laserfiche_session():
    sess = requests.Session()
    sess.headers.update({"User-Agent": UA})
    sess.get(f"{LASERFICHE_BASE}/Browse.aspx?id={LASERFICHE_ROOT_FOLDER}", timeout=30)
    return sess


def laserfiche_folder_listing(sess, folder_id):
    data = _post_json(
        f"{LASERFICHE_BASE}/FolderListingService.aspx/GetFolderListing2",
        {"repoName": LASERFICHE_REPO, "folderId": folder_id, "getNewListing": True,
         "start": 0, "end": 200, "sortColumn": "", "sortAscending": True},
        session=sess,
    )
    return (data or {}).get("results", [])


def walk_laserfiche_committee(sess, folder_id, board_name, cutoff, future_limit, depth=0):
    results = []
    if depth > MAX_WALK_DEPTH:
        return results
    for entry in laserfiche_folder_listing(sess, folder_id):
        name = entry.get("name", "")
        entry_id = entry.get("entryId")
        if entry.get("type") == 0:  # folder
            m = _DECADE_RE.match(name)
            if m:
                lo, hi = int(m.group(1)), int(m.group(2))
                if hi < cutoff.year or lo > future_limit.year:
                    continue
                results += walk_laserfiche_committee(
                    sess, entry_id, board_name, cutoff, future_limit, depth + 1
                )
                continue
            m = _YEAR_RE.match(name)
            if m:
                yr = int(m.group(1))
                if not (cutoff.year <= yr <= future_limit.year):
                    continue
                results += walk_laserfiche_committee(
                    sess, entry_id, board_name, cutoff, future_limit, depth + 1
                )
                continue
            # unrecognized folder name — recurse anyway, bounded by MAX_WALK_DEPTH
            results += walk_laserfiche_committee(
                sess, entry_id, board_name, cutoff, future_limit, depth + 1
            )
        else:  # file
            doc_type = _doc_type_from_name(name)
            if not doc_type:
                continue
            d = _extract_date_loose(name)
            if d and cutoff <= d <= future_limit:
                results.append({
                    "board": board_name, "meeting_date": d, "doc_type": doc_type,
                    "source": "laserfiche", "entry_id": entry_id, "name": name,
                })
    return results


def list_laserfiche_committees(sess):
    return [
        (e["entryId"], e["name"]) for e in laserfiche_folder_listing(sess, LASERFICHE_ROOT_FOLDER)
        if e.get("type") == 0
    ]


def collect_laserfiche(cutoff, future_limit, board_filter):
    sess = laserfiche_session()
    docs = []
    committees = list_laserfiche_committees(sess)
    for fid, name in committees:
        if board_filter and board_filter not in name.lower():
            continue
        docs += walk_laserfiche_committee(sess, fid, name, cutoff, future_limit)
    return docs, sess, [name for _, name in committees]


def download_laserfiche_text(sess, entry_id, dest_path):
    info = _post_json(
        f"{LASERFICHE_BASE}/DocumentService.aspx/GetBasicDocumentInfo",
        {"repoName": LASERFICHE_REPO, "entryId": entry_id}, session=sess,
    )
    page_count = (info or {}).get("pageCount") or 0
    if not page_count:
        print("  WARNING: no pages returned for Laserfiche entry", entry_id, file=sys.stderr)
        return False
    parts = []
    for page_num in range(1, page_count + 1):
        page = _post_json(
            f"{LASERFICHE_BASE}/DocumentService.aspx/GetTextHtmlForPage",
            {"repoName": LASERFICHE_REPO, "documentId": entry_id, "pageNum": page_num,
             "showAnn": True, "searchUuid": ""},
            session=sess,
        )
        parts.append((page or {}).get("text") or "")
    text = f"\n\n----- page break -----\n\n".join(parts)
    if not text.strip():
        return False
    with open(dest_path, "w", encoding="utf-8") as f:
        f.write(text)
    return True


# --- (3) School Committee ---

def _resolve_google_redirect(href):
    if "google.com/url?q=" in href:
        qs = urllib.parse.parse_qs(urllib.parse.urlparse(href).query)
        return qs.get("q", [href])[0]
    return href


def fetch_gdoc_html(doc_id):
    url = f"https://docs.google.com/document/d/{doc_id}/export?format=html"
    try:
        r = _get(url)
        r.raise_for_status()
    except Exception as e:
        print(f"  WARNING: Google Doc {doc_id} failed: {e}", file=sys.stderr)
        return None
    return BeautifulSoup(r.text, "html.parser")


def _parse_meeting_date(text):
    m = _MEETING_ROW_DATE_RE.search(text)
    if not m:
        return None
    mon, day, yr = m.group(1), int(m.group(2)), int(m.group(3))
    try:
        return datetime.date(yr, _MONTH_ABBR[mon], day)
    except (ValueError, KeyError):
        return None


def collect_school_committee(cutoff, future_limit):
    """Docs (agenda/minutes) plus video links from the SC master index doc."""
    docs, videos = [], []
    soup = fetch_gdoc_html(SCHOOL_COMMITTEE_INDEX_DOC)
    if not soup:
        return docs, videos
    for tr in soup.find_all("tr"):
        cells = tr.find_all("td")
        if not cells:
            continue
        row_text = cells[0].get_text(" ", strip=True)
        d = _parse_meeting_date(row_text)
        if not d or not (cutoff <= d <= future_limit):
            continue
        for cell in cells[1:]:
            a = cell.find("a")
            if not a or not a.get("href"):
                continue
            href = _resolve_google_redirect(a["href"])
            label = cell.get_text(strip=True).lower()
            if "youtube.com" in href or "youtu.be" in href:
                videos.append({"board": SCHOOL_COMMITTEE_BOARD_NAME, "meeting_date": d, "url": href})
            elif "minutes" in label:
                doc_id = _doc_or_file_id(href)
                if doc_id:
                    docs.append({
                        "board": SCHOOL_COMMITTEE_BOARD_NAME, "meeting_date": d, "doc_type": "minutes",
                        "source": "gdoc" if "/document/" in href else "gdrive", "file_id": doc_id, "name": row_text,
                    })
            else:
                doc_id = _doc_or_file_id(href)
                if doc_id:
                    docs.append({
                        "board": SCHOOL_COMMITTEE_BOARD_NAME, "meeting_date": d, "doc_type": "agenda",
                        "source": "gdoc" if "/document/" in href else "gdrive", "file_id": doc_id, "name": row_text,
                    })
    return docs, videos


def _doc_or_file_id(href):
    m = re.search(r"/document/d/([-\w]{10,})", href)
    if m:
        return m.group(1)
    m = re.search(r"/file/d/([-\w]{10,})", href)
    if m:
        return m.group(1)
    return None


def download_gdoc_pdf(doc_id, dest_path):
    url = f"https://docs.google.com/document/d/{doc_id}/export?format=pdf"
    return download_url_to_file(url, dest_path)


# --- (4) Cablecast video ---

def fetch_cablecast_shows(search=None, page_size=20):
    # The API's own eventDate range params don't filter server-side
    # (confirmed directly), and a plain -eventDate sort surfaces months of
    # pre-scheduled non-government content (podcasts, church services)
    # ahead of any recent board meeting. `search` DOES filter server-side,
    # so this is called once per known board name rather than paginating
    # the whole unfiltered feed.
    params = {"pageSize": page_size, "sort": "-eventDate"}
    if search:
        params["search"] = search
    url = f"{CABLECAST_BASE}/shows?{urllib.parse.urlencode(params)}"
    try:
        r = _get(url)
        r.raise_for_status()
        return r.json().get("shows", [])
    except Exception as e:
        print(f"  WARNING: Cablecast shows fetch failed: {e}", file=sys.stderr)
        return []


def fetch_cablecast_vod_url(vod_id):
    url = f"{CABLECAST_BASE}/vods/{vod_id}"
    try:
        r = _get(url)
        r.raise_for_status()
        return (r.json().get("vod") or {}).get("url")
    except Exception as e:
        print(f"  WARNING: Cablecast vod {vod_id} fetch failed: {e}", file=sys.stderr)
        return None


def collect_videos(cutoff, future_limit, board_filter, known_boards):
    all_boards = set(known_boards) | {SCHOOL_COMMITTEE_BOARD_NAME}
    if board_filter:
        all_boards = {b for b in all_boards if board_filter in b.lower()}
    videos = []
    seen_show_ids = set()
    for board in sorted(all_boards):
        for show in fetch_cablecast_shows(search=board):
            if show["id"] in seen_show_ids:
                continue
            title = show.get("title") or ""
            m = _VIDEO_TITLE_RE.match(title)
            if not m:
                continue
            board_candidate, mo, day, yr = m.group(1).strip(), int(m.group(2)), int(m.group(3)), int(m.group(4))
            try:
                meeting_date = datetime.date(yr, mo, day)
            except ValueError:
                continue
            if not (cutoff <= meeting_date <= future_limit):
                continue
            if not show.get("vods"):
                continue  # scheduled but not yet recorded/published
            candidate_lower = board_candidate.lower()
            bl = board.lower()
            if not (candidate_lower in bl or bl in candidate_lower):
                continue
            seen_show_ids.add(show["id"])
            videos.append({
                "board": board, "meeting_date": meeting_date,
                "vod_id": show["vods"][0], "title": title,
            })
    return videos


def download_video(vod_id, dest_path):
    url = fetch_cablecast_vod_url(vod_id)
    if not url:
        return False
    return download_url_to_file(url, dest_path)


# --- File naming ---

def slugify(text, max_len=55):
    text = str(text).lower().strip()
    text = re.sub(r"[/\\&]", "-", text)
    text = re.sub(r"[^\w\s-]", "", text)
    text = re.sub(r"[\s_]+", "-", text)
    text = re.sub(r"-{2,}", "-", text)
    return text.strip("-")[:max_len]


def make_path(board, doc_type, meeting_date, output_dir, ext=".pdf", counter=0):
    date_str = meeting_date.strftime("%Y-%m-%d")
    month_dir = os.path.join(output_dir, meeting_date.strftime("%Y-%m"))
    os.makedirs(month_dir, exist_ok=True)
    suffix = f"-{counter}" if counter else ""
    return os.path.join(month_dir, f"{date_str}-{slugify(board)}-{slugify(doc_type)}{suffix}{ext}")


def assign_counters(items, key_fn):
    seen = {}
    for item in items:
        key = key_fn(item)
        item["counter"] = seen.get(key, 0)
        seen[key] = item["counter"] + 1


# --- Main ---

def main():
    parser = argparse.ArgumentParser(
        description=(
            "Download North Andover MA municipal agendas, minutes, and video "
            "recordings for meetings within the past N days (and up to M days ahead)."
        )
    )
    parser.add_argument("--days", type=int, default=DAYS_BACK, metavar="N",
                        help=f"Look back N days (default: {DAYS_BACK})")
    parser.add_argument("--ahead", type=int, default=DAYS_AHEAD, metavar="N",
                        help=f"Also include meetings up to N days ahead (default: {DAYS_AHEAD})")
    parser.add_argument("--output-dir", default=OUTPUT_DIR, metavar="DIR",
                        help=f"Destination directory (default: {OUTPUT_DIR})")
    parser.add_argument("--dry-run", action="store_true", help="List matching items without downloading")
    parser.add_argument("--board", metavar="NAME",
                        help="Only process boards whose name contains NAME (case-insensitive)")
    parser.add_argument("--no-minutes", action="store_true", help="Skip minutes, download agendas only")
    parser.add_argument("--no-agendas", action="store_true", help="Skip agendas, download minutes only")
    parser.add_argument("--include-video", action="store_true", help="Also download NACAM video recordings")
    parser.add_argument("--video-only", action="store_true", help="Download only video recordings")
    args = parser.parse_args()

    now = datetime.datetime.now()
    if (now.weekday() == 5 and now.hour >= 18) or (now.weekday() == 6 and now.hour < 12):
        print("Skipping — no downloads on Saturday nights or Sunday mornings.")
        sys.exit(0)

    do_docs = not args.video_only
    do_video = args.include_video or args.video_only

    today = datetime.date.today()
    cutoff = today - datetime.timedelta(days=args.days)
    future_limit = today + datetime.timedelta(days=args.ahead)
    board_filter = args.board.lower() if args.board else None

    print(f"Date window : {cutoff} to {future_limit}")
    if not args.dry_run:
        print(f"Output dir  : {args.output_dir}")
    print()

    docs = []
    laserfiche_committees = set()
    if do_docs:
        print("Fetching Google Drive agendas...")
        gdrive_docs = collect_gdrive_agendas(cutoff, future_limit, board_filter)
        print(f"  {len(gdrive_docs)} found")

        print("Fetching Laserfiche minutes/agendas...")
        laserfiche_docs, _lf_sess, laserfiche_committees_list = collect_laserfiche(cutoff, future_limit, board_filter)
        laserfiche_committees = set(laserfiche_committees_list)
        print(f"  {len(laserfiche_docs)} found")

        print("Fetching School Committee agendas/minutes...")
        sc_docs, sc_videos = collect_school_committee(cutoff, future_limit)
        print(f"  {len(sc_docs)} found")

        docs = gdrive_docs + laserfiche_docs + sc_docs
        if args.no_minutes:
            docs = [d for d in docs if d["doc_type"] != "minutes"]
        if args.no_agendas:
            docs = [d for d in docs if d["doc_type"] != "agenda"]
        print(f"Documents   : {len(docs)} found\n")
    else:
        print("Fetching School Committee video links...")
        _, sc_videos = collect_school_committee(cutoff, future_limit)

    videos = []
    if do_video:
        if not laserfiche_committees:
            print("Fetching known board list (for video-title matching)...")
            laserfiche_committees = {
                name for _, name in list_laserfiche_committees(laserfiche_session())
            }
        print("Fetching NACAM (Cablecast) recent shows...")
        videos = collect_videos(cutoff, future_limit, board_filter, laserfiche_committees)
        # School Committee video links from the SC index doc's own
        # "Recording" column (YouTube) are a separate feed from Cablecast.
        if not board_filter or board_filter in SCHOOL_COMMITTEE_BOARD_NAME.lower():
            for v in sc_videos:
                videos.append({
                    "board": v["board"], "meeting_date": v["meeting_date"],
                    "vod_id": None, "youtube_url": v["url"], "title": f"{v['board']} {v['meeting_date']}",
                })
        print(f"Video       : {len(videos)} found\n")

    docs.sort(key=lambda x: (x["meeting_date"], x["board"]), reverse=True)
    videos.sort(key=lambda x: (x["meeting_date"], x["board"]), reverse=True)
    # Keyed on the same fields make_path() uses for the filename (not
    # "source") so that same-day same-type docs from different sources
    # don't collide on the same output filename.
    assign_counters(docs, lambda d: (d["board"], d["meeting_date"], d["doc_type"]))
    assign_counters(videos, lambda v: (v["board"], v["meeting_date"]))

    total = len(docs) + len(videos)
    if total == 0:
        print("No items found in the date window.")
        return

    if args.dry_run:
        if docs:
            print(f"{'Board':<35} {'Date':<12} {'Type':<10} Source")
            print("-" * 80)
            for d in docs:
                print(f"{d['board'][:34]:<35} {d['meeting_date']!s:<12} {d['doc_type']:<10} {d['source']}")
            print()
        if videos:
            print(f"{'Board':<35} {'Date':<12} Video")
            print("-" * 80)
            for v in videos:
                print(f"{v['board'][:34]:<35} {v['meeting_date']!s:<12} {v['title']}")
            print()
        print(f"{total} item(s). Re-run without --dry-run to download.")
        return

    os.makedirs(args.output_dir, exist_ok=True)
    log_path = os.path.join(args.output_dir, "download-log.txt")
    log_lines = []
    downloaded = skipped = failed = 0
    lf_sess = laserfiche_session()

    for d in docs:
        ext = ".txt" if d["source"] == "laserfiche" else ".pdf"
        dest = make_path(d["board"], d["doc_type"], d["meeting_date"], args.output_dir, ext=ext, counter=d["counter"])
        label = os.path.basename(dest)
        if os.path.exists(dest):
            print(f"  skip (exists)  {label}")
            skipped += 1
            continue
        print(f"  [{d['meeting_date']}] {d['board']} — {d['doc_type']} ({d['source']})")
        print(f"  downloading    {label}")
        if d["source"] == "gdrive":
            ok = download_gdrive_file(d["file_id"], dest)
        elif d["source"] == "gdoc":
            ok = download_gdoc_pdf(d["file_id"], dest)
        else:  # laserfiche
            ok = download_laserfiche_text(lf_sess, d["entry_id"], dest)
        if ok:
            downloaded += 1
            log_lines.append(f"{datetime.datetime.now().isoformat()}  OK       {dest}")
        else:
            failed += 1
            log_lines.append(f"{datetime.datetime.now().isoformat()}  FAILED   {d.get('name', dest)}")
        time.sleep(DELAY_SECONDS)

    for v in videos:
        dest = make_path(v["board"], "video", v["meeting_date"], args.output_dir, ext=".mp4", counter=v["counter"])
        label = os.path.basename(dest)
        if os.path.exists(dest):
            print(f"  skip (exists)  {label}")
            skipped += 1
            continue
        print(f"  [{v['meeting_date']}] {v['board']} — video ({v['title']})")
        print(f"  downloading    {label}")
        if v.get("vod_id"):
            ok = download_video(v["vod_id"], dest)
        else:
            print("  WARNING: YouTube-only video link, not downloaded (no yt-dlp dependency for this town):", v.get("youtube_url"), file=sys.stderr)
            ok = False
        if ok:
            downloaded += 1
            log_lines.append(f"{datetime.datetime.now().isoformat()}  OK       {dest}")
        else:
            failed += 1
            log_lines.append(f"{datetime.datetime.now().isoformat()}  FAILED   video {v.get('vod_id') or v.get('youtube_url')}")

    if log_lines:
        with open(log_path, "a") as f:
            f.write("\n".join(log_lines) + "\n")

    print()
    print(f"Done — downloaded: {downloaded}  skipped: {skipped}  failed: {failed}")
    print(f"Files in: {args.output_dir}")
    if log_lines:
        print(f"Log:      {log_path}")


if __name__ == "__main__":
    main()


# --- Tips ---
#
# 1. Preview without downloading:
#    python3 scripts/download-north-andover-agendas.py --dry-run
#
# 2. Just Select Board:
#    python3 scripts/download-north-andover-agendas.py --board "Select Board"
#
# 3. PDFs/text only (no video — the default; --include-video/--video-only turn it on):
#    python3 scripts/download-north-andover-agendas.py
#
# 4. Video only:
#    python3 scripts/download-north-andover-agendas.py --video-only
#
# 5. Change the lookback window:
#    python3 scripts/download-north-andover-agendas.py --days 14
#
# 6. Run on a schedule (cron — evening):
#    0 19 * * 1-5 cd /path/to/repo && python3 scripts/download-north-andover-agendas.py
#
# COVERAGE: School Committee (Board of Education) is covered via its own
# Google Doc-based system on northandoverpublicschools.com (separate from
# every other board's Drive/Laserfiche/Cablecast setup — see the platform
# notes at the top of this file). There is no active Parks & Recreation
# board — confirmed dormant since 2021-2022 across Drive, Laserfiche, and
# Cablecast records; recreation is a staff department, not a public board
# (same pattern as Andover MA elsewhere in this repo).
