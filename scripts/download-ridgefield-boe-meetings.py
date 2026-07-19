#!/usr/bin/env python3
# download-ridgefield-boe-meetings.py
# Download Ridgefield Board of Education meeting videos from YouTube.
#
# USAGE:
#   python3 scripts/download-ridgefield-boe-meetings.py [options]
#
# REQUIREMENTS:
#   yt-dlp: pip install yt-dlp   OR   sudo apt install yt-dlp
#
# SOURCE:
#   Ridgefield Public Schools BOE YouTube channel (RPS BOE)
#   https://www.youtube.com/channel/UCFK7CGQjWPQQQ05N2_2zROA
#
# OUTPUT STRUCTURE:
#   beat-archive/ridgefield-boe-meetings/
#     YYYYMMDD-<title-slug>.mp4
#     YYYYMMDD-<title-slug>.info.json
#     YYYYMMDD-<title-slug>.description
#     download-log.txt

import argparse
import datetime
import os
import re
import shutil
import subprocess
import sys

# ── Configuration ──────────────────────────────────────────────────────────────

# The channel's "Videos" tab is empty; all content posts via live streams.
# The uploads playlist (UC→UU prefix) surfaces them all and supports --dateafter.
CHANNEL_URL = "https://www.youtube.com/playlist?list=UUFK7CGQjWPQQQ05N2_2zROA"
OUTPUT_DIR  = "beat-archive/ridgefield-boe-meetings"
DAYS_BACK   = 4

# ── Utilities ──────────────────────────────────────────────────────────────────

def ensure_dir(path):
    os.makedirs(path, exist_ok=True)
    return path

# ── Download ───────────────────────────────────────────────────────────────────

def download_videos(cutoff, output_dir, dry_run, audio_only):
    ytdlp = shutil.which("yt-dlp")
    if not ytdlp:
        print("ERROR: yt-dlp not found.", file=sys.stderr)
        print("Install with:  pip install yt-dlp   or   sudo apt install yt-dlp", file=sys.stderr)
        sys.exit(1)

    date_str = cutoff.strftime("%Y%m%d")
    out_tmpl  = os.path.join(output_dir, "%(upload_date)s-%(title)s.%(ext)s")

    if audio_only:
        fmt_args = ["--format", "bestaudio/best"]
    else:
        # Prefer a single-file mp4 that doesn't require ffmpeg to merge streams.
        fmt_args = ["--format", "best[ext=mp4]/best"]

    deno_path = os.path.expanduser("~/.deno/bin/deno")
    deno_arg  = f"deno:{deno_path}" if os.path.exists(deno_path) else "deno"

    cmd = [
        ytdlp,
        "--dateafter", date_str,
        "--break-match-filters", f"upload_date>={date_str}",
        # Playlist is newest-first; without a hard cap, yt-dlp fully extracts
        # every video in the playlist's history just to check its date. If
        # the session gets rate-limited mid-walk, each extraction fails with
        # an error rather than a clean filter rejection, so
        # break-match-filters never fires either — this bounds the number
        # of videos attempted regardless of success or failure.
        "--playlist-end", "20",
        "--sleep-requests", "0.75",
        "--sleep-interval", "10",
        "--max-sleep-interval", "20",
        "--cookies-from-browser", "firefox",
        "--js-runtimes", deno_arg,
        "--remote-components", "ejs:github",  # download challenge solver on first run
        *fmt_args,
        "--output", out_tmpl,
        "--restrict-filenames",
        "--write-description",
        "--write-info-json",
    ]

    if dry_run:
        cmd += ["--simulate", "--print", "%(upload_date)s  %(title)s  [%(id)s]"]
        print(f"DRY RUN — videos uploaded after {cutoff}:")
        print(f"  {CHANNEL_URL}\n")
    else:
        ensure_dir(output_dir)
        print(f"Downloading videos uploaded after {cutoff}")
        print(f"Output: {output_dir}\n")

    cmd.append(CHANNEL_URL)

    try:
        result = subprocess.run(cmd, timeout=3600)
    except subprocess.TimeoutExpired:
        print("ERROR: yt-dlp timed out — partial file(s) kept, will resume next run", file=sys.stderr)
        return 1

    if not dry_run and result.returncode == 0:
        log_path = os.path.join(output_dir, "download-log.txt")
        with open(log_path, "a") as f:
            ts = datetime.datetime.now().isoformat()
            f.write(f"{ts}  ran --dateafter {date_str}  exit={result.returncode}\n")
        print(f"\nLog: {log_path}")

    return result.returncode

# ── Main ───────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description=(
            "Download Ridgefield Board of Education meeting videos from YouTube "
            "(RPS BOE channel) uploaded within a recent date window."
        )
    )
    parser.add_argument(
        "--days", type=int, default=DAYS_BACK, metavar="N",
        help=f"Download videos uploaded in the last N days (default: {DAYS_BACK})"
    )
    parser.add_argument(
        "--output-dir", default=OUTPUT_DIR, metavar="DIR",
        help=f"Directory to save videos (default: {OUTPUT_DIR})"
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="List matching videos without downloading"
    )
    parser.add_argument(
        "--audio-only", action="store_true",
        help="Download audio as MP3 instead of full video"
    )
    args = parser.parse_args()

    now = datetime.datetime.now()
    if (now.weekday() == 5 and now.hour >= 18) or (now.weekday() == 6 and now.hour < 12):  # Saturday night, Sunday morning
        print("Skipping — no downloads on Saturday nights or Sunday mornings.")
        sys.exit(0)

    today  = datetime.date.today()
    cutoff = today - datetime.timedelta(days=args.days)

    print(f"Playlist: {CHANNEL_URL}")
    print(f"Since   : {cutoff}  ({args.days} days back from {today})")
    if args.dry_run:
        print("Mode    : DRY RUN (no files written)")
    print()

    rc = download_videos(cutoff, args.output_dir, args.dry_run, args.audio_only)
    sys.exit(rc)


if __name__ == "__main__":
    main()


# ── Tips ───────────────────────────────────────────────────────────────────────
#
# Preview what would be downloaded (no files written):
#   python3 scripts/download-ridgefield-boe-meetings.py --dry-run
#
# Download the last 15 days (default):
#   python3 scripts/download-ridgefield-boe-meetings.py
#
# Download the last 30 days:
#   python3 scripts/download-ridgefield-boe-meetings.py --days 30
#
# Audio only (smaller files, faster):
#   python3 scripts/download-ridgefield-boe-meetings.py --audio-only
#
# Save to a custom directory:
#   python3 scripts/download-ridgefield-boe-meetings.py --output-dir ~/Downloads/ridgefield-boe
#
# Run daily via cron (7 AM, last 15 days):
#   0 7 * * * cd /path/to/repo && python3 scripts/download-ridgefield-boe-meetings.py
#
# Then process with Claude:
#   python3 scripts/download-ridgefield-boe-meetings.py && \
#   bash scripts/batch-process.sh beat-archive/ridgefield-boe-meetings/
