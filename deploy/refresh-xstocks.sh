#!/usr/bin/env bash
# Optional xStocks-only refresh. The configuration is maintained on the host.
# Exit status and JSON output report calendar coverage gaps and fetch failures.
set -euo pipefail

config="${1:-/etc/kaupo/xstocks-refresh.json}"
if [[ ! -f "$config" || "$config" != /* ]]; then
  echo "Expected an existing absolute configuration path: $config" >&2
  exit 2
fi
cd /opt/kaupo
exec docker compose --env-file /etc/kaupo/kaupo.env -f deploy/compose.prod.yml --profile trading \
  run --rm -T -v "$config:/run/xstocks-refresh.json:ro" api \
  kaupo ingest xstocks-refresh --config /run/xstocks-refresh.json
