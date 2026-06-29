#!/usr/bin/env bash
# One-shot script: installs the Bristol Borough nightly cron entry, then removes itself.
# Scheduled to run Monday June 22 at 09:15.

CRON_LINE="44 20 * * 0-5 bash -c 'set -a; source \$HOME/.config/newtown-mail.env; set +a; cd \"/home/richkirby/SpiderOak Hive/Code/GitHubProjects/Clinton-Claude\" && /usr/bin/python3 scripts/send-bristol-boro-docs.py >> beat-archive/bristol-boro-agendas/cron-\$(date +\\%Y-\\%m-\\%d).log 2>&1'"

(crontab -l | grep -v "install-bristol-boro-cron.sh"; echo "$CRON_LINE") | crontab -
