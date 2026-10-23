# Live limit order expiry

Every limit placed by `KrakenVenue` has an expiry enforced by Kraken.
The client sends `timeinforce=GTD`, an absolute Unix timestamp in `expiretm`, and the existing post-only flag.
Kraken documents these parameters in [Add Order](https://docs.kraken.com/api-reference/trading/add-order).

The deadline is the close of the period immediately after the decision candle.
Orders store the opening timestamp of the decision candle, so the calculation is:

```text
expiry = order.created_ts + 2 * timeframe.seconds
```

For example, a 1h decision candle opens Friday at 20:00 UTC and closes at 21:00.
Its limit can rest until Friday at 22:00. A delayed submission does not extend that deadline.
The venue cancels a stale limit locally before placement. It checks the deadline after market metadata and sizing calls.

The exchange expiry does not require another candle or a running Kaupo process.
The venue still cancels tracked orders before it polls trades on the next candle.
A failed placement never retries without expiry. An ambiguous placement failure still makes only one placement attempt.

## Fills and reporting

Expiry cancels the unfilled remainder. Actual trades remain the source of fills, including partial fills before expiry.
The next candle or startup reconciliation records those fills. Expiry does not add a background poll or update the ledger during a closure.

Market orders have no GTD expiry. They keep their existing immediate-placement behavior.

## Scope

This implements the live-limit expiry safeguard in [#43](https://github.com/nemecec/kaupo/issues/43).
It also bounds order lifetime during candle outages on crypto markets.
It does not enable xStocks live trading. Calendar-aware watchdogs, risk-history persistence, and the other instrument prerequisites remain separate work.

Validation uses fake exchange calls and the installed ccxt parameter builder. No authenticated order was submitted to validate this change.
