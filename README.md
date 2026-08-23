# Kaupo

Autonomous algorithmic crypto-trading system. Named after the Estonian given name
*Kaupo*, derived from *kaup* — "goods / wares / merchandise".

Kaupo runs 24/7 in the background and trades fully autonomously. Trading
strategies are **deterministic code plugins** (kept in a separate, private
repository) that run **unchanged** in three execution modes:

| | Backtest | Shadow | Live |
|---|---|---|---|
| Candle source | historical (DB) | live polling | live polling |
| Venue | paper (fees + slippage modeled) | paper | exchange via ccxt |
| Money | virtual | virtual | real |
| Purpose | fast feedback for (AI-agent) strategy iteration | forward validation | real trading |

## Status

Phase 1 (MVP): market-data ingestion (Kraken), strategy SDK, backtesting,
shadow trading, ledger, risk manager, REST API + daily reports, React/TS
dashboard. Live trading with real money is **not** enabled yet (Phase 3).

## Quickstart (local)

Prerequisites: [uv](https://docs.astral.sh/uv/), Docker (for Postgres), Node 22+ (for the UI).

```bash
# 1. Install Python deps
uv sync

# 2. Start Postgres
docker compose up -d db

# 3. Run migrations
uv run alembic upgrade head

# 4. Download historical candles
uv run kaupo ingest --pair BTC/EUR --timeframe 1h --days 365

# 5. Run a backtest with the example strategy
uv run kaupo backtest --strategy regime-switch --pair BTC/EUR --timeframe 1h --days 365

# 6. Start shadow trading (paper money, live data)
uv run kaupo run shadow --strategy regime-switch --pair BTC/EUR --timeframe 1h

# 7. Start the API and the UI
uv run uvicorn kaupo.api.app:app --reload
cd ui && npm install && npm run dev
```

Or the whole stack in Docker: `docker compose up` (add `--profile trading` to
also start a shadow-trading container).

## Tests

```bash
uv run pytest tests/unit tests/behaviour   # fast, no Docker needed
uv run pytest tests/integration            # uses testcontainers (Docker)
uv run ruff check . && uv run mypy         # lint + typecheck
```

## Repository layout

- `kaupo/` — the platform (Python package; `core`, `data`, `sdk`, `backtest`,
  `venues`, `risk`, `ledger`, `report`, `api`, `cli`)
- `examples/strategies/` — open-source example strategies (your real
  strategies live in the private `kaupo-strategies` repo)
- `ui/` — React + TypeScript dashboard
- `tests/` — `unit`, `behaviour` (scenario/parity), `integration`

## Design

See `docs/design.html` for the full architecture and roadmap.

## License

Apache-2.0. Strategies are *not* part of this repo and carry their own terms.
