#!/usr/bin/env bash
# One-shot script: installs the New Hope Borough nightly cron entry, then removes itself.
# Scheduled to run Monday June 22 at 08:25.

CRON_LINE="24 20 * * 0-5 bash -c 'set -a; source \$HOME/.config/newtown-mail.env; set +a; cd \"/home/richkirby/SpiderOak Hive/Code/GitHubProjects/Clinton-Claude\" && /usr/bin/python3 scripts/send-new-hope-docs.py >> beat-archive/new-hope-agendas/cron-\$(date +\\%Y-\\%m-\\%d).log 2>&1'"

(crontab -l | grep -v "install-new-hope-cron.sh"; echo "$CRON_LINE") | crontab -
