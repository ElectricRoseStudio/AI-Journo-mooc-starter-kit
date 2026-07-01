#!/usr/bin/env python3
# send-ridgefield-boe-docs.py
# Run the Ridgefield Board of Education meeting-video downloader and email a
# summary (videos are noted, not attached — they're too large for SMTP).
#
# CONFIGURATION:
#   Source ~/.config/newtown-mail.env before running, or export:
#     export SMTP_HOST=smtp.sendgrid.net
#     export SMTP_PORT=587
#     export SMTP_USER=apikey
#     export SMTP_PASS=<sendgrid-key>
#     export SMTP_FROM=rich@electricrose.net
#
# NOTE: The CT Business Registry table is attached by send-ridgefield-docs.py
# only, to avoid emailing the same table three times a day for one town.

import datetime
import email.mime.multipart
import email.mime.text
import os
import smtplib
import subprocess
import sys

SMTP_HOST = os.environ.get("SMTP_HOST", "")
SMTP_PORT = int(os.environ.get("SMTP_PORT", "587"))
SMTP_USER = os.environ.get("SMTP_USER", "")
SMTP_PASS = os.environ.get("SMTP_PASS", "")

FROM_ADDRESS = os.environ.get("SMTP_FROM", "rich@electricrose.net")
TO_ADDRESS   = "rich.kirby@patch.com"

REPO_DIR   = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LOG_PATH   = os.path.join(REPO_DIR, "beat-archive", "send-log.txt")
SCRIPT     = os.path.join(REPO_DIR, "scripts", "download-ridgefield-boe-meetings.py")
OUTPUT_DIR = os.path.join(REPO_DIR, "beat-archive", "ridgefield-boe-meetings")
TOWN_NAME  = "Ridgefield Board of Education, CT"

VIDEO_EXTENSIONS = {".mp4", ".webm", ".mkv"}


def check_config():
    missing = [v for v in ("SMTP_HOST", "SMTP_USER", "SMTP_PASS")
               if not os.environ.get(v) and not globals()[v]]
    if missing:
        print(
            f"ERROR: Missing SMTP configuration: {', '.join(missing)}\n"
            "Set them as environment variables before running:\n"
            "  export SMTP_HOST=smtp.sendgrid.net\n"
            "  export SMTP_USER=apikey\n"
            "  export SMTP_PASS=<sendgrid-key>",
            file=sys.stderr,
        )
        sys.exit(1)


def run_downloader():
    print("Running download-ridgefield-boe-meetings.py ...")
    result = subprocess.run(
        [sys.executable, SCRIPT],
        capture_output=True,
        text=True,
        cwd=REPO_DIR,
    )
    output = result.stdout + result.stderr
    print(output)
    return output


def collect_recent_videos(hours=24):
    """Return list of video files under OUTPUT_DIR modified in the past N hours."""
    cutoff = datetime.datetime.now().timestamp() - hours * 3600
    found  = []
    for root, _, files in os.walk(OUTPUT_DIR):
        for fname in sorted(files):
            ext = os.path.splitext(fname)[1].lower()
            if ext not in VIDEO_EXTENSIONS:
                continue
            fpath = os.path.join(root, fname)
            if os.path.getmtime(fpath) >= cutoff:
                found.append(fpath)
    return found


def send_email(video_files, downloader_output):
    n_vids = len(video_files)
    subject = (
        f"{TOWN_NAME} meeting videos — {datetime.date.today().strftime('%B %-d, %Y')} "
        f"({n_vids} video{'s' if n_vids != 1 else ''})"
    )

    msg = email.mime.multipart.MIMEMultipart()
    msg["From"]    = f"Patch_Edit_AI <{FROM_ADDRESS}>"
    msg["To"]      = TO_ADDRESS
    msg["Subject"] = subject

    video_note = ""
    if video_files:
        video_note = (
            "\n--- Video recordings (stored locally, not attached) ---\n"
            + "\n".join(f"  {os.path.basename(p)}  ({os.path.getsize(p) // (1024*1024)} MB)" for p in video_files)
            + "\nSource URLs appear in the downloader log below.\n"
        )

    body = (
        f"{TOWN_NAME} meeting video download — "
        f"{datetime.datetime.now().strftime('%Y-%m-%d %H:%M')}\n\n"
        f"{n_vids} new video(s) in the past 24 hours.\n"
        + video_note
        + "\n--- Downloader log ---\n"
        + downloader_output
    )
    msg.attach(email.mime.text.MIMEText(body, "plain"))

    print(f"Connecting to {SMTP_HOST}:{SMTP_PORT} ...")
    if SMTP_PORT == 465:
        with smtplib.SMTP_SSL(SMTP_HOST, SMTP_PORT, timeout=30) as server:
            server.login(SMTP_USER, SMTP_PASS)
            server.sendmail(FROM_ADDRESS, TO_ADDRESS, msg.as_string())
    else:
        with smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=30) as server:
            server.ehlo()
            server.starttls()
            server.login(SMTP_USER, SMTP_PASS)
            server.sendmail(FROM_ADDRESS, TO_ADDRESS, msg.as_string())

    print(f"Email sent to {TO_ADDRESS}  ({n_vids} video(s) noted)")
    write_send_log(TO_ADDRESS, n_vids)


def write_send_log(to, n_files):
    os.makedirs(os.path.dirname(LOG_PATH), exist_ok=True)
    with open(LOG_PATH, "a") as lf:
        lf.write(
            f"{datetime.datetime.now().isoformat()}  "
            f"{os.path.basename(__file__)}  "
            f"-> {to}  "
            f"{n_files} attachment(s)\n"
        )


def main():
    check_config()

    now = datetime.datetime.now()
    if (now.weekday() == 5 and now.hour >= 18) or (now.weekday() == 6 and now.hour < 12):
        print("Skipping — no sends on Saturday nights or Sunday mornings.")
        sys.exit(0)

    log = run_downloader()
    video_files = collect_recent_videos()

    if not video_files:
        print("No new videos in the past 24 hours — sending summary email with no attachments.")

    send_email(video_files, log)


if __name__ == "__main__":
    main()
