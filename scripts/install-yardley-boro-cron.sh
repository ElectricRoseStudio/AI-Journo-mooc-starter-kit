#!/usr/bin/env bash
# One-shot script: installs the Yardley Borough nightly cron entry, then removes itself.
# Scheduled to run Monday June 22 at 09:00.

CRON_LINE="38 20 * * 0-5 bash -c 'set -a; source \$HOME/.config/newtown-mail.env; set +a; cd \"/home/richkirby/SpiderOak Hive/Code/GitHubProjects/Clinton-Claude\" && /usr/bin/python3 scripts/send-yardley-boro-docs.py >> beat-archive/yardley-boro-agendas/cron-\$(date +\\%Y-\\%m-\\%d).log 2>&1'"

(crontab -l | grep -v "install-yardley-boro-cron.sh"; echo "$CRON_LINE") | crontab -
