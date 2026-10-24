# Kraken xStocks data

Kaupo can import public xStocks candles without account credentials. This is the data-access part of [issue #43](https://github.com/nemecec/kaupo/issues/43).
Market calendars, live orders, risk controls, and currency-aware reports remain separate work. This importer does not enable xStocks trading.

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

The production refresh scripts still serve the crypto universe. Use these dedicated commands for xStocks imports.
Do not add xStocks to the shared Binance candle, funding, or open-interest loops.

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

This change does not add xStocks API routing to the live runners or recognize scheduled closures.
The watchdog, poller, and stale-tail warnings still require calendar support.

## Remaining work in #43

Daily reports and notifications separate amounts by currency. See [daily report currencies](daily-reports.md) for the API fields and compatibility rules.
Equity-curve stitching still needs currency isolation.

The following items remain prerequisites for supported xStocks trading:

- Market calendars for the watchdog, poller, and activity reports.
- Calendar-aware execution beyond the paper gap guard and [live limit expiry](live-order-expiry.md).
- Risk checks that include losses across a market closure.
- Session-aware return statistics, indicator warm-up, and rebalance timing.
- Tick-size rounding, instrument-specific fees, and currency-aware reports.

## References and validation

The public API check on October 23, 2026 returned AAPLx and SPYx trades from August 2025 and QQQx trades from September 2025.
AAPLx trade pagination advanced to a second historical page. Account-specific trading permission was not tested.

- [Kraken instrument discovery](https://docs.kraken.com/api-reference/market-data/get-tradable-asset-pairs)
- [Kraken OHLC parameters and limits](https://docs.kraken.com/api-reference/market-data/get-ohlc-data)
- [Kraken trade endpoint](https://docs.kraken.com/api-reference/market-data/get-recent-trades)
- [Kraken historical-data guidance](https://support.kraken.com/articles/advanced-api-faq)
