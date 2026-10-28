# Read an asset's ledger window

Use this command to inspect ledger activity beyond the entries referenced by fills:

```sh
kaupo run xstocks-ledgers --asset AAPLx --asset-class tokenized_asset --units rebased --start 1792886400 --end 1792972800 --check-balance-links
```

Use an exact Kraken asset ID and an explicit asset class: `currency` or `tokenized_asset`.
The command does not translate symbols, strip suffixes, or accept `all` or multiple assets.
It requires `KAUPO_KRAKEN_API_KEY` and `KAUPO_KRAKEN_API_SECRET` with permission to query ledger entries.
It reads the default wallet.

The start is exclusive and the end is inclusive, in integer Unix seconds.
Kaupo limits the window to 31 days and collection to 100 cursor pages.
Kraken returns up to 50 entries per page. The command requests `type=all` to retain non-trade activity.
Each page uses the same asset, class, units, and time bounds.
See the [Kraken Ledgers API](https://docs.kraken.com/api-reference/account-data/get-ledgers-info).

The signed JSON request uses `asset: [{"asset": "AAPLx", "aclass": "tokenized_asset"}]` for this example.
It omits the deprecated top-level `aclass` field.
A dedicated Ledgers signer preserves the structured filter and signs the exact JSON body.
Other endpoints retain ccxt signing. Transport, throttling, and response handling still use ccxt.
See [Kraken REST authentication](https://docs.kraken.com/exchange/guides/rest/authentication).
Authenticated acceptance has not been tested. A rejected request fails without partial success.

Output includes the exact request scope, observation interval, and ledger entries keyed by ID.
Amount, fee, and balance strings remain unchanged. Deposits, withdrawals, adjustments, and other returned types remain present.
Non-trade entries can have missing or empty reference IDs; trade-linked lookup retains its stricter reference checks.
Every entry must match the requested asset, class, and time window.
Malformed fields, changed duplicates, stalled cursors, and collections beyond the page limit cause a nonzero exit without success JSON.
Identical duplicate entries are combined by ledger ID. Empty results remain explicitly empty.

The optional `--check-balance-links` applies the [balance-link checker](xstocks-fills.md#check-balance-links-in-selected-ledgers) to this asset window.
The report uses scope `asset_window`. Gaps, tied timestamps, unsupported numbers, or insufficient entries produce exit code 1 with the report.
The first balance remains an unverified anchor. Empty windows report `no_ledgers` and exit with code 0.

## Assert opening and closing balances

Supply both balances to check the window against independent records:

```sh
kaupo run xstocks-ledgers --asset AAPLx --asset-class tokenized_asset --units base --start 1792886400 --end 1792972800 --opening-balance 10 --closing-balance 11.1
```

The opening balance must describe the account immediately after the exclusive start timestamp.
The closing balance must describe the account at the inclusive end timestamp, after all entries at that time.
Both values must use the requested asset, asset class, default wallet, and unit mode.
Use historical records for these times. A current Balance response does not establish a historical boundary.
Both options are required together. Invalid or unsupported decimal values fail before the account request.

The `balance_window` report labels its anchors as `operator` evidence and preserves both supplied values.
It checks the first entry against `opening + amount - fee`, each internal link, and the last balance against the supplied closing balance.
A single entry can pass both boundary checks. An empty window requires equal opening and closing balances.
These checks use decimal arithmetic and include signed fees and rebates.
Gaps, tied timestamps, or unsupported numbers produce `needs_review` and exit code 1.
A successful report uses `balances_match`; it does not verify the source of the supplied anchors.
Offsetting omitted activity can still pass arithmetic checks. Matching balances do not prove complete history.

This option does not require `--check-balance-links`.
If both checks are requested, each report retains its own meaning; either `needs_review` result causes exit code 1.

This request covers the returned activity for one asset window, not all account history.
Matching links do not prove current account balances, account eligibility, or correct corporate-action treatment.
The command does not compare its last balance with the Balance endpoint or write database records.
Live xStocks trading remains disabled. Tests use mocked transport; no authenticated account request was made during implementation.
