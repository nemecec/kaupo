# Inspect fill history for xStocks account validation

The command reads trade history from the default Kraken wallet.
It requires `KAUPO_KRAKEN_API_KEY` and `KAUPO_KRAKEN_API_SECRET` with permission to query closed orders and trades.
Use the existing secret configuration. Do not put credentials in command arguments.

Choose explicit units and a time window:

```sh
kaupo run xstocks-fills --units rebased --start 1792886400 --end 1792972800
```

The bounds are integer Unix seconds. The start is exclusive; the end is inclusive.
Kaupo limits each request window to 31 days and each collection to 100 pages.
If the page limit is exceeded, use smaller adjacent windows. The command does not return partial success.

Kraken defines `rebased` as underlying-equity units and `base` as SPV-token units.
The reader sends the same bounds and units on every cursor request, with at most 100 fills per page.
It disables taker consolidation and requests related ledger IDs.
See the [Kraken Trades History API](https://docs.kraken.com/api-reference/account-data/get-trades-history).

The JSON output records the observation interval, requested bounds, units, and fills keyed by exchange transaction ID.
Each fill retains its order ID, symbol, amount strings, signed fee, and other returned fields.
Negative fees remain negative. The reader does not infer fee currency or calculate ledger entries.
Timestamps retain the numeric representation returned by the transport; they are not an exact-decimal timestamp archive.
The command includes all returned asset classes. It does not infer xStocks membership from symbols.

The reader checks required identifiers, side, numeric fields, and timestamps against the requested window.
It rejects malformed responses, non-finite amounts, changed duplicate fills, stalled cursors, and pagination beyond the page limit.
Identical duplicate fills are combined by transaction ID. An empty successful response produces an empty `fills` object.
Failures produce a nonzero exit and no success JSON. Transport error details are suppressed because they can contain signed requests.

This output is account inspection evidence. It does not establish an atomic snapshot or prove that the account history is complete.
Historical corrections and corporate actions can change later observations of the same window.
Treat saved output as private account data.

The command does not place or cancel orders, write database records, or start a runner.
xStocks live assignments remain disabled. Asset mapping, fee-currency reconciliation, and corporate-action handling remain separate work.
Tests exercise signing, cursor pagination, and failures with mocked transport. No authenticated account request was made during implementation.

## Read the referenced ledger entries

Add `--include-ledgers` to fetch the ledger entries referenced by the returned fills:

```sh
kaupo run xstocks-fills --units rebased --start 1792886400 --end 1792972800 --include-ledgers
```

This option also requires Kraken permission to query ledger entries.
Kaupo queries unique ledger IDs in batches of 20, with the same unit setting as the fill request.
See the [Kraken Query Ledgers API](https://docs.kraken.com/api-reference/account-data/query-ledgers).

Successful output has status `fills_and_ledgers_read` and a `ledgers` object keyed by ledger ID.
Entries preserve exchange asset IDs, reference IDs, signed amounts, signed fees, and balance strings.
The original fill references remain available for inspection. Kaupo does not infer fee currency from the trading pair.
It does not sum entries, translate asset aliases, or check that ledger amounts reconcile with fill amounts.

Every nonempty fill collection must provide nonempty lists of valid ledger references.
Missing or unexpected response IDs, invalid core entry fields, and failed batches cause a nonzero exit with no success JSON.
An empty fill collection returns empty ledgers without a ledger request.
Kaupo permits at most 2,000 unique ledger IDs per invocation. If this limit is exceeded, use a smaller fill window.

This option leaves the existing read-only boundary in place. The fill and ledger requests do not form an atomic snapshot.
Tests use mocked transport; no authenticated ledger request was made during implementation.
