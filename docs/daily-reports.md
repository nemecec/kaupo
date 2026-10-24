# Daily report currencies

`GET /api/v1/reports/daily` reports live and shadow activity for one UTC day.
Each run includes `quote_currency`, derived from its configured pair or the shared quote currency of its portfolio.
Missing or inconsistent pair configuration produces a null currency.

`totals.by_currency` contains separate P&L and fee amounts for each currency. It does not convert currencies or add amounts across currencies.
Run and fill counts remain available across the whole report.
The dashboard, daily report page, and notification show these separate amounts.

Each run also includes `fees_by_currency`. Explicit exchange fee currencies take precedence over the pair's quote currency.
Legacy and paper fills without a fee currency use their pair's quote currency.
Small crypto-denominated fees retain their precision instead of rounding to two decimal places.

A fill marked `mixed` does not identify its fee denominations. Its fee amount stays outside the currency totals.
`unclassified_fee_fills` counts these fills within each run.
`totals.unclassified_runs` counts active runs with unknown quote currencies or unclassified fees. The UI and notification flag these runs alongside known subtotals.

For compatibility, `totals.total_pnl` and `totals.total_fees` remain numeric when all amounts have one known currency.
`totals.quote_currency` names that currency. The scalar amounts are null for mixed or unknown currencies.
A day without activity retains zero scalar totals and an empty currency map.
A run's `fees_paid` is null when its fees cannot be expressed in its quote currency without conversion.

API consumers must handle null scalar amounts. Use `totals.by_currency` for monetary summaries.
The updated UI and notification omit monetary totals from older reports without currency metadata.
Regenerating a report replaces its stored body with the current format.

Equity-curve stitching remains separate work under #43. This change does not convert historical equity curves or change trade accounting.
