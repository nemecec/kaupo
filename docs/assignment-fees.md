# Assignment fee schedules

The assignment API accepts optional `taker_fee_bps` and `maker_fee_bps` fields on creation and update.
One basis point equals 0.01 percent. A negative maker value represents a rebate.
These values configure the model; they do not change the fees charged by an exchange.

This update sets an example schedule:

```http
PUT /api/v1/assignments/example-shadow
Content-Type: application/json

{"taker_fee_bps": 10, "maker_fee_bps": -2}
```

Use the rates applicable to your instrument and account. The example does not select a fee tier automatically.
A portfolio assignment uses one schedule for its entire universe.

Taker fees must be finite, at least zero, and below 10,000 basis points.
Maker fees must be finite and strictly between -10,000 and 10,000 basis points.
Zero is an explicit fee override.

Omitting a fee field during an update preserves its stored value. Setting it to null restores the corresponding global default:

```json
{"maker_fee_bps": null}
```

The defaults come from `KAUPO_DEFAULT_TAKER_FEE_BPS` and `KAUPO_DEFAULT_MAKER_FEE_BPS`.
Migration `0021` adds nullable fee columns. Existing assignments retain their default behavior.
The assignment API returns the stored overrides. Run configurations record the effective rates.

The supervisor passes effective fees to single-pair shadow, portfolio shadow, and live requests.
The shadow venue and sizing risk model use those rates. Live fills retain the exchange's actual fees.
Changing an effective rate stops and restarts the affected run through normal reconciliation.
The fee change does not change ledger resume identity. Compatible runs retain cash and positions through the existing resume path.

Rolling-origin replays use the assignment's effective schedule. Each recorded run in the replay chain must have matching maker and taker rates.
Missing or mismatched rates produce `unknown` with `verdict_reason: fee_schedule_mismatch` and `fee_mismatch_run_ids`.
The check includes the replay prefix before the comparison window. No backtest runs for that entry.
A fee change can therefore leave a resumed chain unassessed; the current replay cannot reproduce historical changes in fee schedules.
Do not rewrite recorded fee metadata to remove this guard.

Forward-trial admission still requires positive reference fees at or above the global defaults.
Zero fees, maker rebates, and lower fee schedules do not qualify under that policy.
Assignment overrides do not change this evidence requirement.
