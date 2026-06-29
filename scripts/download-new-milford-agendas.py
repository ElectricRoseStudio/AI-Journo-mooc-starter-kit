#!/usr/bin/env python3
"""
Download recently uploaded municipal documents from New Milford CT (Qscend CMS).
The site uses a multi-level AJAX file browser; this script navigates each level
via ASP.NET async-postback calls and collects files uploaded in the last N days.
"""

import sys
import os
import re
import html
import gzip
import http.cookiejar
import urllib.parse
import urllib.request
import datetime

DAYS_BACK = 3
OUTPUT_DIR = "beat-archive/new-milford-agendas"
BASE_URL = "https://www.newmilford.org"
BROWSER_URL = f"{BASE_URL}/content/3086/3112/default.aspx"
UA = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"

# Root select field name in the Qscend file browser
ROOT_FIELD = "FB$F_7528"

# Shared opener with persistent cookie jar (session auto-maintained)
_cj = http.cookiejar.CookieJar()
_opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(_cj))


def fetch_raw(url, *, data=None, extra_headers=None, timeout=30):
    h = {"User-Agent": UA}
    if extra_headers:
        h.update(extra_headers)
    req = urllib.request.Request(url, data=data, headers=h)
    with _opener.open(req, timeout=timeout) as r:
        raw = r.read()
    if raw[:2] == b"\x1f\x8b":
        raw = gzip.decompress(raw)
    return raw.decode("utf-8", errors="replace")


def ajax_post(vs, vsg, ev, event_target, extra_fields):
    """Submit an ASP.NET async-postback (Delta=true) to the file browser page."""
    data_dict = {
        "__VIEWSTATE": vs,
        "__VIEWSTATEGENERATOR": vsg,
        "__EVENTVALIDATION": ev,
        "__EVENTTARGET": event_target,
        "__EVENTARGUMENT": "",
        "__ASYNCPOST": "true",
    }
    data_dict.update(extra_fields)
    return fetch_raw(
        BROWSER_URL,
        data=urllib.parse.urlencode(data_dict).encode(),
        extra_headers={
            "Content-Type": "application/x-www-form-urlencoded",
            "X-MicrosoftAjax": "Delta=true",
            "X-Requested-With": "XMLHttpRequest",
            "RadAJAXControlID": "FB_AP",
            "Referer": BROWSER_URL,
        },
    )


def parse_delta(resp):
    """Parse ASP.NET AJAX delta format: length|type|id|content|..."""
    segs = {}
    pos = 0
    while pos < len(resp):
        try:
            pipe1 = resp.index("|", pos)
            length = int(resp[pos:pipe1])
            pos = pipe1 + 1
            pipe2 = resp.index("|", pos)
            seg_type = resp[pos:pipe2]
            pos = pipe2 + 1
            pipe3 = resp.index("|", pos)
            seg_id = resp[pos:pipe3]
            pos = pipe3 + 1
            content = resp[pos:pos + length]
            pos = pos + length + 1
            segs[(seg_type, seg_id)] = content
        except Exception:
            break
    return segs


def extract_form_fields(html_text):
    vs = re.search(r'name="__VIEWSTATE"\s+[^>]*value="([^"]+)"', html_text)
    vsg = re.search(r'name="__VIEWSTATEGENERATOR"[^>]*value="([^"]+)"', html_text)
    ev = re.search(r'name="__EVENTVALIDATION"[^>]*value="([^"]+)"', html_text)
    return (
        vs.group(1) if vs else "",
        vsg.group(1) if vsg else "",
        ev.group(1) if ev else "",
    )


def parse_upload_date(span_text):
    """Parse 'uploaded on M/D/YYYY H:MM AM/PM' from file listing span."""
    m = re.search(
        r"uploaded on (\d+/\d+/\d{4})\s+(\d+:\d+\s+[AP]M)",
        span_text,
        re.IGNORECASE,
    )
    if not m:
        return None
    try:
        return datetime.datetime.strptime(
            f"{m.group(1)} {m.group(2)}", "%m/%d/%Y %I:%M %p"
        )
    except ValueError:
        return None


def slugify(text):
    text = html.unescape(text).strip()
    text = re.sub(r"[^\w\s-]", "", text)
    text = re.sub(r"[\s_]+", "-", text)
    return text.strip("-").lower()


def download_file(url, dest_path):
    if os.path.exists(dest_path):
        return False
    os.makedirs(os.path.dirname(dest_path), exist_ok=True)
    try:
        raw = fetch_raw(url, timeout=60)
        with open(dest_path, "wb") as f:
            f.write(raw.encode("latin-1") if isinstance(raw, str) else raw)
        print(f"  saved: {dest_path}")
        return True
    except Exception as e:
        print(f"  ERROR downloading {url}: {e}", file=sys.stderr)
        return False


def download_file_binary(url, dest_path):
    """Download binary file (PDF, etc.)."""
    if os.path.exists(dest_path):
        return False
    os.makedirs(os.path.dirname(dest_path), exist_ok=True)
    try:
        req = urllib.request.Request(url, headers={"User-Agent": UA})
        with _opener.open(req, timeout=60) as r:
            raw = r.read()
        with open(dest_path, "wb") as f:
            f.write(raw)
        print(f"  saved: {dest_path}")
        return True
    except Exception as e:
        print(f"  ERROR downloading {url}: {e}", file=sys.stderr)
        return False


def collect_files_from_panel(panel_html):
    """Extract (href, filename, upload_datetime) from a populated FB_F div."""
    results = []
    # The FB_F div contains a UL with nested divs; match the UL directly
    # to avoid the non-greedy </div> stopping too early inside LI items.
    ul_m = re.search(
        r'id="FB_F"[^>]*>.*?<UL[^>]*>(.*?)</UL>',
        panel_html,
        re.DOTALL | re.IGNORECASE,
    )
    if not ul_m:
        return results
    fb_html = ul_m.group(1)
    items = re.findall(r"<LI[^>]*>(.*?)</LI>", fb_html, re.DOTALL | re.IGNORECASE)
    for item in items:
        link_m = re.search(r'href="(/filestorage[^"]+)"', item, re.IGNORECASE)
        name_m = re.search(
            r">([^<]+\.(?:pdf|docx?|xlsx?|pptx?|txt))</a>", item, re.IGNORECASE
        )
        span_m = re.search(r"<span[^>]*>(.*?)</span>", item, re.IGNORECASE | re.DOTALL)
        if not link_m:
            continue
        href = link_m.group(1)
        fname = name_m.group(1).strip() if name_m else os.path.basename(href)
        upload_dt = None
        if span_m:
            upload_dt = parse_upload_date(html.unescape(span_m.group(1)))
        results.append((href, fname, upload_dt))
    return results


def get_sub_selects(panel_html, already_selected):
    """Return list of (field_name, options) for new sub-selects not yet chosen."""
    selects = re.findall(
        r'<select name="(FB\$F_\d+)"[^>]*>(.*?)</select>',
        panel_html,
        re.DOTALL | re.IGNORECASE,
    )
    result = []
    for field_name, options_html in selects:
        if field_name in already_selected:
            continue
        options = re.findall(
            r'<option[^>]*value="(\d+)"[^>]*>([^<]+)</option>',
            options_html,
            re.IGNORECASE,
        )
        result.append((field_name, options))
    return result


def navigate(
    vs,
    vsg,
    ev,
    selected_fields,  # dict of field_name -> value, accumulated so far
    event_target,     # field being changed now
    new_value,        # value selected for event_target
    label_path,       # human-readable path for logging
    cutoff,           # datetime - only keep files uploaded >= cutoff
    collected,        # list to accumulate (full_url, dest_path, upload_dt)
    max_depth=5,
    depth=0,
):
    if depth > max_depth:
        return vs, ev

    all_fields = dict(selected_fields)
    all_fields[event_target] = new_value
    all_fields["FB$AP"] = "FB$AP"

    try:
        resp = ajax_post(vs, vsg, ev, event_target, all_fields)
    except Exception as e:
        print(f"  AJAX error at {label_path}: {e}", file=sys.stderr)
        return vs, ev

    segs = parse_delta(resp)
    vs = segs.get(("hiddenField", "__VIEWSTATE"), vs)
    ev = segs.get(("hiddenField", "__EVENTVALIDATION"), ev)
    panel = segs.get(("updatePanel", "FB_FB_APPanel"), "")
    if not panel:
        return vs, ev

    # Collect any files visible at this level
    files = collect_files_from_panel(panel)
    if files:
        for href, fname, upload_dt in files:
            if upload_dt and upload_dt >= cutoff:
                full_url = BASE_URL + href
                safe_name = re.sub(r"[^\w.\-]", "_", fname)
                dest = os.path.join(OUTPUT_DIR, slugify(label_path), safe_name)
                collected.append((full_url, dest, upload_dt))

    # Find new sub-selects and recurse, prioritising recent years
    sub_selects = get_sub_selects(panel, set(all_fields.keys()))
    for field_name, options in sub_selects:
        if not options:
            continue

        year_opts = [
            (v, lbl.strip())
            for v, lbl in options
            if re.fullmatch(r"\d{4}", lbl.strip())
        ]
        non_year_opts = [
            (v, lbl.strip())
            for v, lbl in options
            if not re.fullmatch(r"\d{4}", lbl.strip())
        ]

        if year_opts:
            current_year = datetime.date.today().year
            relevant = [(v, y) for v, y in year_opts if int(y) >= current_year - 1]
            candidates = relevant or [year_opts[-1]]
        else:
            candidates = [(v, lbl) for v, lbl in non_year_opts]

        for opt_val, opt_label in candidates:
            child_path = f"{label_path}/{opt_label}"
            vs, ev = navigate(
                vs,
                vsg,
                ev,
                dict(all_fields),
                field_name,
                opt_val,
                child_path,
                cutoff,
                collected,
                max_depth=max_depth,
                depth=depth + 1,
            )

    return vs, ev


def weekend_skip():
    now = datetime.datetime.now()
    if (now.weekday() == 5 and now.hour >= 18) or (
        now.weekday() == 6 and now.hour < 12
    ):
        print("Weekend off-hours — skipping run.")
        sys.exit(0)


def main():
    weekend_skip()
    os.makedirs(OUTPUT_DIR, exist_ok=True)

    cutoff = datetime.datetime.now() - datetime.timedelta(days=DAYS_BACK)
    print(
        f"Scanning New Milford CT file browser for uploads since {cutoff:%Y-%m-%d %H:%M}"
    )

    # Load page once to establish session cookie and get initial form state
    page_html = fetch_raw(BROWSER_URL)
    vs, vsg, ev = extract_form_fields(page_html)

    # Extract all top-level categories from the root select
    root_m = re.search(
        r'name="FB\$F_7528"[^>]*>(.*?)</select>', page_html, re.DOTALL | re.IGNORECASE
    )
    if not root_m:
        print("ERROR: Could not find root category select.", file=sys.stderr)
        sys.exit(1)

    top_cats = re.findall(
        r'<option[^>]*value="(\d+)"[^>]*>([^<]+)</option>',
        root_m.group(1),
        re.IGNORECASE,
    )
    print(f"Found {len(top_cats)} top-level categories.")

    collected = []  # (full_url, dest_path, upload_dt)

    for cat_val, cat_label in top_cats:
        cat_label = html.unescape(cat_label).strip()
        print(f"Checking: {cat_label}")
        try:
            vs, ev = navigate(
                vs,
                vsg,
                ev,
                {},
                ROOT_FIELD,
                cat_val,
                cat_label,
                cutoff,
                collected,
            )
        except Exception as e:
            print(f"  Error navigating {cat_label}: {e}", file=sys.stderr)

    if not collected:
        print("No new uploads found.")
        return

    print(f"\nDownloading {len(collected)} file(s):")
    saved = 0
    for full_url, dest_path, upload_dt in collected:
        print(f"  {upload_dt:%Y-%m-%d %H:%M}  {os.path.basename(dest_path)}")
        if download_file_binary(full_url, dest_path):
            saved += 1

    print(f"\nDone — {saved} new file(s) saved to {OUTPUT_DIR}/")


if __name__ == "__main__":
    main()
