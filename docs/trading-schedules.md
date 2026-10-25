# Assignment trading schedules

An assignment can specify `trading_schedule` through `POST` or `PUT /api/v1/assignments`.
The schedule controls the supervisor watchdog.
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
It does not change polling, daily activity reports, return sampling, or metric annualization.
Those parts of issue #43 remain separate work.
