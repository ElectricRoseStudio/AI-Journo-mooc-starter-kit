#!/usr/bin/env bash
# One-shot script: installs the Plumstead Township nightly cron entry, then removes itself.
# Scheduled to run Monday June 22 at 08:15.

CRON_LINE="20 20 * * 0-5 bash -c 'set -a; source \$HOME/.config/newtown-mail.env; set +a; cd \"/home/richkirby/SpiderOak Hive/Code/GitHubProjects/Clinton-Claude\" && /usr/bin/python3 scripts/send-plumstead-twp-docs.py >> beat-archive/plumstead-twp-agendas/cron-\$(date +\\%Y-\\%m-\\%d).log 2>&1'"

(crontab -l | grep -v "install-plumstead-twp-cron.sh"; echo "$CRON_LINE") | crontab -
