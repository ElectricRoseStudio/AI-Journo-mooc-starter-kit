#!/usr/bin/env bash
# One-shot script: installs the Solebury Township nightly cron entry, then removes itself.
# Scheduled to run Monday June 22 at 08:20.

CRON_LINE="22 20 * * 0-5 bash -c 'set -a; source \$HOME/.config/newtown-mail.env; set +a; cd \"/home/richkirby/SpiderOak Hive/Code/GitHubProjects/Clinton-Claude\" && /usr/bin/python3 scripts/send-solebury-twp-docs.py >> beat-archive/solebury-twp-agendas/cron-\$(date +\\%Y-\\%m-\\%d).log 2>&1'"

(crontab -l | grep -v "install-solebury-twp-cron.sh"; echo "$CRON_LINE") | crontab -
