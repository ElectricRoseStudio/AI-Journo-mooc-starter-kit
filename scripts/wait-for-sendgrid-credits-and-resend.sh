#!/bin/bash
# Poller for the 2026-07-17 SendGrid free-plan credit outage.
#
# Installed as a cron job running every 15 minutes. Checks SendGrid's
# actual credit balance via the API (not a guessed reset time, since
# SendGrid didn't reset at UTC midnight as expected). Once credits are
# available again, runs resend-2026-07-17-tier2-failures.sh exactly
# once, then removes its own crontab entry so it doesn't keep firing.
#
# Crontab line is tagged with the marker RESEND_CREDIT_CHECK for
# self-removal — do not remove that tag if editing the line manually.

set -u
cd "/home/richkirby/SpiderOak Hive/Code/GitHubProjects/Clinton-Claude"
set -a
source "$HOME/.config/newtown-mail.env"
set +a

LOCK="/tmp/sendgrid-credit-check.lock"
exec 9>"$LOCK"
flock -n 9 || exit 0

LOG="beat-archive/resend-check-log.txt"

CREDITS_JSON=$(curl -s "https://api.sendgrid.com/v3/user/credits" -H "Authorization: Bearer $SMTP_PASS")
USED=$(echo "$CREDITS_JSON" | python3 -c "import json,sys; print(json.load(sys.stdin)['used'])" 2>/dev/null)
TOTAL=$(echo "$CREDITS_JSON" | python3 -c "import json,sys; print(json.load(sys.stdin)['total'])" 2>/dev/null)

if [ -z "$USED" ] || [ -z "$TOTAL" ]; then
  echo "$(date -Iseconds)  could not parse credits response: $CREDITS_JSON" >> "$LOG"
  exit 0
fi

if [ "$USED" -lt "$TOTAL" ]; then
  echo "$(date -Iseconds)  credits available ($USED/$TOTAL used) - running resend" >> "$LOG"
  bash scripts/resend-2026-07-17-tier2-failures.sh >> "$LOG" 2>&1
  echo "$(date -Iseconds)  resend script finished - removing self from crontab" >> "$LOG"
  crontab -l | grep -v "RESEND_CREDIT_CHECK" | crontab -
else
  echo "$(date -Iseconds)  still exhausted ($USED/$TOTAL used)" >> "$LOG"
fi
