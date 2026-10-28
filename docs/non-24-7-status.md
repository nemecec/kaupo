# Non-24/7 support status

This audit covers merged safeguards through PR #96 and the current order-to-fill instrument checks for [issue #43](https://github.com/nemecec/kaupo/issues/43).
The original calendar defects have targeted fixes. Supported xStocks live trading still requires the work listed after the table.
A merged safeguard does not establish account eligibility or validate real orders.

## Completed work

| Finding in #43 | Merged change | Current behavior and boundary |
| --- | --- | --- |
| Watchdog restarts during closures | [#69](https://github.com/nemecec/kaupo/pull/69) | An explicit assignment schedule changes the stale-candle calculation. Missing scheduled candles can still trigger a restart. |
| Daily-loss baseline discards opening gaps | [#59](https://github.com/nemecec/kaupo/pull/59) | A running risk manager compares the new day's first observation with its previous equity floor. Compatible restarts also restore [risk checkpoints](risk-resume.md). |
| Paper limits fill across stale candle gaps | [#58](https://github.com/nemecec/kaupo/pull/58) | Paper limits expire at their candle-based deadline. A late next candle cannot fill an expired order. |
| Live limits rest until another candle arrives | [#62](https://github.com/nemecec/kaupo/pull/62) | Kraken receives an absolute GTD expiry. Recording fills still waits for a candle or startup reconciliation. |
| Return intervals and annualization assume continuous candles | [#72](https://github.com/nemecec/kaupo/pull/72) | Scheduled backtests and rolling reports use UTC daily returns. Missing scheduled observations make ratios unavailable. |
| Warm-up counts elapsed time instead of observations | [#63](https://github.com/nemecec/kaupo/pull/63), [#65](https://github.com/nemecec/kaupo/pull/65) | Backtests select stored observations. Shadow/live startup refreshes recent closed candles before selecting history. Source depth still limits coverage. |
| Weekend gaps change momentum rebalance cadence | [#64](https://github.com/nemecec/kaupo/pull/64) | The default interval uses the declared timeframe. Closures can extend elapsed time between rebalances. |
| Polling and startup warnings misclassify closures | [#71](https://github.com/nemecec/kaupo/pull/71) | Explicit schedules control owed-candle and stale-tail diagnostics. Fetch errors remain visible. |
| Daily reports label closed runs inactive | [#70](https://github.com/nemecec/kaupo/pull/70) | Reports distinguish observed activity, supported closure, and inactivity. Current assignment evidence has historical limits. |
| Daily orderflow cannot identify a scheduled closure | [#73](https://github.com/nemecec/kaupo/pull/73) | Explicit rollup schedules produce scheduled-open or scheduled-closed states. Rows without schedule evidence remain unknown. |
| Limit prices ignore the market tick | [#45](https://github.com/nemecec/kaupo/pull/45) | Live limits use market tick metadata. This does not add xStocks market discovery to the trading client. |
| Assignments cannot select fees | [#68](https://github.com/nemecec/kaupo/pull/68) | Assignments carry maker and taker rates, including rebates, into supervised runs and rolling replays. |
| Reports combine currencies | [#66](https://github.com/nemecec/kaupo/pull/66), [#67](https://github.com/nemecec/kaupo/pull/67) | Daily totals separate currencies. Account-equity curves stay within one quote currency. |

## Data access and history

[PR #57](https://github.com/nemecec/kaupo/pull/57) added a dedicated public xStocks importer.
It supports market discovery, recent OHLC, and candle reconstruction from historical trades.
The [importer guide](xstocks-data.md) records the October 23, 2026 public API checks and their scope.
Those checks address public data access, not authenticated trading permission.

Available history starts at the first available trade for each instrument.
The importer cannot guarantee a one-year research window for every pair.
It does not substitute stock-exchange or perpetual-futures history for tokenized spot history.

## Implementation boundaries and remaining validation

### xStocks routing in runners and the trading client

The dedicated importer uses `XStocksClient` in [xstocks.py](../kaupo/data/xstocks.py).
Supervised shadow assignments with `asset_class=tokenized_asset` use this client for discovery, warm-up, and polling.
Existing assignments default to `currency`. xStocks assignments do not start Binance funding refreshes.
The API and supervisor reject xStocks live mode. The standalone shadow CLI retains crypto routing.
The [trading client](../kaupo/venues/kraken_client.py) still loads markets through its standard ccxt adapter.
A trading schedule changes diagnostics and metrics. It does not select an asset class or change endpoint routing.

The [xStocks guide](xstocks-data.md) describes assignment configuration and public OHLC history limits.
Authenticated symbol handling, order submission, reconciliation, and expiry need separate validation for the tokenized-asset path.
Public market discovery alone does not validate those operations.

### Risk state across process restarts

Shadow and live runners now persist and restore [risk checkpoints](risk-resume.md) with compatible ledger resumes.
Incomplete or invalid checkpoints and persisted risk halts refuse automatic restart for the same run identity.
Legacy predecessors without checkpoints retain fresh-risk compatibility behavior with a warning.
This transition cannot reconstruct historical risk state that was never recorded.

### Scheduled data operations

The [production refresh script](../deploy/refresh-kraken.sh) uses a fixed crypto universe and crypto data sources.
The separate [xStocks refresh command](xstocks-refresh.md) uses explicit per-pair calendars and reports stored candle coverage.
Its optional host wrapper supports a separate cron job and keeps xStocks outside the Binance loops.
New orderflow rows from the default refresh command remain unknown unless an explicit schedule accompanies their rollup.
Operators must maintain the calendars, schedule the refresh, and investigate missing observations.
The [raw-trade importer](xstocks-data.md#import-raw-trades-for-orderflow) preserves xStocks trade IDs for existing orderflow readers.
Operators schedule trade imports and calendar-aware daily rollups separately from candle refreshes.

### Account-specific validation

The [order-check command](xstocks-order-check.md) checks public rules and optionally requests authenticated validation without matching-engine trading.
The repository does not establish permission to trade xStocks on a particular account.
Account eligibility, instrument availability, and authenticated order behavior remain unverified by the public importer checks.
No authenticated order was submitted as part of this audit.
The [balance command](xstocks-balances.md) reads account balances with explicit xStocks units.
It preserves exchange asset IDs and does not feed live reconciliation.
The [open-order command](xstocks-open-orders.md) reads cursor pages with explicit units and preserves exchange fields.
The [order-status command](xstocks-order-status.md) reads exact order IDs, including terminal states, without treating missing orders as expiry evidence.
Its optional expiry assertion compares one limit order with an operator-supplied deadline and reports ambiguous or late observations for review.
Its optional fill check fetches exact trade references and compares their volume totals with reported executed volume.
Referenced fill timestamps are checked against order lifetime and, when supplied, the intended expiry.
The order lookup can also resolve referenced ledgers and reuse spot principal and per-asset fee checks.
Its metadata check requires order and fill aliases to resolve to the same instrument.
Order, fill, and ledger reads remain separate observations.
The [fill-history command](xstocks-fills.md) reads bounded cursor pages and preserves exchange transaction IDs and signed fees.
Its optional ledger lookup preserves the referenced asset IDs, signed amounts, and fees.
Optional metadata mapping annotates exact pair and asset IDs and reports unknown identifiers.
Ledger asset mapping also requires the declared asset class to match; known-ID class mismatches remain unresolved.
The optional ledger check compares spot principal movements and reports actual fees and net movements by mapped asset.
Fee-only entries in other assets retain their exact exchange IDs and remain separate from base and quote totals.
Optional balance-link checks detect arithmetic gaps between selected entries, with explicit limits for missing activity and tied timestamps.
The [ledger-history command](xstocks-ledger-history.md) reads all returned activity types for one exact asset window and can check its balance links.
Optional opening and closing balance assertions check window boundaries against operator-supplied historical records.
They do not establish the source or accuracy of those records.
Historical aliases, complete account balance continuity, third-asset principal movements, and corporate-action handling remain unsupported.

## Configuration and evidence

Schedules are explicit. Kaupo does not infer venue hours or holidays from a symbol.
Assignment schedules govern watchdogs, diagnostics, activity reports, and rolling metrics.
Backtest requests and orderflow rollups accept their own explicit schedules.
These inputs must describe the intended instrument and period.

The regression suite covers synthetic gaps, schedule boundaries, missing observations, and persisted data contracts.
It includes [gap-risk](../tests/behaviour/test_gap_risk.py), [gap-limit](../tests/behaviour/test_gap_limits.py),
[daily-return](../tests/unit/test_daily_return_sampling.py), and [orderflow-session](../tests/unit/test_orderflow_sessions.py) tests.
Those tests establish software behavior within their scope, not end-to-end xStocks trading readiness.

Related guides: [trading schedules](trading-schedules.md), [live expiry](live-order-expiry.md),
[assignment fees](assignment-fees.md), [daily reports](daily-reports.md), and [account equity](account-equity.md).
