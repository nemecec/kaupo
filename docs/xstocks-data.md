# Kraken xStocks data

Kaupo can import public xStocks candles without account credentials. This is the data-access part of [issue #43](https://github.com/nemecec/kaupo/issues/43).
The [support status](non-24-7-status.md) separates completed calendar safeguards from remaining routing and account validation.
This importer does not enable xStocks trading.

## List markets

Run the discovery command:

```sh
uv run kaupo ingest xstocks-markets --country EE
```

The country filter limits the public market list. It does not establish permission to trade on a specific account.
Kaupo stores `AAPLX/USD` and sends Kraken's canonical `AAPLxUSD` symbol. Discovery removes duplicate aliases.

Kraken uses different parameters for discovery and market data:

| Endpoint | Required parameter |
| --- | --- |
| `AssetPairs` | `aclass_base=tokenized_asset` |
| `OHLC`, `Trades` | `asset_class=tokenized_asset` |

The standard currency request omits xStocks. The dedicated importer uses public REST requests because the installed crypto adapter does not supply these parameters.

## Import historical candles

Configure `KAUPO_DATABASE_URL` for the target database. Then run the import:

```sh
uv run kaupo ingest xstocks-candles \
  --pair AAPLx/USD --timeframe 1h \
  --start 2025-08-14 --end 2026-10-23
```

The default source is historical trades. The importer follows Kraken's nanosecond cursor and reconstructs candles on UTC boundaries.
It uses trade IDs to remove page overlap. Distinct trades with identical timestamps, prices, and sizes all contribute volume.
The importer retains decimal timestamps and volumes until it creates each candle.

The range is half-open: `[start, end)`. Only whole candle periods inside that range qualify.
The current candle and the two-second closing margin do not qualify. The importer does not create bars for periods without trades.
Thus, a weekend gap remains a gap. A missing bar alone does not distinguish a market closure from a period without trades.

The importer commits batches of 100 completed candles. An API error stops the command and leaves completed batches in the database.
An unfinished candle stays in memory and never overwrites a stored candle. To resume after an error, run the same command again.
The upsert replaces the same candle keys, so a repeated import does not add volume twice.

History starts with the first available Kraken trade. Stock-exchange history and perpetual-futures history are not substitutes for xStocks spot trades.
The coverage summary reports the stored candle count and timestamps. It does not certify a continuous trading history.

## Import recent candles

For a short refresh, use Kraken's OHLC endpoint:

```sh
uv run kaupo ingest xstocks-candles \
  --pair AAPLx/USD --timeframe 1h --days 7 --recent
```

Kraken limits this endpoint to the newest 720 bars. An older `since` value does not provide deeper history.
The historical-trade mode has no OHLC bar limit. Both modes store candles under the `kraken` exchange and the normalized pair.
The recent mode drops Kraken's zero-volume, zero-trade bars to preserve empty periods.

The existing production refresh scripts serve the crypto universe.
The separate [scheduled xStocks refresh](xstocks-refresh.md) supports explicit calendars and candle coverage checks.
Do not add xStocks to the shared Binance candle, funding, or open-interest loops.

## Import raw trades for orderflow

Run the trade importer with an explicit asset class:

```sh
uv run kaupo ingest trades --exchange kraken --asset-class tokenized_asset \
  --pair AAPLx/USD --days 3
```

This path uses public xStocks discovery and trade pagination. It preserves exchange trade IDs and taker sides.
The default `currency` path retains the existing crypto adapters. The command rejects xStocks with `--exchange binance`.

Distinct trade IDs remain distinct rows even when their timestamps, prices, sizes, and sides match.
Repeated imports skip retained IDs. Stored IDs are immutable: another payload with the same exchange, pair, and ID does not replace the row.
The importer rejects changed trades within overlapping API pages.

Source timestamps retain decimal precision during pagination and range filtering.
Storage floors timestamps to microseconds, so sub-microsecond trades do not round into the next candle bucket.
Prices and sizes use the existing floating-point tick representation. This store is an analytics input, not an exact exchange audit archive.

The importer commits batches of 100 ticks. A failed import retains committed batches and returns an error.
An incomplete batch stays in memory. A repeated import safely recovers those ticks.
The displayed import count includes ticks submitted again, while the database count reflects retained rows.

The requested range is half-open: `[start, end)`. The command permits at most 31 days per run.
After a successful import, the command prunes this pair's ticks using `KAUPO_TRADES_RETENTION_DAYS`, which defaults to 30 days.
Retention uses the current time, not the requested range end. Old historical tick imports can therefore be pruned immediately.
Candle reconstruction uses its separate historical importer and does not depend on retained raw ticks.

After a successful trade import, run the daily rollup with the instrument's explicit calendar:

```sh
uv run kaupo ingest orderflow-rollup --exchange kraken --pair AAPLX/USD \
  --trading-schedule /etc/kaupo/aaplx-schedule.json
```

This schedule file contains a single `TradingSchedule` object, not the refresh manifest.
The default rollup date is yesterday in UTC. An explicit `--day YYYY-MM-DD` selects another date.
The existing orderflow readers include these ticks in buy/sell counts and volumes.
The [candle refresh command](xstocks-refresh.md) does not start trade collection or orderflow rollups. Operators schedule these commands separately.

### Tick-store migration

Migration `0026` adds the optional trade ID and rebuilds the tick table's primary key.
This operation locks the table and rebuilds its index. Its duration depends on the retained table size.
Existing rows receive an empty ID and retain their previous tuple-based deduplication behavior.
The migration cannot recover trades that the old adapters already collapsed.
A downgrade refuses to remove IDs when distinct trades share a legacy key.

## Backtest warm-up

Backtests load up to `lookback` stored candles strictly before the evaluation start.
They count observations instead of elapsed calendar periods. A portfolio backtest selects this history independently for each pair.
Funding and positioning history extend to the oldest selected candle when necessary.

Warm-up does not call the strategy or create equity snapshots. The strategy first runs inside the evaluation window.
If the database has fewer candles, the backtest uses the available history. It does not invent bars or guarantee a full indicator warm-up.

## Live and shadow warm-up

At startup, live and shadow runners request Kraken's latest closed candle page without a calendar cutoff.
Portfolio shadow runs refresh each pair separately. The runners then select the requested count from stored observations.
Older imported candles remain available when the requested warm-up exceeds the API page limit.

The refresh makes one candle request per pair. It does not retrieve history beyond Kraken's OHLC limit or create missing candles.
If the refresh fails, the runner logs a warning and uses stored history.
Sparse source history can still leave indicators short of their requested warm-up.

The runners support explicit [trading schedules](trading-schedules.md) for closure diagnostics.
Supervised shadow assignments select the dedicated xStocks client when `asset_class` is `tokenized_asset`.
Create an assignment through `POST /api/v1/assignments`, for example:

```json
{
  "id": "xstocks-shadow",
  "strategy_id": "sma-cross",
  "pair": "AAPLX/USD",
  "timeframe": "1h",
  "mode": "shadow",
  "asset_class": "tokenized_asset",
  "enabled": false
}
```

Set the intended parameters, fees, and trading schedule before enabling the assignment.
The example is disabled for configuration; it does not define an exchange calendar.
For a portfolio strategy, supply `pairs` instead of `pair`. Every pair must belong to the xStocks market list.
Discovery validates the entire universe before warm-up or run creation. Discovery failures stop startup.
Startup and polling use the dedicated public OHLC endpoint. Neither path starts a Binance funding client.
Polling uses the newest OHLC page; import history separately after an outage that exceeds its 720-bar limit.

Existing assignments default to `currency`. A schedule or symbol suffix does not select xStocks routing.
Changing `asset_class` restarts the assignment with a new ledger and risk identity.
An omitted or null `asset_class` in an update preserves the stored class.
The run configuration records the class. Rolling reports reject comparisons against a chain with a different class.

This routing applies to supervised shadow assignments. The standalone `kaupo run shadow` command retains crypto routing.
The assignment API and supervisor reject xStocks live mode. Authenticated xStocks orders remain unsupported.

## Trading readiness

The [support status](non-24-7-status.md) maps each original finding in #43 to its merged fix.
Completed work includes closure-aware watchdogs and reports, limit expiry, and opening-gap risk checks.
It also includes daily return sampling, observation-based warm-up, stable rebalance cadence, and orderflow sessions.

Daily reports separate amounts by currency, and account-equity curves support currency filtering.
Assignments support maker and taker fees, including rebates.
The [order-check command](xstocks-order-check.md) checks public rules and can request authenticated validation.
These safeguards do not establish supported xStocks live trading.

Compatible restarts restore [risk checkpoints](risk-resume.md), with an explicit limitation for legacy runs.
Remaining work includes authenticated trading-client routing, dedicated data operations, and account-specific validation.

## References and validation

The public API check on October 23, 2026 returned AAPLx and SPYx trades from August 2025 and QQQx trades from September 2025.
AAPLx trade pagination advanced to a second historical page. Account-specific trading permission was not tested.

- [Kraken instrument discovery](https://docs.kraken.com/api-reference/market-data/get-tradable-asset-pairs)
- [Kraken OHLC parameters and limits](https://docs.kraken.com/api-reference/market-data/get-ohlc-data)
- [Kraken trade endpoint](https://docs.kraken.com/api-reference/market-data/get-recent-trades)
- [Kraken historical-data guidance](https://support.kraken.com/articles/advanced-api-faq)

For read-only account evidence, use the [balance inspection command](xstocks-balances.md).

Use [open-order inspection](xstocks-open-orders.md) to read resting orders with explicit xStocks units.

Use [fill-history inspection](xstocks-fills.md) to read authenticated trades within an explicit time window.

## Start a standalone shadow run

Use explicit static configuration for an xStocks side run:

```sh
uv run kaupo run shadow --no-config-from-db --asset-class tokenized_asset \
  --strategy regime-switch --pair AAPLX/USD --timeframe 1h \
  --trading-schedule ./xstocks-schedule.json --maker-fee-bps=-2 --taker-fee-bps=10
```

The fee values above illustrate signed basis points. Set them to the rates you intend to simulate.
Without fee overrides, the command retains the configured default rates; it does not infer xStocks fees.
The schedule file contains a [TradingSchedule](trading-schedules.md), without an outer `trading_schedule` key.
Maintain the actual venue hours and date overrides. Without a schedule, the existing 24/7 assumptions apply to diagnostics.

For a portfolio strategy, replace `--pair` with a comma-separated `--pairs` universe.
Use a schedule that covers every instrument in that universe.
Both paths use public xStocks discovery, warm-up, and polling. The runner validates every requested pair.
Neither path constructs a Binance funding client.

xStocks requires `--no-config-from-db` because shared instance settings do not contain an asset-class field.
This prevents stored crypto strategy or pair settings from overriding the explicit xStocks configuration.
The default `--asset-class currency` retains existing crypto routing and database-backed settings behavior.
Schedule and fee options are also available for crypto shadow runs.

Shadow runs use virtual money but persist their candles, run records, and simulated activity to the configured database.
This command does not enable live xStocks trading or deploy a supervised assignment.
