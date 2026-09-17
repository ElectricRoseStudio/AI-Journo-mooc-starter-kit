import os
import smtplib
from email.mime.text import MIMEText
from datetime import datetime

FROM = "Patch_Edit_AI <rich@electricrose.net>"
SUBJECT = "Guilford, CT recent property sales — draft article"
RECIPIENTS = ["richard.kaufman@patch.com"]

with open("/tmp/claude-1000/-home-richkirby-SpiderOak-Hive-Code-GitHubProjects-Clinton-Claude/e522aed8-1072-4e1c-910c-4357d9d68ec6/scratchpad/guilford-property-sales-article.html") as f:
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
        log.write(f"{datetime.now().isoformat()}  send-guilford-property-sales-article.py  -> {to}  0 attachment(s)\n")

print("Sent: Guilford")
