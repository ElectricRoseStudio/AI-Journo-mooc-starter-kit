#!/usr/bin/env python3
# download-langhorne-boro-agendas.py
# Downloads Langhorne Borough, PA meeting documents (agendas, minutes).
#
# langhorneborough.com is a WordPress site that stores documents as a
# 'document' custom post type (functional-gov-wp plugin). Each post has a
# featured-media attachment that IS the PDF.
#
# API approach:
#   GET /wp-json/wp/v2/document?orderby=modified&order=desc&per_page=100
#   Filter by `modified` field >= cutoff.
#   For each matching post, resolve the wp:featuredmedia link to get source_url.
#   Download PDFs not already in the local archive.
#
# The `modified` timestamp on the document post is within seconds of the actual
# PDF upload time, so it's a reliable proxy for "when was this published."
#
# No video channel identified for this borough.

import argparse
import datetime
import json
import os
import re
import urllib.error
import urllib.parse
import urllib.request

UA      = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36"
)
BASE    = "https://langhorneborough.com"
API     = BASE + "/wp-json/wp/v2"

REPO_DIR   = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUTPUT_DIR = os.path.join(REPO_DIR, "beat-archive", "langhorne-boro-agendas")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _req(url, method="GET"):
    return urllib.request.Request(url, headers={"User-Agent": UA}, method=method)


def api_get(path, params=None):
    url = API + path
    if params:
        url += "?" + urllib.parse.urlencode(params)
    try:
        with urllib.request.urlopen(_req(url), timeout=30) as r:
            return json.loads(r.read().decode("utf-8", errors="replace"))
    except urllib.error.HTTPError as e:
        print(f"  API error {e.code}: {url}")
        return None


def subdir_for(dt):
    path = os.path.join(OUTPUT_DIR, dt.strftime("%Y-%m"))
    os.makedirs(path, exist_ok=True)
    return path


def parse_wp_date(s):
    """Parse '2026-06-09T15:37:07' → datetime."""
    try:
        return datetime.datetime.strptime(s, "%Y-%m-%dT%H:%M:%S")
    except (ValueError, TypeError):
        return None


def safe_filename(title, doc_id, ext=".pdf"):
    """Convert a WordPress document title to a safe filesystem name."""
    name = re.sub(r"[&#;].*?;", " ", title)   # strip HTML entities
    name = re.sub(r"[^\w\s-]", "", name)
    name = re.sub(r"\s+", "-", name.strip())
    return f"{name.lower()}-id{doc_id}{ext}"


# ---------------------------------------------------------------------------
# Fetch documents and PDFs
# ---------------------------------------------------------------------------

def get_media_url(media_href):
    """
    Given the wp:featuredmedia href (e.g. /wp-json/wp/v2/media/7885),
    return (source_url, mime_type, modified_dt) or (None, None, None).
    """
    try:
        with urllib.request.urlopen(_req(media_href + "?_fields=source_url,mime_type,modified"), timeout=20) as r:
            media = json.loads(r.read().decode("utf-8", errors="replace"))
        source = media.get("source_url")
        mime   = media.get("mime_type", "")
        mod_dt = parse_wp_date(media.get("modified", ""))
        return source, mime, mod_dt
    except Exception as e:
        print(f"    Media fetch failed {media_href}: {e}")
        return None, None, None


def download_doc(source_url, out_path, dry_run):
    with urllib.request.urlopen(_req(source_url), timeout=120) as r:
        data = r.read()
    if not dry_run:
        with open(out_path, "wb") as f:
            f.write(data)
    return True


def process_documents(cutoff, dry_run):
    """
    Fetch all documents modified after cutoff, download their PDFs.
    Returns count of files downloaded.
    """
    # `modified_after` filters by the document's `modified` field (upload time).
    # Note: `after` filters by `date` (publish/meeting date) — not what we want here,
    # since minutes can be published months after the meeting they cover.
    cutoff_str = cutoff.strftime("%Y-%m-%dT%H:%M:%S")
    params = {
        "per_page":       100,
        "orderby":        "modified",
        "order":          "desc",
        "modified_after": cutoff_str,
        "_fields":        "id,title,modified,_links",
    }
    print(f"\nQuerying WP API for documents modified after {cutoff.date()} ...")
    docs = api_get("/document", params)
    if docs is None:
        docs = []
    if not docs:
        print("  No documents in window.")
        return 0

    print(f"  Found {len(docs)} document(s)")
    downloaded = 0

    for doc in docs:
        doc_id    = doc["id"]
        title_raw = doc.get("title", {}).get("rendered", f"document-{doc_id}")
        title     = re.sub(r"&[#\w]+;", " ", title_raw).strip()
        mod_str   = doc.get("modified", "")
        mod_dt    = parse_wp_date(mod_str)
        links     = doc.get("_links", {})

        fm_list = links.get("wp:featuredmedia", [])
        if not fm_list:
            print(f"  [{doc_id}] {title} — no featured media, skipping")
            continue

        media_href = fm_list[0]["href"]
        source_url, mime, _ = get_media_url(media_href)

        if not source_url:
            print(f"  [{doc_id}] {title} — could not resolve media URL")
            continue
        if mime and not mime.startswith("application/pdf"):
            print(f"  [{doc_id}] {title} — not a PDF ({mime}), skipping")
            continue

        ext      = os.path.splitext(source_url)[-1] or ".pdf"
        fname    = safe_filename(title, doc_id, ext)
        date_dir = mod_dt if mod_dt else datetime.datetime.now()
        out_dir  = subdir_for(date_dir)
        out_path = os.path.join(out_dir, fname)

        if os.path.exists(out_path):
            print(f"  Already have: {fname}")
            continue

        mod_label = mod_dt.strftime("%Y-%m-%d") if mod_dt else "unknown"
        print(f"  Downloading [{mod_label}]: {fname}")
        if dry_run:
            downloaded += 1
            continue

        try:
            download_doc(source_url, out_path, dry_run)
            downloaded += 1

            log_path = os.path.join(out_dir, "download-log.txt")
            with open(log_path, "a") as lf:
                lf.write(
                    f"{datetime.datetime.now().isoformat()}  "
                    f"[{doc_id}]  {fname}  modified={mod_str}  {source_url}\n"
                )
        except Exception as e:
            print(f"    Download failed: {e}")

    return downloaded


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        description="Download Langhorne Borough, PA meeting documents via WordPress REST API."
    )
    parser.add_argument(
        "--lookback", type=int, default=3,
        help="Days back for document 'modified' cutoff (default 3). "
             "The WP API 'modified' field closely matches the actual PDF upload time.",
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Print what would be downloaded; don't write files",
    )
    args = parser.parse_args()

    now    = datetime.datetime.now()
    cutoff = datetime.datetime.combine(
        now.date() - datetime.timedelta(days=args.lookback),
        datetime.time.min,
    )

    print(f"Document modified cutoff: {cutoff.date()}")

    n = process_documents(cutoff, args.dry_run)
    if n == 0:
        print("\nNo new documents within the cutoff window.")
    else:
        print(f"\n{n} document(s) downloaded.")
    print("Done.")


if __name__ == "__main__":
    main()
