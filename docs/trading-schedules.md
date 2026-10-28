# Assignment trading schedules

An assignment can specify `trading_schedule` through `POST` or `PUT /api/v1/assignments`.
The schedule controls the supervisor watchdog, polling diagnostics, and closure explanations in daily reports.
It also controls return sampling in backtests and rolling reports.
A missing or null schedule keeps the existing 24/7 behavior.

The schedule contains three fields:

- `timezone`: An IANA timezone name, such as `America/New_York`
- `week`: Local trading intervals for each weekday, numbered from Monday (`0`) through Sunday (`6`)
- `overrides`: Replacement intervals for specific local dates

Each interval has `start` and `end` values in minutes after local midnight.
The start is inclusive. The end is exclusive and can equal `1440`.
An omitted weekday is closed. An empty override closes that date.
Intervals cannot overlap. Split overnight sessions at midnight.
The week must contain at least one open interval.

This example describes hypothetical weekday hours, one early close, and one holiday.
It is not an exchange calendar or an xStocks recommendation.

```json
{
  "trading_schedule": {
    "timezone": "America/New_York",
    "week": {
      "0": [{"start": 570, "end": 960}],
      "1": [{"start": 570, "end": 960}],
      "2": [{"start": 570, "end": 960}],
      "3": [{"start": 570, "end": 960}],
      "4": [{"start": 570, "end": 960}]
    },
    "overrides": {
      "2026-12-24": [{"start": 570, "end": 780}],
      "2026-12-25": []
    }
  }
}
```

For portfolio assignments, specify hours that cover every instrument in the universe.
Maintain holiday and early-close overrides for the actual venue.
Kaupo does not infer schedules from symbols or download exchange holidays.

If a PUT request omits `trading_schedule`, the stored schedule stays unchanged.
An explicit null removes it. Schedule changes apply on the next supervisor check without restarting the run.

## Watchdog behavior

The watchdog counts UTC candle intervals that overlap an open session.
A partial session still counts as a whole candle interval.
This rule supports hourly, four-hour, and daily candles without treating a short trading day as a fraction of a daily candle.
Overlapping candle intervals count once.

The existing threshold remains two candle intervals plus ten minutes for delivery.
The delivery allowance uses wall time, including closed hours.
A healthy Friday candle therefore survives the weekend.
A missing final Friday candle can still trigger the watchdog after Friday closes.

Before the task creates its own run row, startup uses wall time.
After that row exists, the run can wait through a closure for its first candle.
Existing behavior for a task with no run row remains unchanged.

Timezone rules determine daylight-saving offsets.
For repeated local times, sessions open at the first occurrence and close at the last occurrence.
Missing local boundary times move forward by the daylight-saving gap.

The schedule does not restrict order submission or change order expiry.
Live limit expiry uses the separate [GTD safeguard](live-order-expiry.md).
A schedule does not enable xStocks live routing.

## Polling diagnostics

Live and shadow pollers use the schedule when deciding whether to warn about a missing candle.
A healthy final Friday candle produces no owed-candle warnings during the weekend.
A missed final Friday candle still produces warnings after its delivery deadline.
The poller allows two minutes for delivery after the next expected candle closes.

Shadow startup also uses the schedule for stale warm-up-tail warnings, including portfolio runs.
The warning about insufficient warm-up history still uses the number of available candles.

The supervisor refreshes schedules on each reconciliation pass.
Schedule edits and clearing reach existing pollers without restarting their runs.
Portfolio pollers share the assignment schedule; there is no separate schedule for each pair.

Polling frequency, retries, and candle delivery stay unchanged.
Pollers continue to fetch during closures and accept candles outside configured hours.
The schedule does not filter market data or suppress fetch-error diagnostics.

## Daily activity reports

Daily reports distinguish three activity states:

- `active`: The report contains snapshots or fills for the UTC day
- `market_closed`: The schedule explains the absence of observations for the entire UTC day
- `inactive`: The report has no observations and cannot establish a scheduled closure

The existing `active` flag still means observed activity.
Only observed activity contributes to financial totals.
Closure rows include `last_snapshot_at`; they do not invent a new equity value or daily P&L.
The digest and UI show separate active, market-closed, and inactive counts.
These states describe recorded activity, not current process health.

A closure requires an enabled, matching assignment and a prior snapshot that passes the schedule-aware watchdog check.
A missing final Friday candle remains inactive throughout the weekend.
A run without a prior snapshot remains inactive because the report lacks evidence of progress.
Observed snapshots or fills always take priority over the schedule.

Assignment history is not versioned.
The current assignment must predate both the report day and its last prior snapshot.
An assignment edit after either point makes the schedule evidence unavailable, even when that edit changes another field.
Regenerating an old report after such an edit can replace `market_closed` with `inactive`.
The `activity_reason` field explains the result; the report does not guess which schedule applied in the past.


## Backtest and rolling-report returns

Backtest requests accept the same optional `trading_schedule` object.
For the CLI, pass `--trading-schedule schedule.json`.
The JSON file contains the schedule itself, without the outer `trading_schedule` field.
Single-pair and portfolio runs store the schedule in their run config.

With an explicit schedule, Sharpe and Sortino use UTC daily equity returns.
Each daily boundary uses the latest available candle close.
Snapshot timestamps identify candle opens, so a snapshot becomes available one timeframe later.
Partial first and last days do not contribute returns.

The sampler carries the last value through configured closures, including weekends and holidays.
A missing candle during scheduled hours prevents this carry.
Hourly intervals that overlap part of a session count as expected candles.
Unavailable ratios are null, with a reason in `return_sampling.reason_code`.
At least two daily returns are necessary.

The `utc_daily_v1` method uses 365.25 periods per year and a zero return target.
Sharpe retains the sample standard deviation of returns.
Sortino retains Kaupo's existing sample standard deviation of negative returns.
Zero denominators produce zero, as before.
This change does not redefine the Sortino denominator.

Annualization multiplies each ratio by the square root of 365.25.
This is a reporting convention for equal calendar-day intervals.
It does not correct for serial correlation.
See [Sharpe's discussion of time dependence](https://stanford.edu/~wfsharpe/art/sr/sr.htm).

Rolling reports apply the current assignment schedule to both shadow and replay equity.
Each entry records this analysis basis as `metric_schedule`.
It does not establish the historical execution schedule.
Coverage counts expected candle intervals, while the minimum chain age remains seven calendar days.
Missing scheduled candles produce an unknown verdict.

Without an explicit schedule, existing per-candle metrics stay unchanged.
P&L, drawdown, CAGR, and trade statistics continue to use the original equity and fills.
Schedules do not filter backtest candles or restrict simulated orders.
Other report types retain their existing metric calculations.

## Daily orderflow sessions

Daily orderflow rows describe a UTC day, with optional schedule metadata:

- `session_state`: `scheduled_open`, `scheduled_closed`, or `unknown`
- `scheduled_open_seconds`: Actual session seconds within the UTC day, or null without a recorded schedule
- `trading_schedule`: The explicit schedule used for that row, or null

A scheduled-open day contains at least one open interval.
A scheduled-closed day contains none.
Holiday overrides, early closes, and daylight-saving offsets determine the duration.
The duration uses actual session overlap, without rounding to candle intervals.
A session can cross a UTC boundary even when its local date stays unchanged.

These fields describe expected hours, not collection completeness.
Zero collected trades on a scheduled-open day do not establish zero market volume.
Observed trades and book snapshots remain in the row, including observations outside configured hours.
The schedule never removes observations or changes their totals.
Legacy rows and new rows without a schedule have an unknown session state.

To record a schedule, pass one pair explicitly:

```sh
kaupo ingest orderflow-rollup --exchange kraken --pair TEST/USD \
  --day 2026-10-24 --trading-schedule schedule.json
```

`TEST/USD` is a placeholder for the actual pair.
The JSON file contains the schedule itself, as in the backtest option.
The command writes a row even when that pair has no raw observations.
It requires exactly one `--pair` with `--trading-schedule`.
For scheduled collection, run this command daily for each pair with its applicable schedule.
The existing refresh command supplies no schedule and leaves new rows unknown.

An ordinary rerun retains the recorded schedule for that exchange, pair, and day.
An explicit schedule replaces that day's schedule and recomputes its open seconds.
The rollup does not infer schedules from assignments, symbols, other pairs, or adjacent days.
Direct aggregate imports without schedule metadata also retain an existing session basis.
Omitting metadata does not clear it.

As before, a rerun replaces aggregates with the raw observations still in storage.
Rerunning an old day after raw-data retention can erase its historical totals.
The schedule option does not make that operation a metadata-only update.

The data API and both forms of `ctx.tick_flow_daily` expose these fields.
Providers return a row only after its UTC day ends, including scheduled-closed rows.
Closed rows count toward the requested number of rows.
The fields reflect the latest stored rollup, not a versioned history of schedule changes.
