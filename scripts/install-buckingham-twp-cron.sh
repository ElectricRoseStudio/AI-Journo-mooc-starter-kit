#!/usr/bin/env bash
# One-shot script: installs the Buckingham Township nightly cron entry, then removes itself.
# Scheduled to run Monday June 22 at 08:10.

CRON_LINE="18 20 * * 0-5 bash -c 'set -a; source \$HOME/.config/newtown-mail.env; set +a; cd \"/home/richkirby/SpiderOak Hive/Code/GitHubProjects/Clinton-Claude\" && /usr/bin/python3 scripts/send-buckingham-twp-docs.py >> beat-archive/buckingham-twp-agendas/cron-\$(date +\\%Y-\\%m-\\%d).log 2>&1'"

(crontab -l | grep -v "install-buckingham-twp-cron.sh"; echo "$CRON_LINE") | crontab -
