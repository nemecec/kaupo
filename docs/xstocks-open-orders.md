# Inspect open orders for xStocks account validation

The command reads open orders from the default Kraken wallet.
It requires `KAUPO_KRAKEN_API_KEY` and `KAUPO_KRAKEN_API_SECRET` with permission to query open orders and trades.
Use the existing secret configuration. Do not put credentials in command arguments.

Choose the xStocks unit explicitly:

```sh
kaupo run xstocks-open-orders --units rebased
kaupo run xstocks-open-orders --units base
```

Kraken defines `rebased` as underlying-equity units and `base` as SPV-token units.
The command sends `rebase_multiplier` and requests cursor pages of at most 100 orders.
See the [Kraken Open Orders API](https://docs.kraken.com/api-reference/account-data/get-open-orders).

The JSON output contains the observation interval, wallet scope, unit setting, and orders keyed by exchange order ID.
Order fields retain exchange symbols, volume strings, prices, and expiry values. The command does not translate symbols or convert units.
It includes all returned orders, including crypto orders. It does not infer asset class from a symbol.
An empty successful response produces an empty `open_orders` object.

The reader checks order IDs, descriptions, open or pending status, volumes, and expiry values.
It rejects invalid core fields, inconsistent volumes, API errors, and malformed cursors.
Identical orders on multiple pages are combined by ID. Changed orders cause an error.
Repeated cursors, pages without progress, and pagination beyond 100 pages also cause an error.
An error produces a nonzero exit and no success JSON. Transport error details are suppressed because they can contain signed requests.

These checks do not establish an atomic account snapshot.
Orders can change, fill, or disappear during pagination. The command cannot detect every such change.
The result is inspection evidence, not a reconciliation input or proof that an expiry was enforced.
Treat saved output as private account data.

The command does not place or cancel orders, write database records, or start a runner.
xStocks live assignments remain disabled. Fill pagination, symbol mapping, and corporate-action handling need further work.
Tests exercise signing and pagination with a mocked transport. No authenticated account request was made during implementation.
