#!/usr/bin/env bash
# One-shot script: installs the Newtown Borough nightly cron entry, then removes itself.
# Scheduled to run Monday June 22 at 08:35.

CRON_LINE="28 20 * * 0-5 bash -c 'set -a; source \$HOME/.config/newtown-mail.env; set +a; cd \"/home/richkirby/SpiderOak Hive/Code/GitHubProjects/Clinton-Claude\" && /usr/bin/python3 scripts/send-newtown-boro-docs.py >> beat-archive/newtown-boro-agendas/cron-\$(date +\\%Y-\\%m-\\%d).log 2>&1'"

(crontab -l | grep -v "install-newtown-boro-cron.sh"; echo "$CRON_LINE") | crontab -
