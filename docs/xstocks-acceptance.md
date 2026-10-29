# xStocks acceptance boundaries

The original calendar, risk, warm-up, metric, and reporting findings in [issue #43](https://github.com/nemecec/kaupo/issues/43) have targeted fixes.
The [support audit](non-24-7-status.md) links each finding to its implementation and describes the remaining limits.
Public data import and both supervised and standalone shadow routing are implemented.
These changes do not enable supported live xStocks trading.

## Evidence already available

Synthetic gap tests cover closed periods without requiring a trading account.
Public API checks establish access to xStocks metadata, recent candles, and historical spot trades.
History starts at each instrument's first available Kraken trade; a requested research period can still lack data.

On October 29, 2026, the current identity mapper accepted 354 public metadata rows.
Those rows supplied 531 exact pair aliases and 178 asset IDs across `currency` and `tokenized_asset` classes.
The response identified `AAPLx` as `tokenized_asset` and `ZUSD` as `currency`.
This check used no account credentials. It does not establish account eligibility or historical alias continuity.

## Account evidence still required

Use an explicitly selected environment and its existing secret store. Do not put credentials in command arguments or reports.
The local workspace had no configured Kraken API key or secret when checked on October 29, 2026.
No authenticated account request was made during this implementation work.

Read balances and open orders with an explicit unit mode using the [balance](xstocks-balances.md) and [open-order](xstocks-open-orders.md) commands.
Use the [order-status command](xstocks-order-status.md) for existing order IDs from that account.
Its ledger option collects order, fill, timing, identity, principal, and fee evidence in one report:

```sh
kaupo run xstocks-order-status --units base --order-id EXISTING-ORDER-ID --check-ledgers
```

If an independent record gives the intended expiry, add `--expected-expiry` with that Unix timestamp.
Do not infer the intended deadline from the returned expiry field itself.
A canceled or expired order can still have partial executions. Retain the fill and ledger evidence.
Use the [ledger-window command](xstocks-ledger-history.md) for non-trade activity and independent boundary-balance assertions.
Treat output as private account data. A matching report validates only its stated checks and scope.

The separate [order-check command](xstocks-order-check.md) can request validation-only order handling when explicitly selected.
Account reads and validation-only responses do not prove real order submission, cancellation, or expiry behavior.
This procedure does not place a real test order or authorize one.

## Live implementation still required

The live market adapter must route tokenized assets, preserve explicit units, and use validated exchange identities.
Live reconciliation must consume account evidence without losing signed fees or mixing asset classes and currencies.
Corporate actions, historical identity changes, and complete balance continuity remain unresolved.
Real order lifecycle acceptance needs a separately defined test scope and account-specific evidence.
The API and supervisor must continue rejecting live xStocks assignments until these prerequisites are met.

Deployment is separate from merging these changes. No production assignment, schedule, secret, or trading mode was changed.
