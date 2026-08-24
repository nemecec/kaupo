# Kaupo

Kaupo is an autonomous algorithmic crypto-trading system. The name is an Estonian given name. It comes from *kaup*, which means "goods" or "merchandise".

Kaupo runs in the background and trades without manual input. Trading strategies are deterministic code plugins. They live in a separate private repository. The same strategy code runs in three modes:

| | Backtest | Shadow | Live |
|---|---|---|---|
| Candle source | Historical data from Postgres | Live polling from the exchange | Live polling from the exchange |
| Venue | Paper venue with fees and slippage | Paper venue | Exchange through ccxt |
| Money | Virtual | Virtual | Real |
| Purpose | Fast feedback for strategy iteration | Forward validation | Real trading |

## Status

Phase 1 (MVP) is complete:

- Market-data ingestion from Kraken
- Strategy SDK and determinism linter
- Backtesting and shadow trading
- Ledger, risk manager, and kill switch
- REST API, daily reports, and React dashboard

Live trading with real money is not enabled yet. That is Phase 3.

## Quickstart (local)

Prerequisites: uv, Docker, and Node 22 or later.

1. Install the Python dependencies:
   ```bash
   uv sync
   ```
2. Start Postgres:
   ```bash
   docker compose up -d db
   ```
3. Run the migrations:
   ```bash
   uv run alembic upgrade head
   ```
4. Download historical candles:
   ```bash
   uv run kaupo ingest --pair BTC/EUR --timeframe 1h --days 365
   ```
   Kraken serves only the 720 newest candles. For deep history, backfill from Binance (public API, no key) with `--exchange binance`:
   ```bash
   uv run kaupo ingest --exchange binance --pair BTC/EUR --timeframe 1h --days 2400
   ```
5. Run a backtest with the example strategy:
   ```bash
   uv run kaupo backtest --strategy regime-switch --pair BTC/EUR --timeframe 1h --days 365
   ```
   To use the Binance candles, add `--exchange binance`.
6. Start shadow trading with virtual money:
   ```bash
   uv run kaupo run shadow --strategy regime-switch --pair BTC/EUR --timeframe 1h
   ```
7. Start the API and the UI:
   ```bash
   uv run uvicorn kaupo.api.app:app --reload
   cd ui && npm install && npm run dev
   ```

## The full stack in Docker

1. Start the stack:
   ```bash
   docker compose up -d
   ```
2. To also start a shadow-trading container, use the trading profile:
   ```bash
   docker compose --profile trading up -d
   ```
3. Open the dashboard at http://localhost:3000. The API listens on http://localhost:8100.

To use your private strategies instead of the example, set `KAUPO_STRATEGIES_DIR`:

```bash
KAUPO_STRATEGIES_DIR=../kaupo-strategies/strategies docker compose --profile trading up -d
```

The shadow strategy comes from `KAUPO_SHADOW_STRATEGY` (default `regime-switch`). Set it to a strategy id from your private repository.

### Switching the shadow strategy at runtime

`GET /api/v1/settings` shows the effective shadow configuration. `PUT /api/v1/settings` changes it. The current shadow run stops through the control channel, and the restarted container reads the new values from the database. No redeploy is necessary. The `KAUPO_SHADOW_STRATEGY` variable only seeds a fresh database; it does not overwrite an API change.

To stop the stack:

```bash
docker compose --profile trading down
```

## Deployment (production)

The production stack runs on one Hetzner CX23 server behind Caddy at https://kaupo.trade. Images come from GHCR. Deploys run from GitHub Actions over SSH after a green CI run on `main`. Nightly `pg_dump` backups go to AWS S3.

See `deploy/README.md` for the setup steps.

## Tests

```bash
uv run pytest tests/unit tests/behaviour   # fast, no Docker needed
uv run pytest tests/integration            # uses testcontainers (Docker required)
uv run ruff check . && uv run mypy         # lint and typecheck
```

The CI workflow runs the same checks. The coverage gate is 85%.

## Repository layout

- `kaupo/` — the platform package: `core`, `data`, `sdk`, `backtest`, `venues`, `risk`, `ledger`, `report`, `api`, `cli`, `db`
- `examples/strategies/` — open-source example strategies. Your real strategies live in the private `kaupo-strategies` repository
- `ui/` — the React and TypeScript dashboard
- `tests/` — `unit`, `behaviour` (scenario and parity), and `integration` tests
- `docs/design.html` — the full architecture and roadmap

## Design

Read `docs/design.html` for the architecture and the roadmap. Where the document and the code differ, the code wins.

## License

Apache-2.0. Strategies are not part of this repository and carry their own terms.
