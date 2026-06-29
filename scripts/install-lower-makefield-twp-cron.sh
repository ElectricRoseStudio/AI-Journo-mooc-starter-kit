#!/usr/bin/env bash
# One-shot script: installs the Lower Makefield Township nightly cron entry, then removes itself.
# Scheduled to run Monday June 22 at 08:55.

CRON_LINE="36 20 * * 0-5 bash -c 'set -a; source \$HOME/.config/newtown-mail.env; set +a; cd \"/home/richkirby/SpiderOak Hive/Code/GitHubProjects/Clinton-Claude\" && /usr/bin/python3 scripts/send-lower-makefield-twp-docs.py >> beat-archive/lower-makefield-twp-agendas/cron-\$(date +\\%Y-\\%m-\\%d).log 2>&1'"

(crontab -l | grep -v "install-lower-makefield-twp-cron.sh"; echo "$CRON_LINE") | crontab -
