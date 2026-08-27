#!/usr/bin/env bash
# Refresh the Kraken rolling windows for the pair-quality universe, plus the
# trade-tick (order-flow) windows of the three most liquid pairs.
# Kraken serves at most the 720 newest candles of a timeframe: about 2 years
# at 1d, about 120 days at 4h. Venue checks against the trade venue go stale
# without a regular re-ingest. Trade ticks stay bounded: the ingest command
# prunes rows older than the retention window after each run. Runs daily from
# cron on the host. Idempotent (upserts).
set -uo pipefail

PAIRS="BTC/EUR ETH/EUR SOL/EUR XRP/EUR ADA/EUR LINK/EUR DOGE/EUR LTC/EUR AVAX/EUR DOT/EUR ATOM/EUR"
TIMEFRAMES="1d 4h"
TRADE_PAIRS="BTC/EUR ETH/EUR SOL/EUR"

cd /opt/kaupo || exit 1
fail=0
for pair in $PAIRS; do
  for tf in $TIMEFRAMES; do
    if ! docker compose --env-file /etc/kaupo/kaupo.env -f deploy/compose.prod.yml --profile trading \
      run --rm -T api kaupo ingest candles --exchange kraken --pair "$pair" --timeframe "$tf" --days 700; then
      echo "FAILED: $pair $tf" >&2
      fail=1
    fi
  done
done
for pair in $TRADE_PAIRS; do
  if ! docker compose --env-file /etc/kaupo/kaupo.env -f deploy/compose.prod.yml --profile trading \
    run --rm -T api kaupo ingest trades --exchange kraken --pair "$pair" --days 3; then
    echo "FAILED: trades $pair" >&2
    fail=1
  fi
done
[[ "$fail" -eq 0 ]] && echo "refreshed kraken ${TIMEFRAMES} for ${PAIRS}; trades for ${TRADE_PAIRS}"
exit "$fail"
