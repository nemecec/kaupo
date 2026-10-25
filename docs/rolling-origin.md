# Rolling-origin assessment and missing history

The report compares a shadow chain with a replay of its strategy. The comparison window usually covers the latest 30 days.
The replay starts at the first recorded decision of the original run. It includes earlier decisions that establish positions carried into the comparison window.

## Why a verdict becomes unknown

PR #56 added a conservative origin check. An incomplete chain can no longer produce a verdict from a replay that starts with fresh cash.
This can change an old `lags` or `tracks` result to `unknown` without a strategy change or a new deletion.
The report cannot determine why a run is absent. Missing history alone does not prove that a retention job deleted it.

The `verdict` field retains its existing values. Every new `unknown` entry also has a stable `verdict_reason` code.
The `shadow.note` field explains the reason. The daily digest shows both the verdict and the code.
An `unknown` result is neither a kill signal nor an all-clear. Automation must treat the assignment as unassessed.

| `verdict_reason` | Meaning |
| --- | --- |
| `missing_chain_ancestor` | A retained run references an absent parent. |
| `fee_schedule_mismatch` | Recorded chain fees differ from the replay schedule or are unavailable. |
| `chain_cycle` | The resume links contain a cycle. |
| `missing_origin_equity` | The original retained run has no equity observations. |
| `no_shadow_runs` | The assignment has no shadow run. |
| `insufficient_shadow_observations` | The comparison window has fewer than two shadow observations. |
| `insufficient_shadow_coverage` | The chain is too young or its observed window is too short. |
| `incomplete_replay_history` | The replay does not start at the original recorded decision. |
| `insufficient_common_observations` | The curves share fewer than three timestamps. |
| `insufficient_common_coverage` | The shared observations cover too little of the window. |
| `invalid_common_equity` | Shared equity contains a non-positive or non-finite value. |
| `invalid_common_sharpe` | A Sharpe calculation produces a non-finite value. |

Each entry also includes a `lineage` object. Its `status` describes the retained ancestry and the availability of the first observation.
The object identifies the latest run, the oldest retained run, and the unresolved parent.
A `complete` status does not certify every historical snapshot or strategy version. Comparison checks still apply.
Structural lineage problems take precedence over a short comparison window.

An incomplete chain produces this shape:

```json
{
  "verdict": "unknown",
  "verdict_reason": "missing_chain_ancestor",
  "backtest": null,
  "lineage": {
    "status": "missing_chain_ancestor",
    "tip_run_id": "current-run",
    "oldest_retained_run_id": "oldest-retained-run",
    "unresolved_parent_run_id": "absent-parent",
    "retained_run_count": 3,
    "first_observed_decision": "2026-09-16T04:00:00+00:00"
  }
}
```

Stored reports from earlier versions do not have these fields. The digest labels their unknown reason as `unspecified`.
A report rerun replaces the row for the same ISO week. It does not reconstruct absent history.

## Retain enough history

The repository has no automatic retention job for run rows. Trade and book retention settings do not govern run lineage.
A future cleanup process must preserve these records for every active resume chain:

- All ancestor run rows, including their original configuration and resume links.
- Equity observations from the original decision through the latest run.
- Orders, fills, and ledger records needed for accounting and replay validation.
- Source candles and required strategy inputs from the original decision, including indicator warm-up.

Keeping only the latest 30 days is insufficient. The replay needs earlier decisions even when the report compares a later window.

## Investigate an unknown result

1. Read `verdict_reason` in `GET /api/v1/reports/rolling-origin?period=2026-W43`.
2. For a lineage problem, inspect `lineage.oldest_retained_run_id` and `lineage.unresolved_parent_run_id` in the database.
3. Check the backup or archive for the original ancestor records and observations.
4. If an authoritative backup exists, restore the missing history through the normal database recovery process.
5. After recovery, run `kaupo report rolling-origin` again.

A run-list page cannot establish whether a parent exists. Pagination limits can omit older runs from that response.
Waiting for new candles does not repair a missing ancestor or a resume cycle.

Without an authoritative history source, the comparison remains unknown. Editing `resumed_from` or inventing an opening balance does not restore historical decisions.
A fresh evaluation chain establishes new evidence, but it does not repair the old comparison.

See [assignment fee schedules](assignment-fees.md) for overrides, restart behavior, and the fee comparison guard.
