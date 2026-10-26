# Scheduled xStocks refresh

`kaupo ingest xstocks-refresh` updates public xStocks candles and reports coverage against explicit calendars.
It uses the dedicated Kraken OHLC client. It does not request Binance candles, funding, open interest, or archives.

The command accepts a JSON configuration with one entry per pair.
Each entry requires a list of timeframes and a trading schedule.
The same pair cannot appear twice. The command rejects unknown configuration fields before database or exchange access.

## Configure the refresh

Create `/etc/kaupo/xstocks-refresh.json` on the host.
Set the pairs, timeframes, and verified calendars for the intended instruments.

This example uses **synthetic weekday hours**, not verified xStocks hours.
The empty override demonstrates a closed local date.

```json
{
  "days": 3,
  "instruments": [
    {
      "pair": "AAPLX/USD",
      "timeframes": ["1h", "4h", "1d"],
      "trading_schedule": {
        "timezone": "UTC",
        "week": {
          "0": [{"start": 0, "end": 1440}],
          "1": [{"start": 0, "end": 1440}],
          "2": [{"start": 0, "end": 1440}],
          "3": [{"start": 0, "end": 1440}],
          "4": [{"start": 0, "end": 1440}]
        },
        "overrides": {"2026-12-25": []}
      }
    }
  ]
}
```

Schedules use local minutes and IANA time zones. Date overrides replace the entire local day's sessions.
The [schedule guide](trading-schedules.md) explains overnight sessions and daylight-saving transitions.
Refresh schedules are independent of assignment schedules. Both require maintained hours and date overrides.

## Run the command

Configure `KAUPO_DATABASE_URL` for the target database. Then run:

```sh
uv run kaupo ingest xstocks-refresh --config /etc/kaupo/xstocks-refresh.json
```

The command writes one JSON document to standard output. Logs use standard error.
The result includes the checked time, source, per-target range, schedule, imported count, coverage, and errors.

| Result | Meaning | Exit status |
| --- | --- | --- |
| `complete` | Every scheduled bucket in the range has a stored candle, with no observations outside the schedule. | 0 |
| `scheduled_closed` | The range has no scheduled buckets and no stored candles. | 0 |
| `missing` | At least one scheduled bucket has no stored candle. | 1 |
| `schedule_mismatch` | Stored candles occur outside scheduled buckets or UTC candle boundaries. | 1 |
| Non-null `error` | Discovery, fetch, storage, or coverage processing failed for this target. | 1 |
| Invalid configuration | The command cannot read or validate the configuration. | 2 |

One failed target makes the overall exit status nonzero. Other targets still run.
The command fetches data even during scheduled closures. A failed fetch remains an error even when cached candles cover the range.

Coverage includes stored history and the newly imported candles.
A bucket qualifies when its UTC interval overlaps an open session.
Missing observations do not prove an outage: an open instrument can have no trades.
The result includes counts and up to ten missing timestamps for investigation. It does not create replacement candles.

## Schedule the host command

Run the optional wrapper from the deployed repository:

```sh
/opt/kaupo/deploy/refresh-xstocks.sh /etc/kaupo/xstocks-refresh.json
```

The wrapper mounts the configuration read-only into the API container.
It returns the command's exit status and JSON output.
The existing crypto refresh job retains its fixed crypto universe.

For a Linux host with `flock`, add a separate cron entry:

```cron
*/15 * * * * /usr/bin/flock -n /run/lock/kaupo-xstocks-refresh.lock /opt/kaupo/deploy/refresh-xstocks.sh >> /var/log/kaupo-xstocks-refresh.log 2>&1
```

Monitor the exit status and retain the JSON results for diagnosis.
The wrapper does not create a cron entry or change the deployed configuration.

## History limits

`days` defaults to 3 and accepts integers from 1 through 30.
Each timeframe uses a separate UTC-aligned window.
The end is the latest candle boundary at least two seconds before the checked time.
The current incomplete candle is outside the window.

Each target fetches only Kraken's newest OHLC page, which contains at most 720 bars.
A longer coverage window does not expand this source limit. Stored history can cover older buckets.
A successful coverage result establishes observation coverage under the supplied schedule, not source accuracy or trading permission.

For missing history, run the [historical-trade importer](xstocks-data.md#import-historical-candles) with an explicit range.
Then run the refresh again to inspect coverage.
History before an instrument's first available trade remains unavailable.
This command does not refresh raw trades, orderflow rollups, or authenticated account data.
