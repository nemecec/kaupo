# Account equity currencies

`GET /api/v1/equity/account` returns a stitched curve for one strategy and mode.
Use `quote_currency` to select the curve's denomination:

```text
/api/v1/equity/account?strategy=momentum-rotation&mode=shadow&quote_currency=USD
```

The filter accepts lowercase or uppercase currency codes. It excludes runs with another currency or unknown currency metadata.
The response remains an array of equity points. A known strategy without matching history returns an empty array.
An unknown strategy returns HTTP 404.

Without a currency filter, all runs with snapshots must share one known quote currency.
Mixed or unknown currencies return HTTP 409 with the available known currencies. Runs without snapshots do not make a request ambiguous.
Existing single-currency requests retain their response format and stitching behavior.

The run list and run detail APIs expose `quote_currency` from the configured pair or portfolio universe.
The dashboard selects the newest shadow run's currency and includes it in the account-equity request, cache key, and chart title.
If that run has no known currency, the dashboard shows an explanation instead of requesting an unfiltered curve.

Within the selected currency, fresh runs retain the existing rebase behavior. Resumed runs retain their absolute equity values.
Overlapping runs retain the existing later-run preference. Runs in other currencies cannot replace or offset selected points.

This curve is a historical strategy view, not a sum of independent accounts.
Currency filtering does not separate assignments that share a currency or convert currencies.
