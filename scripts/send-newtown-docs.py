#!/usr/bin/env python3
# send-newtown-docs.py
# Run the Newtown CT agenda downloader and email all collected files.
#
# USAGE:
#   python3 scripts/send-newtown-docs.py [--days N] [--output-dir DIR]
#
# CONFIGURATION:
#   Set the four SMTP_* variables below, or export them as environment
#   variables before running:
#
#     export SMTP_HOST=smtp.gmail.com
#     export SMTP_PORT=587
#     export SMTP_USER=you@gmail.com
#     export SMTP_PASS=your-app-password
#
# WHAT IT DOES:
#   1. Runs download-newtown-agendas.py and captures its output
#   2. Collects every PDF in beat-archive/newtown-agendas/ modified today
#   3. Sends them as email attachments to TO_ADDRESS

import datetime
import email.mime.application
import email.mime.multipart
import email.mime.text
import os
import json
import re
import smtplib
import subprocess
import sys
import urllib.parse
import urllib.request

# --- Configuration ---
SMTP_HOST = os.environ.get("SMTP_HOST", "")
SMTP_PORT = int(os.environ.get("SMTP_PORT", "587"))
SMTP_USER = os.environ.get("SMTP_USER", "")
SMTP_PASS = os.environ.get("SMTP_PASS", "")

FROM_ADDRESS = os.environ.get("SMTP_FROM", "rich@electricrose.net")
TO_ADDRESSES = ["rich.kirby@patch.com", "hayleigh.evans@patch.com"]

REPO_DIR   = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LOG_PATH   = os.path.join(REPO_DIR, "beat-archive", "send-log.txt")
SCRIPT     = os.path.join(REPO_DIR, "scripts", "download-newtown-agendas.py")
OUTPUT_DIR   = os.path.join(REPO_DIR, "beat-archive", "newtown-agendas")

CT_BIZ_API   = "https://data.ct.gov/resource/n7gp-d28j.json"
_NAICS_PAREN = re.compile(r"\s*\(\d+\)\s*$")


def check_config():
    missing = [v for v in ("SMTP_HOST", "SMTP_USER", "SMTP_PASS")
               if not os.environ.get(v) and not globals()[v]]
    if missing:
        print(
            f"ERROR: Missing SMTP configuration: {', '.join(missing)}\n"
            "Set them as environment variables before running:\n"
            "  export SMTP_HOST=smtp.example.com\n"
            "  export SMTP_USER=you@example.com\n"
            "  export SMTP_PASS=yourpassword",
            file=sys.stderr,
        )
        sys.exit(1)


def run_downloader():
    """Run the Newtown downloader and return its combined output as a string."""
    print("Running download-newtown-agendas.py ...")
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
    """Return list of PDF paths under OUTPUT_DIR added in the past N hours."""
    cutoff = datetime.datetime.now().timestamp() - hours * 3600
    found = []
    for root, _, files in os.walk(OUTPUT_DIR):
        for fname in sorted(files):
            if not fname.lower().endswith(".pdf"):
                continue
            fpath = os.path.join(root, fname)
            if os.path.getmtime(fpath) >= cutoff:
                found.append(fpath)
    return found


def fetch_newtown_businesses(days=7):
    """Return list of Newtown CT business registrations from the past N days."""
    today = datetime.date.today()
    cutoff = today - datetime.timedelta(days=days)
    where = (
        f"upper(billingcity)='NEWTOWN' "
        f"AND date_registration >= '{cutoff.isoformat()}T00:00:00' "
        f"AND date_registration <= '{today.isoformat()}T23:59:59'"
    )
    params = urllib.parse.urlencode({
        "$select": "date_registration,name,billingstreet,business_email_address,naics_code",
        "$where": where,
        "$limit": "500",
        "$order": "date_registration,name",
    })
    try:
        with urllib.request.urlopen(f"{CT_BIZ_API}?{params}", timeout=30) as resp:
            return json.loads(resp.read().decode())
    except Exception as e:
        print(f"WARNING: Could not fetch business registrations: {e}", file=sys.stderr)
        return []


def format_business_table(businesses):
    """Format CT Business Registry records as a plain-text table."""
    if not businesses:
        return "No new Newtown business registrations in the past 7 days.\n"

    rows = []
    for b in businesses:
        raw_date = b.get("date_registration", "")
        date = raw_date[:10]
        try:
            date = datetime.date.fromisoformat(date).strftime("%-m/%-d/%Y")
        except ValueError:
            pass
        naics = _NAICS_PAREN.sub("", b.get("naics_code") or "").strip() or "—"
        rows.append((
            date,
            b.get("name", ""),
            b.get("billingstreet", ""),
            b.get("business_email_address", ""),
            naics,
        ))

    headers = ("Date", "Business", "Address", "Email", "Type")
    widths = [max(len(r[i]) for r in rows + [headers]) for i in range(5)]
    sep = "  ".join("-" * w for w in widths)
    lines = ["  ".join(h.ljust(widths[i]) for i, h in enumerate(headers)), sep]
    for row in rows:
        lines.append("  ".join(cell.ljust(widths[i]) for i, cell in enumerate(row)))
    lines.append(f"\n{len(businesses)} business(es) registered in Newtown in the past 7 days.")
    return "\n".join(lines) + "\n"


def send_email(files, downloader_output, biz_table=""):
    subject = (
        f"Newtown CT meeting docs — {datetime.date.today().strftime('%B %-d, %Y')} "
        f"({len(files)} file{'s' if len(files) != 1 else ''})"
    )

    msg = email.mime.multipart.MIMEMultipart()
    msg["From"]    = f"Patch_Edit_AI <{FROM_ADDRESS}>"
    msg["To"]      = ", ".join(TO_ADDRESSES)
    msg["Subject"] = subject

    body = (
        f"Newtown CT agenda/minutes download — {datetime.datetime.now().strftime('%Y-%m-%d %H:%M')}\n\n"
        f"{len(files)} file(s) attached (new in past 24 hours).\n\n"
        "--- New Newtown business registrations (past 7 days) ---\n"
        + biz_table
        + "\n--- Downloader log ---\n"
        + downloader_output
    )
    msg.attach(email.mime.text.MIMEText(body, "plain"))

    for fpath in files:
        with open(fpath, "rb") as f:
            part = email.mime.application.MIMEApplication(f.read(), Name=os.path.basename(fpath))
        part["Content-Disposition"] = f'attachment; filename="{os.path.basename(fpath)}"'
        msg.attach(part)

    print(f"Connecting to {SMTP_HOST}:{SMTP_PORT} ...")
    if SMTP_PORT == 465:
        with smtplib.SMTP_SSL(SMTP_HOST, SMTP_PORT, timeout=30) as server:
            server.login(SMTP_USER, SMTP_PASS)
            server.sendmail(FROM_ADDRESS, TO_ADDRESSES, msg.as_string())
    else:
        with smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=30) as server:
            server.ehlo()
            server.starttls()
            server.login(SMTP_USER, SMTP_PASS)
            server.sendmail(FROM_ADDRESS, TO_ADDRESSES, msg.as_string())
    print(f"Email sent to {', '.join(TO_ADDRESSES)}  ({len(files)} attachment(s))")
    write_send_log(', '.join(TO_ADDRESSES), len(files))


def write_send_log(to, n_files):
    os.makedirs(os.path.dirname(LOG_PATH), exist_ok=True)
    with open(LOG_PATH, "a") as _lf:
        _lf.write(
            f"{datetime.datetime.now().isoformat()}  "
            f"{os.path.basename(__file__)}  "
            f"-> {to}  "
            f"{n_files} attachment(s)\n"
        )

def main():
    check_config()

    now = datetime.datetime.now()
    if (now.weekday() == 5 and now.hour >= 18) or (now.weekday() == 6 and now.hour < 12):  # Saturday night, Sunday morning
        print("Skipping — no sends on Saturday nights or Sunday mornings.")
        sys.exit(0)
    log = run_downloader()
    files = collect_recent_files()
    businesses = fetch_newtown_businesses(days=7)
    biz_table = format_business_table(businesses)

    if not files:
        print("No files downloaded in the past 24 hours — sending summary email with no attachments.")

    send_email(files, log, biz_table)


if __name__ == "__main__":
    main()
