# Assignment trading schedules

An assignment can specify `trading_schedule` through `POST` or `PUT /api/v1/assignments`.
The schedule controls the supervisor watchdog, polling diagnostics, and closure explanations in daily reports.
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
It does not change return sampling or metric annualization.
Those parts of issue #43 remain separate work.

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
