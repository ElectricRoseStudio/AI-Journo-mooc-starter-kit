#!/usr/bin/env python3
"""Delete downloaded municipal documents older than KEEP_DAYS days from beat-archive.

Preserves log files (*.txt, *.log, *.md). Runs from the project root so that
relative beat-archive paths resolve correctly.
"""

import os
import sys
import time
import datetime

KEEP_DAYS = 5
BEAT_ARCHIVE = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "beat-archive")
PRESERVE_SUFFIXES = {".txt", ".log", ".md"}

cutoff = time.time() - KEEP_DAYS * 86400
now_str = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")

deleted = []
errors = []

for dirpath, _dirnames, filenames in os.walk(BEAT_ARCHIVE):
    for fname in filenames:
        if os.path.splitext(fname)[1].lower() in PRESERVE_SUFFIXES:
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
