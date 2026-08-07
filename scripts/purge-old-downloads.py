#!/usr/bin/env python3
"""Delete downloaded municipal documents older than KEEP_DAYS days from beat-archive.

Preserves log files (*.txt, *.log, *.md). Runs from the project root so that
relative beat-archive paths resolve correctly.

Every downloader in this repo names real per-meeting documents with a
leading "YYYY-MM-DD-" date (its make_path()/make_dest_path() convention),
while append-only logs (download-log.txt, media-archive.txt,
yt-archive.txt, etc.) never carry that prefix — including the several
towns that keep a separate download-log.txt per dated month
subdirectory rather than one master log, which rules out using directory
structure alone to tell the two apart. That filename prefix, not just
the extension, decides what counts as a "log" here: North Andover MA's
downloader saves Laserfiche minutes as real dated .txt content (that
deployment has no PDF export available), so a blanket .txt exemption
would keep those forever instead of purging them like every other town's
documents.
"""

import os
import re
import sys
import time
import datetime

KEEP_DAYS = 5
BEAT_ARCHIVE = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "beat-archive")
PRESERVE_SUFFIXES = {".txt", ".log", ".md"}
_DATED_DOC_RE = re.compile(r"^\d{4}-\d{2}-\d{2}-")

cutoff = time.time() - KEEP_DAYS * 86400
now_str = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")

deleted = []
errors = []

for dirpath, _dirnames, filenames in os.walk(BEAT_ARCHIVE):
    for fname in filenames:
        is_dated_doc = bool(_DATED_DOC_RE.match(fname))
        if not is_dated_doc and os.path.splitext(fname)[1].lower() in PRESERVE_SUFFIXES:
            continue
        fpath = os.path.join(dirpath, fname)
        try:
            mtime = os.path.getmtime(fpath)
            if mtime < cutoff:
                os.remove(fpath)
                deleted.append(fpath)
        except OSError as exc:
            errors.append(f"{fpath}: {exc}")

print(f"[{now_str}] purge-old-downloads: removed {len(deleted)} file(s) older than {KEEP_DAYS} days")
for path in deleted:
    print(f"  deleted {path}")
for msg in errors:
    print(f"  ERROR  {msg}", file=sys.stderr)
