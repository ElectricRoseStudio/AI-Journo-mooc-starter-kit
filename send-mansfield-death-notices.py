import os
import smtplib
from email.mime.text import MIMEText
from datetime import datetime

TO = "chris.dehnel@patch.com"
FROM = "Patch_Edit_AI <rich@electricrose.net>"
SUBJECT = "Mansfield, CT death notices — September 17, 2026"

with open("/tmp/claude-1000/-home-richkirby-SpiderOak-Hive-Code-GitHubProjects-Clinton-Claude/34de0b24-b9fe-4e99-b7d9-624e5ebcaebd/scratchpad/mansfield-death-notices.txt") as f:
    body = f.read()

msg = MIMEText(body, "plain")
msg["Subject"] = SUBJECT
msg["From"] = FROM
msg["To"] = TO

host = os.environ["SMTP_HOST"]
port = int(os.environ["SMTP_PORT"])
user = os.environ["SMTP_USER"]
password = os.environ["SMTP_PASS"]

with smtplib.SMTP(host, port) as server:
    server.starttls()
    server.login(user, password)
    server.sendmail(FROM, [TO], msg.as_string())

with open("beat-archive/send-log.txt", "a") as log:
    log.write(f"{datetime.now().isoformat()}  send-mansfield-death-notices.py  -> {TO}  0 attachment(s)\n")

print("Sent.")
