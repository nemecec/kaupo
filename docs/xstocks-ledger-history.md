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

The signed request uses scalar `asset` and `aclass` fields.
Kraken documents `aclass` as deprecated in favor of structured asset filters.
This implementation uses the documented scalar form supported by the existing form-encoded transport.
Authenticated acceptance of this request has not been tested. If Kraken rejects the filter, the command fails without partial success.

Output includes the exact request scope, observation interval, and ledger entries keyed by ID.
Amount, fee, and balance strings remain unchanged. Deposits, withdrawals, adjustments, and other returned types remain present.
Non-trade entries can have missing or empty reference IDs; trade-linked lookup retains its stricter reference checks.
Every entry must match the requested asset, class, and time window.
Malformed fields, changed duplicates, stalled cursors, and collections beyond the page limit cause a nonzero exit without success JSON.
Identical duplicate entries are combined by ledger ID. Empty results remain explicitly empty.

The optional `--check-balance-links` applies the [balance-link checker](xstocks-fills.md#check-balance-links-in-selected-ledgers) to this asset window.
The report uses scope `asset_window`. Gaps, tied timestamps, unsupported numbers, or insufficient entries produce exit code 1 with the report.
The first balance remains an unverified anchor. Empty windows report `no_ledgers` and exit with code 0.

This request covers the returned activity for one asset window, not all account history.
Matching links do not prove current account balances, account eligibility, or correct corporate-action treatment.
The command does not compare its last balance with the Balance endpoint or write database records.
Live xStocks trading remains disabled. Tests use mocked transport; no authenticated account request was made during implementation.
