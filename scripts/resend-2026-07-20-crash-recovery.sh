#!/bin/bash
# One-shot catch-up for the 2026-07-20 system crash.
#
# System crashed sometime after the 18:32 norwalk send and rebooted at
# 18:33:55. Cron kept firing on schedule the whole afternoon (17:00-18:36),
# but these 25 towns' send scripts produced 0-byte cron logs and no entry
# in beat-archive/send-log.txt, meaning they never reached the SMTP send
# step. The next scheduled job after the reboot (derby, 18:40) completed
# normally, so the environment is confirmed healthy again.
#
# Safe to re-run: each town is skipped if today's cron log already shows
# "Email sent", so this won't double-send anything that actually got
# through.

set -u
cd "/home/richkirby/SpiderOak Hive/Code/GitHubProjects/Clinton-Claude"
set -a
source "$HOME/.config/newtown-mail.env"
set +a

TOWNS=(
  bridgeport new-london east-granby stonington avon suffield bloomfield
  canton farmington hartford east-hartford simsbury southington hamden
  redding brookfield monroe easton bethel southbury stratford naugatuck
  new-canaan norwalk shelton
)

SENT=()
SKIPPED=()
FAILED=()
STOPPED_EARLY=0

for t in "${TOWNS[@]}"; do
  cron_log="beat-archive/${t}-agendas/cron-2026-07-20.log"
  retry_log="beat-archive/${t}-agendas/cron-2026-07-20-manual-retry.log"

  if [ -f "$cron_log" ] && grep -q "Email sent" "$cron_log"; then
    echo "[$t] already sent today — skipping"
    SKIPPED+=("$t")
    continue
  fi

  echo "[$t] sending ..."
  echo "--- $(date -Iseconds) manual retry: $t ---" >> "$retry_log"
  timeout 180 /usr/bin/python3 "scripts/send-${t}-docs.py" >> "$retry_log" 2>&1
  rc=$?

  if grep -q "Email sent" "$retry_log"; then
    echo "[$t] sent"
    SENT+=("$t")
  elif grep -q "Maximum credits exceeded" "$retry_log"; then
    echo "[$t] SendGrid credits exhausted — stopping batch."
    FAILED+=("$t")
    STOPPED_EARLY=1
    break
  else
    echo "[$t] failed for a different reason (exit $rc) — see $retry_log"
    FAILED+=("$t")
  fi
done

echo
echo "=== summary ==="
echo "sent (${#SENT[@]}): ${SENT[*]:-none}"
echo "skipped, already sent (${#SKIPPED[@]}): ${SKIPPED[*]:-none}"
echo "failed (${#FAILED[@]}): ${FAILED[*]:-none}"
if [ "$STOPPED_EARLY" -eq 1 ]; then
  echo
  echo "Stopped early — SendGrid is still rejecting logins. Re-run this script"
  echo "once credits are confirmed restored; already-sent towns will be skipped."
fi
