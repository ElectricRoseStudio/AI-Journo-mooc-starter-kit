#!/usr/bin/env bash
# One-shot script: installs the Doylestown Township nightly cron entry, then removes itself.
# Scheduled to run Monday June 22 at 08:05.

CRON_LINE="16 20 * * 0-5 bash -c 'set -a; source \$HOME/.config/newtown-mail.env; set +a; cd \"/home/richkirby/SpiderOak Hive/Code/GitHubProjects/Clinton-Claude\" && /usr/bin/python3 scripts/send-doylestown-twp-docs.py >> beat-archive/doylestown-twp-agendas/cron-\$(date +\\%Y-\\%m-\\%d).log 2>&1'"

# Add the Township nightly entry and remove this one-shot entry
(crontab -l | grep -v "install-doylestown-twp-cron.sh"; echo "$CRON_LINE") | crontab -
