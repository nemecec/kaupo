#!/usr/bin/env bash
# Refresh the Kraken 1d rolling window for the pair-quality universe.
# Kraken serves at most the 720 newest candles of a timeframe, so venue
# checks against the trade venue go stale without a regular re-ingest.
# Runs daily from cron on the host. Idempotent (upserts).
set -uo pipefail

PAIRS="BTC/EUR ETH/EUR SOL/EUR XRP/EUR ADA/EUR LINK/EUR DOGE/EUR LTC/EUR AVAX/EUR DOT/EUR ATOM/EUR"

cd /opt/kaupo || exit 1
fail=0
for pair in $PAIRS; do
  if ! docker compose --env-file /etc/kaupo/kaupo.env -f deploy/compose.prod.yml --profile trading \
    run --rm -T api kaupo ingest candles --exchange kraken --pair "$pair" --timeframe 1d --days 700; then
    echo "FAILED: $pair" >&2
    fail=1
  fi
done
[[ "$fail" -eq 0 ]] && echo "refreshed kraken 1d for ${PAIRS}"
exit "$fail"
