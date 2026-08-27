#!/usr/bin/env bash
# Weekly rolling-origin triage: re-backtest every enabled shadow assignment
# over the last 30 days against stored candles and compare it with the
# shadow chain's actual equity and fills. The digest goes to the ntfy topic;
# the full report persists as one reports row per ISO week (idempotent).
# Runs from cron on the host.
set -uo pipefail

cd /opt/kaupo || exit 1
docker compose --env-file /etc/kaupo/kaupo.env -f deploy/compose.prod.yml --profile trading \
  run --rm -T api kaupo report rolling-origin
