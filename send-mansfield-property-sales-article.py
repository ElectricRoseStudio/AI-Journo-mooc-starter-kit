import os
import smtplib
from email.mime.text import MIMEText
from datetime import datetime

FROM = "Patch_Edit_AI <rich@electricrose.net>"
SUBJECT = "Mansfield, CT recent property sales — draft article"
RECIPIENTS = ["chris.dehnel@patch.com", "rich.kirby@patch.com"]

with open("/tmp/claude-1000/-home-richkirby-SpiderOak-Hive-Code-GitHubProjects-Clinton-Claude/34de0b24-b9fe-4e99-b7d9-624e5ebcaebd/scratchpad/mansfield-property-sales-article.html") as f:
    html_body = f.read()

host = os.environ["SMTP_HOST"]
port = int(os.environ["SMTP_PORT"])
user = os.environ["SMTP_USER"]
password = os.environ["SMTP_PASS"]

with smtplib.SMTP(host, port) as server:
    server.starttls()
    server.login(user, password)
    for to in RECIPIENTS:
        msg = MIMEText(html_body, "html")
        msg["Subject"] = SUBJECT
        msg["From"] = FROM
        msg["To"] = to
        server.sendmail(FROM, [to], msg.as_string())

with open("beat-archive/send-log.txt", "a") as log:
    for to in RECIPIENTS:
        log.write(f"{datetime.now().isoformat()}  send-mansfield-property-sales-article.py  -> {to}  0 attachment(s)\n")

print("Sent: Mansfield")
