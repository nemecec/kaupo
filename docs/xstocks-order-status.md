# Inspect specific orders

Use exact Kraken order IDs to inspect orders after they leave the open-order list:

```sh
kaupo run xstocks-order-status --units base --order-id OEXAMPLE-ORDER1-ABCDEF --order-id OEXAMPLE-ORDER2-ABCDEF
```

Repeat `--order-id` for up to 50 distinct IDs. Comma-separated values and whitespace are rejected.
Choose `base` or `rebased` explicitly. The command preserves exchange symbols, quantities, timestamps, and other returned fields.
It reads the default wallet and does not infer asset class from a symbol.

The command requires `KAUPO_KRAKEN_API_KEY` and `KAUPO_KRAKEN_API_SECRET` through the existing secret configuration.
The key needs permission to query open or closed orders and trades, depending on each order's status.
It calls [Query Orders Info](https://docs.kraken.com/api-reference/account-data/query-orders-info) with explicit units and individual trade references enabled.

Output includes the requested IDs, observation interval, unit mode, and raw orders keyed by exchange ID.
The reader accepts `pending`, `open`, `closed`, `canceled`, and `expired` states.
Terminal orders require valid opening and closing timestamps in chronological order.
Volumes, descriptions, expiry values, and returned trade references must also pass validation.
Missing or additional order IDs cause an error. The command does not return partial success.
Invalid requests fail before network access. API errors and transport failures produce exit code 1 without success JSON.

A missing order is not evidence that it expired, filled, or was canceled.
An expired or canceled order can have partial fills. Inspect its executed volume and returned trade IDs.
Trade references are preserved. The optional fill check below can fetch those references and compare their volumes.
The exchange status and timestamps are inspection evidence; the command does not assert that an expiry deadline was enforced correctly.
Results do not form an atomic account snapshot. Active orders can change after the request.

The command does not submit or cancel orders, update database records, or start a runner.
Tests use mocked transport. No authenticated account request was made during implementation.
Live xStocks trading remains disabled.

## Compare an intended expiry

For one limit order, supply the intended absolute expiry timestamp:

```sh
kaupo run xstocks-order-status --units base --order-id OEXAMPLE-ORDER1-ABCDEF --expected-expiry 1792972800
```

Use the deadline recorded when the order was submitted. The command labels this value as operator evidence.
The option requires exactly one order ID and a positive integer Unix timestamp.
The recorded `expiretm` must equal the supplied deadline. Market and conditional order types require separate interpretation.

The `expiry_check` report preserves the executed volume, including partial fills.
It reports `terminal_by_deadline` when an order closed or was canceled at or before the deadline, or expired exactly at it.
This result means that the reported order was terminal by the deadline. It does not prove expiry caused the terminal state.
An expired state before the intended deadline requires review.

All other outcomes produce `needs_review` and exit code 1 after the JSON report:

- `expiry_mismatch`: the recorded expiry differs from the intended deadline.
- `terminal_after_deadline`: the reported terminal timestamp is later than the deadline.
- `active_after_deadline`: the order remained active in a request that started after the deadline.
- `observation_overlaps_deadline`: the request interval touches or spans the deadline while the order is active.
- `deadline_not_reached`: the active order was observed before the deadline.

Malformed evidence, unsupported order types, and inconsistent timestamps also require review.
The checker applies no assumed grace period. Even a fractional late timestamp requires review.
Observation times come from the local host; exchange timestamps come from Kraken. Clock differences can affect the result.
A matching report does not establish trade execution times, complete fills, or correct account reconciliation.
Without this option, the command retains its existing inspection output and exit behavior.

## Check referenced fills

To fetch an order's trade references and check executed volumes and timestamps, add `--check-fills`:

```sh
kaupo run xstocks-order-status --units base --order-id OEXAMPLE-ORDER1-ABCDEF --check-fills
```

The command calls [Query Trades Info](https://docs.kraken.com/api-reference/account-data/query-trades-info) in batches of at most 20 IDs.
It uses the same unit mode and default wallet as the order request.
The key needs permission to query closed orders and trades.
Kaupo limits the lookup to 2,000 trade IDs across the requested orders.
Each trade must belong to its referencing order and have the same side.
Missing or additional trade IDs, shared references, or failed batches cause an error without success JSON.
An order with nonzero executed volume must have trade references. A zero-execution order can have none.

Output adds raw `fills`, `fill_check` and `fill_time_check` reports, and the completion time of the initial order request.
Each order's report compares the sum of referenced fill volumes with its reported `vol_exec`, using exact decimal arithmetic.
Mismatches and unsupported precision produce `needs_review` and exit code 1 with the evidence.
A successful report uses `volumes_match` and scope `referenced_order_fills`.
Fees, costs, symbols, and returned ledger references remain unchanged. The check does not reconcile fees or infer symbol aliases.

Order and fill requests are separate observations. Active orders can change between requests.
A volume match does not prove complete account history or corporate-action treatment.
The optional expiry check uses the initial order observation interval, excluding time spent fetching fills.
If both checks are requested, either `needs_review` result causes exit code 1.

### Check fill timestamps

The `fill_time_check` report compares each referenced fill timestamp with its order's opening time.
For closed, canceled, and expired orders, it also checks the closing time.
If `--expected-expiry` is supplied, every referenced fill must be at or before that intended deadline.
This check uses the supplied deadline even if the recorded expiry differs. The separate expiry report checks that difference.

Boundary timestamps are inclusive. A fill exactly at opening, closing, or expiry is within the corresponding bound.
The report preserves each timestamp and lists all applicable violations: `fill_before_open`, `fill_after_close`, and `fill_after_expected_expiry`.
Malformed or unsupported timestamps are unresolved. Any violation or unresolved result produces `needs_review` and exit code 1.
A volume match or a successful order-expiry check does not override a timing failure.

The success status is `times_match`, scoped to the referenced fills. Empty references report `no_fills`, not timing evidence.
Active orders have no closing bound. Without an explicit expected expiry, this report checks only their opening bound.
It does not infer a deadline from the symbol or treat `expiretm=0` as an expiry.
The report compares Kraken's returned timestamps without an assumed tolerance.
A match does not prove timestamp accuracy, complete fill history, or correct fees.

## Check principal movements and fees

To inspect the ledger entries referenced by an order's fills, add `--check-ledgers`:

```sh
kaupo run xstocks-order-status --units base --order-id OEXAMPLE-ORDER1-ABCDEF --check-ledgers
```

This option includes `--check-fills`. It fetches referenced ledger entries in the same unit mode and default wallet.
The key also needs permission to query ledger entries.
Current public xStocks metadata supplies exact pair and asset mappings. Unknown aliases remain unresolved; the command does not guess them.

Output adds raw `ledgers`, `identities`, and a `ledger_check` report keyed by trade ID.
The check compares spot principal movements with each fill's volume and cost.
It reports actual ledger fees and net movements by asset, including signed rebates.
Fee-only entries in other assets retain their exact Kraken IDs. Third-asset principal movements remain unsupported.
The fill's quote-currency fee estimate is preserved, but it does not replace ledger fee evidence.

This command reuses the [fill-history ledger checks](xstocks-fills.md), including reference, margin, identity, and decimal-precision restrictions.
Every referenced ledger entry must be returned. A failed lookup or metadata request produces an error without success JSON.
A mismatch or unresolved ledger check produces `needs_review` and exit code 1 with the evidence.
Volume, timestamp, and optional expiry checks retain their own reports. A review result from any check causes exit code 1.

An order without executions produces empty fills and ledgers, with `no_fills` ledger status.
That case does not need a public metadata request and does not establish reconciliation readiness.
This command reads only entries referenced by the selected orders' fills. It does not establish complete account balance continuity.
Order, fill, ledger, and metadata requests are separate observations. Corporate actions and historical aliases still require separate treatment.

Ledger mapping requires both the exact asset ID and the asset class declared in public metadata.
A known ID with a different or missing class stays unmapped and appears in `identities.ledger_class_mismatches`.
Such an entry makes its fill unresolved, including when the entry contains only a fee.
Missing quote-class metadata or conflicting classes for the same raw asset ID cause an error; no class is assumed.
