#!/usr/bin/env bash
# Refresh trade ticks for the pair-quality universe every 4 hours.
# The daily candle refresh also runs (deploy/refresh-kraken.sh); this loop
# keeps the order-flow window fresh between those runs. The ingest prunes
# rows older than the retention window after each pair. Idempotent (upserts).
set -uo pipefail

PAIRS="BTC/EUR ETH/EUR SOL/EUR XRP/EUR ADA/EUR LINK/EUR DOGE/EUR LTC/EUR AVAX/EUR DOT/EUR ATOM/EUR"

cd /opt/kaupo || exit 1
fail=0
for pair in $PAIRS; do
  if ! docker compose --env-file /etc/kaupo/kaupo.env -f deploy/compose.prod.yml --profile trading \
    run --rm -T api kaupo ingest trades --exchange kraken --pair "$pair" --days 1; then
    echo "FAILED: trades $pair" >&2
    fail=1
  fi
done
[[ "$fail" -eq 0 ]] && echo "refreshed trades for ${PAIRS}"
exit "$fail"
