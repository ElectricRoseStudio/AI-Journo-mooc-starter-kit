#!/usr/bin/env bash
# One-shot script: installs the Upper Makefield Township nightly cron entry, then removes itself.
# Scheduled to run Monday June 22 at 08:40.

CRON_LINE="30 20 * * 0-5 bash -c 'set -a; source \$HOME/.config/newtown-mail.env; set +a; cd \"/home/richkirby/SpiderOak Hive/Code/GitHubProjects/Clinton-Claude\" && /usr/bin/python3 scripts/send-upper-makefield-twp-docs.py >> beat-archive/upper-makefield-twp-agendas/cron-\$(date +\\%Y-\\%m-\\%d).log 2>&1'"

(crontab -l | grep -v "install-upper-makefield-twp-cron.sh"; echo "$CRON_LINE") | crontab -
