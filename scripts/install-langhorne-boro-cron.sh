#!/usr/bin/env bash
# One-shot script: installs the Langhorne Borough nightly cron entry, then removes itself.
# Scheduled to run Monday June 22 at 09:20.

CRON_LINE="46 20 * * 0-5 bash -c 'set -a; source \$HOME/.config/newtown-mail.env; set +a; cd \"/home/richkirby/SpiderOak Hive/Code/GitHubProjects/Clinton-Claude\" && /usr/bin/python3 scripts/send-langhorne-boro-docs.py >> beat-archive/langhorne-boro-agendas/cron-\$(date +\\%Y-\\%m-\\%d).log 2>&1'"

(crontab -l | grep -v "install-langhorne-boro-cron.sh"; echo "$CRON_LINE") | crontab -
