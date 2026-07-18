#!/usr/bin/env python3
# send-northampton-twp-docs.py
# Run the Northampton Township PA agenda downloader and email all new files.
#
# CONFIGURATION:
#   Source ~/.config/newtown-mail.env before running, or export:
#     export SMTP_HOST=smtp.sendgrid.net
#     export SMTP_PORT=587
#     export SMTP_USER=apikey
#     export SMTP_PASS=<sendgrid-key>
#     export SMTP_FROM=rich@electricrose.net

import datetime
import email.mime.application
import email.mime.multipart
import email.mime.text
import os
import smtplib
import subprocess
import sys
import urllib.parse

SMTP_HOST = os.environ.get("SMTP_HOST", "")
SMTP_PORT = int(os.environ.get("SMTP_PORT", "587"))
SMTP_USER = os.environ.get("SMTP_USER", "")
SMTP_PASS = os.environ.get("SMTP_PASS", "")

FROM_ADDRESS = os.environ.get("SMTP_FROM", "rich@electricrose.net")
TO_ADDRESS   = "jeff.werner@patch.com"

REPO_DIR   = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LOG_PATH   = os.path.join(REPO_DIR, "beat-archive", "send-log.txt")
SCRIPT     = os.path.join(REPO_DIR, "scripts", "download-northampton-twp-agendas.py")
OUTPUT_DIR = os.path.join(REPO_DIR, "beat-archive/northampton-twp-agendas")
TOWN_NAME  = "Northampton Township, PA"

ATTACH_EXTENSIONS = {".pdf", ".mp4", ".mkv", ".webm", ".m4a", ".mp3", ".mov"}
MAX_ATTACH_BYTES  = 20 * 1024 * 1024
# SendGrid's message-size cap is 30 MB and base64 inflates attachments by
# ~37%, so the raw attachment total needs headroom well under that.
MAX_TOTAL_ATTACH_BYTES = 21 * 1024 * 1024

VIDEO_LINK_BASE_URL = os.environ.get("VIDEO_LINK_BASE_URL", "").rstrip("/")
BEAT_ARCHIVE_ROOT   = os.path.join(REPO_DIR, "beat-archive")


def file_url(fpath):
    """Link to fpath on the beat-archive file server, or None if unconfigured."""
    if not VIDEO_LINK_BASE_URL:
        return None
    rel = os.path.relpath(fpath, BEAT_ARCHIVE_ROOT)
    quoted = "/".join(urllib.parse.quote(part) for part in rel.split(os.sep))
    return f"{VIDEO_LINK_BASE_URL}/{quoted}"


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
    print("Running download-northampton-twp-agendas.py ...")
    result = subprocess.run(
        [sys.executable, SCRIPT],
        capture_output=True,
        text=True,
        cwd=REPO_DIR,
    )
    output = result.stdout + result.stderr
    print(output)
    return output


def collect_recent_files(hours=24):
    cutoff = datetime.datetime.now().timestamp() - hours * 3600
    found  = []
    for root, _, files in os.walk(OUTPUT_DIR):
        for fname in sorted(files):
            ext = os.path.splitext(fname)[1].lower()
            if ext not in ATTACH_EXTENSIONS:
                continue
            fpath = os.path.join(root, fname)
            if os.path.getmtime(fpath) >= cutoff:
                found.append(fpath)
    return found


def send_email(files, downloader_output):
    attached, skipped = [], []
    total_bytes = 0
    for fpath in files:
        size = os.path.getsize(fpath)
        if size > MAX_ATTACH_BYTES or total_bytes + size > MAX_TOTAL_ATTACH_BYTES:
            skipped.append(fpath)
        else:
            attached.append(fpath)
            total_bytes += size

    subject = (
        f"{TOWN_NAME} meeting docs — {datetime.date.today().strftime('%B %-d, %Y')} "
        f"({len(attached)} file{'s' if len(attached) != 1 else ''})"
    )

    msg = email.mime.multipart.MIMEMultipart()
    msg["From"]    = f"Patch_Edit_AI <{FROM_ADDRESS}>"
    msg["To"]      = TO_ADDRESS
    msg["Subject"] = subject

    skipped_note = ""
    if skipped:
        skipped_note = (
            f"\n{len(skipped)} file(s) exceeded the per-file {MAX_ATTACH_BYTES // (1024*1024)} MB "
            f"limit or the combined {MAX_TOTAL_ATTACH_BYTES // (1024*1024)} MB message budget "
            "and were not attached:\n"
            + "\n".join(
                f"  {os.path.basename(p)}  ({os.path.getsize(p) // (1024*1024)} MB)"
                + (f"\n    {file_url(p)}" if file_url(p) else "")
                for p in skipped
            )
            + "\n"
        )

    body = (
        f"{TOWN_NAME} agenda/minutes/video download — "
        f"{datetime.datetime.now().strftime('%Y-%m-%d %H:%M')}\n\n"
        f"{len(attached)} file(s) attached (new in past 24 hours).\n"
        + skipped_note
        + "\n--- Downloader log ---\n"
        + downloader_output
    )
    msg.attach(email.mime.text.MIMEText(body, "plain"))

    for fpath in attached:
        with open(fpath, "rb") as f:
            part = email.mime.application.MIMEApplication(
                f.read(), Name=os.path.basename(fpath)
            )
        part["Content-Disposition"] = f'attachment; filename="{os.path.basename(fpath)}"'
        msg.attach(part)

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

    print(f"Email sent to {TO_ADDRESS}  ({len(attached)} attachment(s), {len(skipped)} skipped)")
    write_send_log(TO_ADDRESS, len(attached))


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
    log   = run_downloader()
    files = collect_recent_files()
    if not files:
        print("No new files in the past 24 hours — sending summary email with no attachments.")
    send_email(files, log)


if __name__ == "__main__":
    main()
