# xStocks order checks

The order-check command prepares a spot limit order from public xStocks market rules.
It can also ask Kraken to validate the request for the configured account.
It does not enable supervised live trading or create a run in the database.

## Check public rules

Run the command with the intended pair, side, quantity, and limit price:

```sh
uv run kaupo run xstocks-order-check \
  --pair AAPLX/USD --side buy --size 0.1 --price 100 --expires-in 3600
```

The default command calls public market discovery only. It needs no account credentials.
The prices and quantities in this example are illustrative, not trading recommendations.

The JSON output contains `status: public_rules_checked` and the normalized validation request.
This status confirms only local checks against the public metadata. It does not establish account access or exchange acceptance.

The command uses `lot_decimals`, `tick_size`, `ordermin`, and `costmin` from discovery.
Kraken documents these market rules through [AssetPairs](https://docs.kraken.com/api-reference/market-data/get-tradable-asset-pairs).
Missing or invalid rules cause an error. The command accepts unit lots with `lot_multiplier=1` only.

Quantity rounds down to the lot step. Buy prices round down to the tick, and sell prices round up.
The command checks minimum quantity and cost after rounding.
It accepts the `online`, `post_only`, and `limit_only` statuses for its post-only limit request.

Numeric inputs and rules must be between `1e-18` and `1e18`, with at most 36 significant digits.
Lot precision must be an integer from 0 through 18.
The command accepts expiry durations from 30 seconds through 28 days and sends an absolute GTD timestamp.
These are conservative tool limits.

## Request authenticated validation

Configure `KAUPO_KRAKEN_API_KEY` and `KAUPO_KRAKEN_API_SECRET` through the existing environment configuration.
Then add `--authenticate`:

```sh
uv run kaupo run xstocks-order-check \
  --pair AAPLX/USD --side buy --size 0.1 --price 100 --expires-in 3600 \
  --authenticate
```

The command sends one signed `AddOrder` request with `asset_class=tokenized_asset`, `oflags=post`, GTD expiry, and `validate=true`.
Kraken documents `validate=true` as validation without matching-engine trading.
The endpoint requires the API key's create/modify-order permission. See [Add Order](https://docs.kraken.com/api-reference/trading/add-order).

The validation flag is fixed. The command has no option that places a live order.
It does not use the ordinary crypto market adapter, retry the request, or change the live-trading switch.
The installed ccxt adapter supplies authentication and transport only.

A successful response produces `status: exchange_validated` and the exchange's order description.
A rejection, malformed response, or unexpected order ID produces a nonzero exit status.
Transport errors show the exception class without signed request details or credentials.
The CLI returns status 2 for invalid arguments and status 1 for a failed check.

## Evidence and limits

An authenticated validation result applies to that request and that account at the checked time.
It does not verify actual fills, cancellation, reconciliation, fee currency, or balance handling.
The API and supervisor continue to reject xStocks live assignments.

On October 26, 2026, a public discovery check returned the required rule fields for `AAPLX/USD`.
The tests check the signed request body with a mocked transport, including the lowercase `validate=true` value.
No authenticated request was sent during development, so account eligibility remains unverified.
