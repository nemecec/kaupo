#!/usr/bin/env bash
# Daily refresh of the rolling data windows, plus a permanent order-flow
# rollup. Runs from cron on the host. Idempotent (upserts).
#
# - Kraken 1d/4h candles for the pair-quality universe (venue checks; Kraken
#   serves at most the 720 newest candles of a timeframe).
# - Binance 1d/4h/1h candles for the universe (research windows must not go
#   stale; Binance is the deep-history venue).
# - Binance hourly open interest for the universe (positioning signal;
#   Binance serves only ~30 days back, so this is forward-collected and the
#   history accumulates here).
# - orderflow_daily rollup for yesterday (permanent aggregate archive).
# Trade ticks refresh separately every 4 hours (deploy/refresh-trades.sh).
set -uo pipefail

PAIRS="BTC/EUR ETH/EUR SOL/EUR XRP/EUR ADA/EUR LINK/EUR DOGE/EUR LTC/EUR AVAX/EUR DOT/EUR ATOM/EUR"
TIMEFRAMES="1d 4h"

cd /opt/kaupo || exit 1
fail=0
compose() {
  docker compose --env-file /etc/kaupo/kaupo.env -f deploy/compose.prod.yml --profile trading \
    run --rm -T api "$@"
}

for pair in $PAIRS; do
  for tf in $TIMEFRAMES; do
    if ! compose kaupo ingest candles --exchange kraken --pair "$pair" --timeframe "$tf" --days 700; then
      echo "FAILED: kraken $pair $tf" >&2
      fail=1
    fi
  done
done

for pair in $PAIRS; do
  for tf in $TIMEFRAMES; do
    if ! compose kaupo ingest candles --exchange binance --pair "$pair" --timeframe "$tf" --days 30; then
      echo "FAILED: binance $pair $tf" >&2
      fail=1
    fi
  done
done
for pair in $PAIRS; do
  if ! compose kaupo ingest candles --exchange binance --pair "$pair" --timeframe 1h --days 30; then
    echo "FAILED: binance $pair 1h" >&2
    fail=1
  fi
done
for pair in $PAIRS; do
  if ! compose kaupo ingest open-interest --pair "$pair" --days 30; then
    echo "FAILED: open-interest $pair" >&2
    fail=1
  fi
done

# defaults cover every pair with raw order-flow rows, yesterday (UTC)
if ! compose kaupo ingest orderflow-rollup; then
  echo "FAILED: orderflow-rollup" >&2
  fail=1
fi
[[ "$fail" -eq 0 ]] && echo "refreshed kraken+binance candles for ${PAIRS}; orderflow-rollup for yesterday"
exit "$fail"
