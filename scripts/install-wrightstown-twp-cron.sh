#!/usr/bin/env bash
# One-shot script: installs the Wrightstown Township nightly cron entry, then removes itself.
# Scheduled to run Monday June 22 at 08:45.

CRON_LINE="32 20 * * 0-5 bash -c 'set -a; source \$HOME/.config/newtown-mail.env; set +a; cd \"/home/richkirby/SpiderOak Hive/Code/GitHubProjects/Clinton-Claude\" && /usr/bin/python3 scripts/send-wrightstown-twp-docs.py >> beat-archive/wrightstown-twp-agendas/cron-\$(date +\\%Y-\\%m-\\%d).log 2>&1'"

(crontab -l | grep -v "install-wrightstown-twp-cron.sh"; echo "$CRON_LINE") | crontab -
