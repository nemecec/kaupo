# Risk state across restarts

Shadow and live runs save a versioned risk checkpoint in `runs.risk_state`.
A compatible successor restores the checkpoint with its ledger.
This includes single-pair shadow, portfolio shadow, and single-pair live runs.
Backtests keep their existing fresh-run behavior.

The checkpoint contains:

- The last observed UTC day
- The daily starting equity floor and the last observed floor
- The consecutive-loss count and remaining cooldown candles
- The halt flag and reason

The new request supplies the risk limits and fee assumptions.
Restoring history does not restore old configuration or erase history when a limit changes.
Rejection messages remain per-run diagnostics and do not carry forward.

## Daily loss and cooldown

A same-day restart retains the original daily baseline.
On a later day, the first observation uses the previous observed floor as its baseline.
Thus, a weekend restart does not discard the opening loss.
A closure does not consume cooldown candles. Each processed candle or portfolio step advances the cooldown once.

Live reconciliation applies recovered closing trades to the restored loss streak.
It retains the previous floor until the next candle evaluates the updated ledger.
Startup saves the restored state even when no new candle arrives.
An empty successor can therefore pass the risk history to another successor.

## Durability boundary

The recorder marks risk state incomplete before each trading candle or portfolio step.
This marker commits before fills or snapshots from that step can persist.
After successful processing, the recorder saves the complete checkpoint and flushes remaining rows in the same transaction.
Warm-up only populates history and does not advance risk state.

A crash during processing can leave recorded fills with an incomplete checkpoint.
The successor refuses automatic resume in that case.
It also refuses missing required checkpoints, malformed checkpoints, unknown versions, and persisted risk halts for the same run identity.
These errors do not fall through to a new ledger or a fresh risk baseline.
The logs identify the predecessor that requires review.

This policy favors an explicit stop over continuing with inconsistent accounting and risk history.
It does not make exchange operations and database writes one atomic transaction.
Existing live reconciliation remains necessary for exchange fills that were not recorded before a crash.

The recorder retains its buffers after a failed commit.
Retrying a complete checkpoint write replaces the same run field, including after an ambiguous commit result.
The additional checkpoint writes apply only to shadow and live execution.

## Compatibility and limits

Migration 0024 adds a nullable column. Existing run rows remain null.
A legacy predecessor without the checkpoint-version marker can still resume its ledger with fresh risk history.
That transition logs a warning because the previous baseline cannot be recovered from a checkpoint that never existed.
The successor then records checkpoints for later restarts.

Existing run identity checks still apply to strategy behavior, pair or universe, timeframe, and parameters.
A changed identity starts a separate chain under the existing rules.
Control stops and other resume eligibility rules remain separate from risk checkpoint validation.
Pending orders, strategy-local state, and stop-loss/take-profit arms do not gain persistence through this change.

Validation uses synthetic candle gaps, restarted engines, PostgreSQL checkpoints, and fake Kraken reconciliation.
It does not establish xStocks routing or account eligibility. See [non-24/7 support status](non-24-7-status.md).
