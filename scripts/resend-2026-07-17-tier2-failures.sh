#!/bin/bash
# One-shot catch-up for the 2026-07-17 tier-2 SendGrid outage.
#
# Tonight's tier-2 batch (20:00 run) downloaded all 48 towns fine, but
# SendGrid started rejecting logins with "451 Maximum credits exceeded"
# partway through, right after west-haven sent. These 26 towns downloaded
# successfully but never sent. Re-run this once the SendGrid account has
# credits again.
#
# Safe to re-run: each town is skipped if today's cron log already shows
# "Email sent", so this won't double-send anything that got through on
# an earlier pass.

set -u
cd "/home/richkirby/SpiderOak Hive/Code/GitHubProjects/Clinton-Claude"
set -a
source "$HOME/.config/newtown-mail.env"
set +a

TOWNS=(
  trumbull greenwich stamford guilford darien mansfield middletown
  old-lyme east-lyme waterford middletown-twp doylestown-twp
  buckingham-twp solebury-twp new-hope newtown-twp northampton-twp
  lower-makefield-twp yardley-boro falls-twp bristol-twp bristol-boro
  bensalem warminster ridgefield
)

SENT=()
SKIPPED=()
FAILED=()
STOPPED_EARLY=0

for t in "${TOWNS[@]}"; do
  cron_log="beat-archive/${t}-agendas/cron-2026-07-17.log"
  retry_log="beat-archive/${t}-agendas/cron-2026-07-17-manual-retry.log"

  if [ -f "$cron_log" ] && grep -q "Email sent" "$cron_log"; then
    echo "[$t] already sent tonight — skipping"
    SKIPPED+=("$t")
    continue
  fi

  echo "[$t] sending ..."
  echo "--- $(date -Iseconds) manual retry: $t ---" >> "$retry_log"
  timeout 3900 /usr/bin/python3 "scripts/send-${t}-docs.py" >> "$retry_log" 2>&1
  rc=$?

  if grep -q "Email sent" "$retry_log"; then
    echo "[$t] sent"
    SENT+=("$t")
  elif grep -q "Maximum credits exceeded" "$retry_log"; then
    echo "[$t] still failing — SendGrid credits still exhausted. Stopping batch."
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
