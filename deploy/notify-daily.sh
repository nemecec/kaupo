#!/usr/bin/env bash
# Daily trading summary to the ntfy topic. Runs from cron on the host.
set -euo pipefail

ENV_FILE=/etc/kaupo/kaupo.env
set -a
# shellcheck source=/dev/null
. "$ENV_FILE"
set +a

if [[ -z "${KAUPO_NTFY_TOPIC:-}" ]]; then
  echo "KAUPO_NTFY_TOPIC is not set; skipping"
  exit 0
fi

day=$(date -u -d 'yesterday' +%F)
report=$(curl -fsSL -m 15 -H "Authorization: Bearer $KAUPO_READONLY_TOKEN" \
  "https://kaupo.trade/api/v1/reports/daily?day=$day")
summary=$(echo "$report" | jq -r \
  '"Kaupo " + .period + ": runs \(.totals.num_runs) (\(.totals.active_runs) active), fills \(.totals.total_fills), P&L \(.totals.total_pnl) EUR, fees \(.totals.total_fees) EUR"')
curl -fsSL -m 10 -d "$summary" "https://ntfy.sh/$KAUPO_NTFY_TOPIC"
