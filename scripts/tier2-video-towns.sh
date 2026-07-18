#!/bin/bash
set -u
cd "/home/richkirby/SpiderOak Hive/Code/GitHubProjects/Clinton-Claude"
set -a
source "$HOME/.config/newtown-mail.env"
set +a
LOCK="/tmp/tier2-video-towns.lock"
exec 9>"$LOCK"
if ! flock -n 9; then
  echo "$(date -Iseconds)  tier2 already running (previous run not finished) - skipping" >> beat-archive/tier2-run-log.txt
  exit 0
fi

echo "--- $(date -Iseconds) tier2: westport (cap 3900s) ---" >> beat-archive/westport-agendas/cron-$(date +\%Y-\%m-\%d).log 2>&1
timeout 3900 /usr/bin/python3 scripts/send-westport-docs.py >> beat-archive/westport-agendas/cron-$(date +\%Y-\%m-\%d).log 2>&1
echo "--- $(date -Iseconds) tier2: east-windsor (cap 3900s) ---" >> beat-archive/east-windsor-agendas/cron-$(date +\%Y-\%m-\%d).log 2>&1
timeout 3900 /usr/bin/python3 scripts/send-east-windsor-docs.py >> beat-archive/east-windsor-agendas/cron-$(date +\%Y-\%m-\%d).log 2>&1
echo "--- $(date -Iseconds) tier2: madison (cap 3900s) ---" >> beat-archive/madison-agendas/cron-$(date +\%Y-\%m-\%d).log 2>&1
timeout 3900 /usr/bin/python3 scripts/send-madison-docs.py >> beat-archive/madison-agendas/cron-$(date +\%Y-\%m-\%d).log 2>&1
echo "--- $(date -Iseconds) tier2: ellington (cap 3900s) ---" >> beat-archive/ellington-agendas/cron-$(date +\%Y-\%m-\%d).log 2>&1
timeout 3900 /usr/bin/python3 scripts/send-ellington-docs.py >> beat-archive/ellington-agendas/cron-$(date +\%Y-\%m-\%d).log 2>&1
echo "--- $(date -Iseconds) tier2: cheshire (cap 3900s) ---" >> beat-archive/cheshire-agendas/cron-$(date +\%Y-\%m-\%d).log 2>&1
timeout 3900 /usr/bin/python3 scripts/send-cheshire-docs.py >> beat-archive/cheshire-agendas/cron-$(date +\%Y-\%m-\%d).log 2>&1
echo "--- $(date -Iseconds) tier2: fairfield (cap 3900s) ---" >> beat-archive/fairfield-agendas/cron-$(date +\%Y-\%m-\%d).log 2>&1
timeout 3900 /usr/bin/python3 scripts/send-fairfield-docs.py >> beat-archive/fairfield-agendas/cron-$(date +\%Y-\%m-\%d).log 2>&1
echo "--- $(date -Iseconds) tier2: somers (cap 3900s) ---" >> beat-archive/somers-agendas/cron-$(date +\%Y-\%m-\%d).log 2>&1
timeout 3900 /usr/bin/python3 scripts/send-somers-docs.py >> beat-archive/somers-agendas/cron-$(date +\%Y-\%m-\%d).log 2>&1
echo "--- $(date -Iseconds) tier2: stafford (cap 3900s) ---" >> beat-archive/stafford-agendas/cron-$(date +\%Y-\%m-\%d).log 2>&1
timeout 3900 /usr/bin/python3 scripts/send-stafford-docs.py >> beat-archive/stafford-agendas/cron-$(date +\%Y-\%m-\%d).log 2>&1
echo "--- $(date -Iseconds) tier2: willington (cap 3900s) ---" >> beat-archive/willington-agendas/cron-$(date +\%Y-\%m-\%d).log 2>&1
timeout 3900 /usr/bin/python3 scripts/send-willington-docs.py >> beat-archive/willington-agendas/cron-$(date +\%Y-\%m-\%d).log 2>&1
echo "--- $(date -Iseconds) tier2: tolland (cap 3900s) ---" >> beat-archive/tolland-agendas/cron-$(date +\%Y-\%m-\%d).log 2>&1
timeout 3900 /usr/bin/python3 scripts/send-tolland-docs.py >> beat-archive/tolland-agendas/cron-$(date +\%Y-\%m-\%d).log 2>&1
echo "--- $(date -Iseconds) tier2: vernon (cap 3900s) ---" >> beat-archive/vernon-agendas/cron-$(date +\%Y-\%m-\%d).log 2>&1
timeout 3900 /usr/bin/python3 scripts/send-vernon-docs.py >> beat-archive/vernon-agendas/cron-$(date +\%Y-\%m-\%d).log 2>&1
echo "--- $(date -Iseconds) tier2: groton (cap 3900s) ---" >> beat-archive/groton-agendas/cron-$(date +\%Y-\%m-\%d).log 2>&1
timeout 3900 /usr/bin/python3 scripts/send-groton-docs.py >> beat-archive/groton-agendas/cron-$(date +\%Y-\%m-\%d).log 2>&1
echo "--- $(date -Iseconds) tier2: oxford (cap 3900s) ---" >> beat-archive/oxford-agendas/cron-$(date +\%Y-\%m-\%d).log 2>&1
timeout 3900 /usr/bin/python3 scripts/send-oxford-docs.py >> beat-archive/oxford-agendas/cron-$(date +\%Y-\%m-\%d).log 2>&1
echo "--- $(date -Iseconds) tier2: milford (cap 3900s) ---" >> beat-archive/milford-agendas/cron-$(date +\%Y-\%m-\%d).log 2>&1
timeout 3900 /usr/bin/python3 scripts/send-milford-docs.py >> beat-archive/milford-agendas/cron-$(date +\%Y-\%m-\%d).log 2>&1
echo "--- $(date -Iseconds) tier2: granby (cap 3900s) ---" >> beat-archive/granby-agendas/cron-$(date +\%Y-\%m-\%d).log 2>&1
timeout 3900 /usr/bin/python3 scripts/send-granby-docs.py >> beat-archive/granby-agendas/cron-$(date +\%Y-\%m-\%d).log 2>&1
echo "--- $(date -Iseconds) tier2: berlin (cap 2100s) ---" >> beat-archive/berlin-agendas/cron-$(date +\%Y-\%m-\%d).log 2>&1
timeout 2100 /usr/bin/python3 scripts/send-berlin-docs.py >> beat-archive/berlin-agendas/cron-$(date +\%Y-\%m-\%d).log 2>&1
echo "--- $(date -Iseconds) tier2: windsor (cap 5400s) ---" >> beat-archive/windsor-agendas/cron-$(date +\%Y-\%m-\%d).log 2>&1
timeout 5400 /usr/bin/python3 scripts/send-windsor-docs.py >> beat-archive/windsor-agendas/cron-$(date +\%Y-\%m-\%d).log 2>&1
echo "--- $(date -Iseconds) tier2: windsor-locks (cap 3900s) ---" >> beat-archive/windsor-locks-agendas/cron-$(date +\%Y-\%m-\%d).log 2>&1
timeout 3900 /usr/bin/python3 scripts/send-windsor-locks-docs.py >> beat-archive/windsor-locks-agendas/cron-$(date +\%Y-\%m-\%d).log 2>&1
echo "--- $(date -Iseconds) tier2: west-hartford (cap 3900s) ---" >> beat-archive/west-hartford-agendas/cron-$(date +\%Y-\%m-\%d).log 2>&1
timeout 3900 /usr/bin/python3 scripts/send-west-hartford-docs.py >> beat-archive/west-hartford-agendas/cron-$(date +\%Y-\%m-\%d).log 2>&1
echo "--- $(date -Iseconds) tier2: meriden (cap 7500s) ---" >> beat-archive/meriden-agendas/cron-$(date +\%Y-\%m-\%d).log 2>&1
timeout 7500 /usr/bin/python3 scripts/send-meriden-docs.py >> beat-archive/meriden-agendas/cron-$(date +\%Y-\%m-\%d).log 2>&1
echo "--- $(date -Iseconds) tier2: north-haven (cap 7500s) ---" >> beat-archive/north-haven-agendas/cron-$(date +\%Y-\%m-\%d).log 2>&1
timeout 7500 /usr/bin/python3 scripts/send-north-haven-docs.py >> beat-archive/north-haven-agendas/cron-$(date +\%Y-\%m-\%d).log 2>&1
echo "--- $(date -Iseconds) tier2: wallingford (cap 7500s) ---" >> beat-archive/wallingford-agendas/cron-$(date +\%Y-\%m-\%d).log 2>&1
timeout 7500 /usr/bin/python3 scripts/send-wallingford-docs.py >> beat-archive/wallingford-agendas/cron-$(date +\%Y-\%m-\%d).log 2>&1
echo "--- $(date -Iseconds) tier2: west-haven (cap 7500s) ---" >> beat-archive/west-haven-agendas/cron-$(date +\%Y-\%m-\%d).log 2>&1
timeout 7500 /usr/bin/python3 scripts/send-west-haven-docs.py >> beat-archive/west-haven-agendas/cron-$(date +\%Y-\%m-\%d).log 2>&1
echo "--- $(date -Iseconds) tier2: trumbull (cap 3900s) ---" >> beat-archive/trumbull-agendas/cron-$(date +\%Y-\%m-\%d).log 2>&1
timeout 3900 /usr/bin/python3 scripts/send-trumbull-docs.py >> beat-archive/trumbull-agendas/cron-$(date +\%Y-\%m-\%d).log 2>&1
echo "--- $(date -Iseconds) tier2: greenwich (cap 3900s) ---" >> beat-archive/greenwich-agendas/cron-$(date +\%Y-\%m-\%d).log 2>&1
timeout 3900 /usr/bin/python3 scripts/send-greenwich-docs.py >> beat-archive/greenwich-agendas/cron-$(date +\%Y-\%m-\%d).log 2>&1
echo "--- $(date -Iseconds) tier2: stamford (cap 3900s) ---" >> beat-archive/stamford-agendas/cron-$(date +\%Y-\%m-\%d).log 2>&1
timeout 3900 /usr/bin/python3 scripts/send-stamford-docs.py >> beat-archive/stamford-agendas/cron-$(date +\%Y-\%m-\%d).log 2>&1
echo "--- $(date -Iseconds) tier2: guilford (cap 3900s) ---" >> beat-archive/guilford-agendas/cron-$(date +\%Y-\%m-\%d).log 2>&1
timeout 3900 /usr/bin/python3 scripts/send-guilford-docs.py >> beat-archive/guilford-agendas/cron-$(date +\%Y-\%m-\%d).log 2>&1
echo "--- $(date -Iseconds) tier2: darien (cap 3900s) ---" >> beat-archive/darien-agendas/cron-$(date +\%Y-\%m-\%d).log 2>&1
timeout 3900 /usr/bin/python3 scripts/send-darien-docs.py >> beat-archive/darien-agendas/cron-$(date +\%Y-\%m-\%d).log 2>&1
echo "--- $(date -Iseconds) tier2: mansfield (cap 3900s) ---" >> beat-archive/mansfield-agendas/cron-$(date +\%Y-\%m-\%d).log 2>&1
timeout 3900 /usr/bin/python3 scripts/send-mansfield-docs.py >> beat-archive/mansfield-agendas/cron-$(date +\%Y-\%m-\%d).log 2>&1
echo "--- $(date -Iseconds) tier2: middletown (cap 5400s) ---" >> beat-archive/middletown-agendas/cron-$(date +\%Y-\%m-\%d).log 2>&1
timeout 5400 /usr/bin/python3 scripts/send-middletown-docs.py >> beat-archive/middletown-agendas/cron-$(date +\%Y-\%m-\%d).log 2>&1
echo "--- $(date -Iseconds) tier2: old-lyme (cap 3900s) ---" >> beat-archive/old-lyme-agendas/cron-$(date +\%Y-\%m-\%d).log 2>&1
timeout 3900 /usr/bin/python3 scripts/send-old-lyme-docs.py >> beat-archive/old-lyme-agendas/cron-$(date +\%Y-\%m-\%d).log 2>&1
echo "--- $(date -Iseconds) tier2: east-lyme (cap 3900s) ---" >> beat-archive/east-lyme-agendas/cron-$(date +\%Y-\%m-\%d).log 2>&1
timeout 3900 /usr/bin/python3 scripts/send-east-lyme-docs.py >> beat-archive/east-lyme-agendas/cron-$(date +\%Y-\%m-\%d).log 2>&1
echo "--- $(date -Iseconds) tier2: waterford (cap 3900s) ---" >> beat-archive/waterford-agendas/cron-$(date +\%Y-\%m-\%d).log 2>&1
timeout 3900 /usr/bin/python3 scripts/send-waterford-docs.py >> beat-archive/waterford-agendas/cron-$(date +\%Y-\%m-\%d).log 2>&1
echo "--- $(date -Iseconds) tier2: middletown-twp (cap 900s) ---" >> beat-archive/middletown-twp-agendas/cron-$(date +\%Y-\%m-\%d).log 2>&1
timeout 900 /usr/bin/python3 scripts/send-middletown-twp-docs.py >> beat-archive/middletown-twp-agendas/cron-$(date +\%Y-\%m-\%d).log 2>&1
echo "--- $(date -Iseconds) tier2: doylestown-twp (cap 3900s) ---" >> beat-archive/doylestown-twp-agendas/cron-$(date +\%Y-\%m-\%d).log 2>&1
timeout 3900 /usr/bin/python3 scripts/send-doylestown-twp-docs.py >> beat-archive/doylestown-twp-agendas/cron-$(date +\%Y-\%m-\%d).log 2>&1
echo "--- $(date -Iseconds) tier2: buckingham-twp (cap 3900s) ---" >> beat-archive/buckingham-twp-agendas/cron-$(date +\%Y-\%m-\%d).log 2>&1
timeout 3900 /usr/bin/python3 scripts/send-buckingham-twp-docs.py >> beat-archive/buckingham-twp-agendas/cron-$(date +\%Y-\%m-\%d).log 2>&1
echo "--- $(date -Iseconds) tier2: solebury-twp (cap 3900s) ---" >> beat-archive/solebury-twp-agendas/cron-$(date +\%Y-\%m-\%d).log 2>&1
timeout 3900 /usr/bin/python3 scripts/send-solebury-twp-docs.py >> beat-archive/solebury-twp-agendas/cron-$(date +\%Y-\%m-\%d).log 2>&1
echo "--- $(date -Iseconds) tier2: new-hope (cap 3900s) ---" >> beat-archive/new-hope-agendas/cron-$(date +\%Y-\%m-\%d).log 2>&1
timeout 3900 /usr/bin/python3 scripts/send-new-hope-docs.py >> beat-archive/new-hope-agendas/cron-$(date +\%Y-\%m-\%d).log 2>&1
echo "--- $(date -Iseconds) tier2: newtown-twp (cap 3900s) ---" >> beat-archive/newtown-twp-agendas/cron-$(date +\%Y-\%m-\%d).log 2>&1
timeout 3900 /usr/bin/python3 scripts/send-newtown-twp-docs.py >> beat-archive/newtown-twp-agendas/cron-$(date +\%Y-\%m-\%d).log 2>&1
echo "--- $(date -Iseconds) tier2: northampton-twp (cap 3900s) ---" >> beat-archive/northampton-twp-agendas/cron-$(date +\%Y-\%m-\%d).log 2>&1
timeout 3900 /usr/bin/python3 scripts/send-northampton-twp-docs.py >> beat-archive/northampton-twp-agendas/cron-$(date +\%Y-\%m-\%d).log 2>&1
echo "--- $(date -Iseconds) tier2: lower-makefield-twp (cap 3900s) ---" >> beat-archive/lower-makefield-twp-agendas/cron-$(date +\%Y-\%m-\%d).log 2>&1
timeout 3900 /usr/bin/python3 scripts/send-lower-makefield-twp-docs.py >> beat-archive/lower-makefield-twp-agendas/cron-$(date +\%Y-\%m-\%d).log 2>&1
echo "--- $(date -Iseconds) tier2: yardley-boro (cap 3900s) ---" >> beat-archive/yardley-boro-agendas/cron-$(date +\%Y-\%m-\%d).log 2>&1
timeout 3900 /usr/bin/python3 scripts/send-yardley-boro-docs.py >> beat-archive/yardley-boro-agendas/cron-$(date +\%Y-\%m-\%d).log 2>&1
echo "--- $(date -Iseconds) tier2: falls-twp (cap 3900s) ---" >> beat-archive/falls-twp-agendas/cron-$(date +\%Y-\%m-\%d).log 2>&1
timeout 3900 /usr/bin/python3 scripts/send-falls-twp-docs.py >> beat-archive/falls-twp-agendas/cron-$(date +\%Y-\%m-\%d).log 2>&1
echo "--- $(date -Iseconds) tier2: bristol-twp (cap 3900s) ---" >> beat-archive/bristol-twp-agendas/cron-$(date +\%Y-\%m-\%d).log 2>&1
timeout 3900 /usr/bin/python3 scripts/send-bristol-twp-docs.py >> beat-archive/bristol-twp-agendas/cron-$(date +\%Y-\%m-\%d).log 2>&1
echo "--- $(date -Iseconds) tier2: bristol-boro (cap 3900s) ---" >> beat-archive/bristol-boro-agendas/cron-$(date +\%Y-\%m-\%d).log 2>&1
timeout 3900 /usr/bin/python3 scripts/send-bristol-boro-docs.py >> beat-archive/bristol-boro-agendas/cron-$(date +\%Y-\%m-\%d).log 2>&1
echo "--- $(date -Iseconds) tier2: bensalem (cap 3900s) ---" >> beat-archive/bensalem-agendas/cron-$(date +\%Y-\%m-\%d).log 2>&1
timeout 3900 /usr/bin/python3 scripts/send-bensalem-docs.py >> beat-archive/bensalem-agendas/cron-$(date +\%Y-\%m-\%d).log 2>&1
echo "--- $(date -Iseconds) tier2: warminster (cap 3900s) ---" >> beat-archive/warminster-agendas/cron-$(date +\%Y-\%m-\%d).log 2>&1
timeout 3900 /usr/bin/python3 scripts/send-warminster-docs.py >> beat-archive/warminster-agendas/cron-$(date +\%Y-\%m-\%d).log 2>&1
echo "--- $(date -Iseconds) tier2: ridgefield (cap 7500s) ---" >> beat-archive/ridgefield-agendas/cron-$(date +\%Y-\%m-\%d).log 2>&1
timeout 7500 /usr/bin/python3 scripts/send-ridgefield-docs.py >> beat-archive/ridgefield-agendas/cron-$(date +\%Y-\%m-\%d).log 2>&1

echo "$(date -Iseconds) tier2 run complete" >> beat-archive/tier2-run-log.txt
