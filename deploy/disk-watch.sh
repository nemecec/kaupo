#!/usr/bin/env bash
# Disk watchdog: ntfy when the root filesystem crosses 85% / 95%, and when it
# recovers. Alerts on level transitions only (a state file remembers the last
# level), so a full disk does not spam the topic every cron tick.
# Runs from cron on the host (see /etc/cron.d/kaupo-diskwatch).
set -uo pipefail

ENV_FILE=/etc/kaupo/kaupo.env
STATE_FILE=/var/lib/kaupo-diskwatch.level

TOPIC=$(grep '^KAUPO_NTFY_TOPIC=' "$ENV_FILE" 2>/dev/null | cut -d= -f2-)
[ -z "$TOPIC" ] && exit 0

usage=$(df -P / | awk 'NR==2 {gsub(/%/,"",$5); print $5}')
if [ "$usage" -ge 95 ]; then
  level=critical
elif [ "$usage" -ge 85 ]; then
  level=warning
else
  level=ok
fi

last=$(cat "$STATE_FILE" 2>/dev/null || echo ok)
[ "$level" = "$last" ] && exit 0
echo "$level" > "$STATE_FILE"

free=$(df -h / | awk 'NR==2 {print $4}')
store=$(du -sh /var/lib/containerd 2>/dev/null | cut -f1 || true)
if [ "$level" = ok ]; then
  msg="kaupo disk RECOVERED: / is ${usage}% full (${free} free)"
else
  msg="kaupo disk ${level^^}: / is ${usage}% full (${free} free); containerd store ${store:-unknown}"
fi
curl -fsS -m 10 -d "$msg" "https://ntfy.sh/$TOPIC" || true
