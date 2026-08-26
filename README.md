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
   uv run kaupo ingest candles --pair BTC/EUR --timeframe 1h --days 365
   ```
   Kraken serves only the 720 newest candles. For deep history, backfill from Binance (public API, no key) with `--exchange binance`:
   ```bash
   uv run kaupo ingest candles --exchange binance --pair BTC/EUR --timeframe 1h --days 2400
   ```
5. Download historical funding rates (optional, used as a filter signal):
   ```bash
   uv run kaupo ingest funding --pair BTC/EUR --days 365
   ```
   Funding rates come from perpetual futures. They mark crowded positioning. Kaupo trades spot only, so funding is an advisory signal, not a traded instrument. The data comes from the Binance USDT-margined perpetual of the pair's base asset (BTC/EUR maps to the BTC perpetual). Kraken funding is not supported.
6. Run a backtest with the example strategy:
   ```bash
   uv run kaupo backtest --strategy regime-switch --pair BTC/EUR --timeframe 1h --days 365
   ```
   To use the Binance candles, add `--exchange binance`.
   For a portfolio backtest over several pairs, pass `--pairs` to a portfolio strategy. All pairs share one quote currency:
   ```bash
   uv run kaupo backtest --strategy momentum-rotation --pairs BTC/EUR,SOL/EUR,ADA/EUR --timeframe 1h --days 365
   ```
   Backtests use the live risk caps by default: 1000 quote per pair, 2000 quote gross exposure, 200 quote daily loss. These caps clamp research strategies that target a larger book. Three flags relax the caps for one backtest run: `--max-position-quote`, `--max-gross-exposure-quote`, and `--max-daily-loss-quote`. Values must be positive. The API accepts the same three fields on `POST /api/v1/backtests`. The overrides apply to backtests only. Live and shadow guardrails do not change.
7. Start shadow trading with virtual money:
   ```bash
   uv run kaupo run shadow --strategy regime-switch --pair BTC/EUR --timeframe 1h
   ```
8. Start the API and the UI:
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

### Changing what runs

The `run_assignments` table declares the desired shadow runs. `GET /api/v1/assignments` lists the rows with their live run ids. `POST`, `PUT`, and `DELETE /api/v1/assignments/{id}` manage them with the admin token. The supervisor (`kaupo run supervisor`, the `supervisor` service in the production stack) reconciles the actual runs to the enabled rows: it starts missing runs, stops disabled or changed ones, and restarts crashed ones. `PUT /api/v1/settings` still switches the main run: it updates the `primary` assignment row. For a manual side run outside the portfolio, use `kaupo run shadow --no-config-from-db` with explicit flags.

### Backtest jobs

Backtests submitted through `POST /api/v1/backtests` run in a separate worker process (`kaupo run backtest-worker`, the `backtest-worker` service), not in the API. The API writes one durable row per job to the `backtest_jobs` table and returns. The worker claims the oldest queued job, runs it, and stores the result. Jobs survive an API restart. When no worker runs, jobs wait queued. At startup, the worker fails jobs that a crashed worker left behind.

### Portfolio shadow runs

An assignment row with a `pairs` list instead of a `pair` declares a portfolio run. The universe follows the portfolio backtest rules: at least two pairs, no duplicates, one shared quote currency, canonical sorted order. The `pair` column stores the comma-joined universe. The strategy must derive from `PortfolioStrategyBase`. The API enforces all of this (`422` on a violation).

```bash
curl -X POST http://localhost:8100/api/v1/assignments \
  -H "Authorization: Bearer $ADMIN_TOKEN" -H "Content-Type: application/json" \
  -d '{"strategy_id": "momentum-rotation", "pairs": ["BTC/EUR", "SOL/EUR", "ADA/EUR"], "timeframe": "1h"}'
```

The supervisor runs the row as one shadow run over the whole universe. One poller per pair fetches the new candles. A joiner merges them into one step per timestamp, the same step sequence the portfolio backtest produces over the same candles. A pair that misses a tick skips it. The engine uses its last known close. Control commands (kill, pause, resume, switch), crash backoff, and orphan cleanup work as for single-pair runs. A change to the universe restarts the run.

A portfolio shadow run accumulates the shadow days that the promotion gates require, the same as a single-pair run. For a manual portfolio shadow run outside the assignments table, pass `--pairs` with `--no-config-from-db`:

```bash
uv run kaupo run shadow --no-config-from-db --strategy momentum-rotation --pairs BTC/EUR,SOL/EUR --timeframe 1h
```

To stop the stack:

```bash
docker compose --profile trading down
```

## Deployment (production)

The production stack runs on one Hetzner CX23 server behind Caddy at https://kaupo.trade. Images come from GHCR. Deploys run from GitHub Actions over SSH after a green CI run on `main`. Nightly `pg_dump` backups go to AWS S3.

See `deploy/README.md` for the setup steps.

## Strategy SDK

A strategy is a Python class with an `id`, a pydantic `params_schema`, and an `on_candle(ctx)` method that returns order intents. Two base classes exist:

- `StrategyBase` — one pair per run. It runs in backtest, shadow, and live modes.
- `PortfolioStrategyBase` — a universe of pairs in one run. It runs in backtest and shadow modes. Its context gives the candles closed at each step, per-pair history, all open positions, cash, and equity. Each order intent names its pair. Intents for pairs outside the universe are rejected. A shadow run feeds the strategy the same step sequence as a backtest over the same candles: one step per universe tick.

The context also gives funding rates: `ctx.funding(n)` on a single pair, `ctx.funding(pair, n)` on a portfolio. Each call returns the newest `n` funding points for the pair's base asset, oldest first. Only points with a funding time at or before `clock.now()` are returned. This holds in backtests and in live runs. The series is empty when no funding data was ingested. Funding is an advisory filter signal from Binance USDT perpetuals, keyed by base asset. One series per base asset, not per pair. Shadow runs refresh recent funding in the background every `KAUPO_FUNDING_REFRESH_SECONDS` (default 1800). A refresh failure does not stop the run.

`kaupo/sdk/portfolio.py` has a rebalance helper. Give it target weights (fractions of equity, sum at most 1) and the context. It returns a plan with `sells` and `buys`. Buys use only the free cash at plan time, never the proceeds of same-plan sells. Emit the sells on one step and the buys on the next step. `examples/strategies/momentum_rotation.py` shows this pattern.

The determinism linter (`kaupo lint-strategies`) checks both kinds. The API accepts a portfolio backtest through `POST /api/v1/backtests` with `pairs` instead of `pair`. Its metrics add `universe` and a `per_pair` attribution (realized PnL, fees, round trips, win rate per pair).

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
- `ui/` — the React and TypeScript dashboard; its account-equity panel stitches the sequential runs of a strategy into one curve
- `tests/` — `unit`, `behaviour` (scenario and parity), and `integration` tests
- `docs/design.html` — the full architecture and roadmap

## Design

Read `docs/design.html` for the architecture and the roadmap. Where the document and the code differ, the code wins.

## License

Apache-2.0. Strategies are not part of this repository and carry their own terms.
