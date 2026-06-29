#!/usr/bin/env bash
# One-shot script: installs the Middletown Township PA nightly cron entry, then removes itself.
# Scheduled to run Monday June 22 at 09:22.

CRON_LINE="48 20 * * 0-5 bash -c 'set -a; source \$HOME/.config/newtown-mail.env; set +a; cd \"/home/richkirby/SpiderOak Hive/Code/GitHubProjects/Clinton-Claude\" && /usr/bin/python3 scripts/send-middletown-twp-docs.py >> beat-archive/middletown-twp-agendas/cron-\$(date +\\%Y-\\%m-\\%d).log 2>&1'"

(crontab -l | grep -v "install-middletown-twp-cron.sh"; echo "$CRON_LINE") | crontab -
