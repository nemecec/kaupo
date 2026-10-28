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
Trade references are preserved, but this command does not fetch or reconcile those fills.
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
