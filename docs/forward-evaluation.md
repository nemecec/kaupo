# Forward evaluation and research access

Kaupo separates research permission from live trading permission. A research token can submit backtests and manage shadow assignments. It cannot change live assignments, settings, or controls. Research admission stops at 12 enabled shadow assignments or six pending backtests. These shared limits do not reserve CPU or exchange capacity for live trading.

The forward evaluator collects evidence after registration. A successful result requests human review. It does not create a live assignment or increase capital.

## Operator constraints

- Eventual capital: EUR 10,000
- Outer loss tolerance: EUR 5,000 from initial capital
- Monthly research ceiling: EUR 100
- Profit calculation: Trading profit after recorded research expenses
- Excluded expenses: Hosting and other expenses

These values describe the evaluation policy. They do not change the live pilot's risk limits. The outer loss tolerance is not a guaranteed maximum loss.

## Register a candidate

1. Deploy migration `0019` and the new platform image.
2. Configure the research token as described in `deploy/README.md`.
3. Create a fresh EUR shadow assignment with EUR 10,000 starting cash. Give it a unique candidate id.
4. Wait for the supervisor to start its run.
5. Before its first order or fill, submit a hypothesis:

```http
POST /api/v1/research/trials
Authorization: Bearer <research-token>
Content-Type: application/json

{
  "assignment_id": "candidate-sol-1d",
  "hypothesis": "A specified mechanism should exceed execution costs on unseen observations."
}
```

The server fixes the start time, end time, policy, strategy identity, and execution configuration. Clients cannot backdate registration or lower thresholds. Registration rejects resumed runs and runs without execution provenance.

The policy fixes a 90-day window and requires 20 completed positions. Each completed position must reach EUR 500 in notional value. Partial exits do not count as separate positions. Historical backtest trades do not count. The server also checks equity coverage, Sharpe, drawdown, and net profit. Risk halt events invalidate the trial. Long data gaps also prevent a pass, even after a valid watchdog restart. Review the frozen daily loss limit separately against `promotion.yml`.

These thresholds are conservative screening defaults. They do not establish statistical significance or correct for repeated strategy selection.

## Read the result

```http
GET /api/v1/research/trials
GET /api/v1/research/trials/<id>
```

| Status | Meaning |
| --- | --- |
| `collecting` | The fixed evaluation window has not ended |
| `invalidated` | Provenance, ledger continuity, or data integrity failed |
| `insufficient_evidence` | The window ended without meeting all screening requirements |
| `review_required` | The screening requirements passed and human review is required |

A strategy behaviour change invalidates its trial. Fee, risk, execution, and engine changes also invalidate it. A strategy documentation change preserves identity when its behaviour hash stays unchanged.

Engine fingerprints cover execution syntax and key runtime dependency versions. Comments, documentation, API authentication, and reporting changes do not alter this identity. Execution changes still invalidate a trial. The Docker base image is pinned to prevent runtime changes during routine rebuilds. Keep execution releases stable during the evaluation window.

The evaluator preserves the original trading window. Later trades cannot improve its result. Reports are recomputed, so late invoices and cost attestations can change a verdict. Trial and cost records have no update or delete API.

Shadow fills remain simulated. The report cannot prove live execution quality. Human review must examine matched venue tests, selection bias, benchmark returns, and execution calibration.

## Record actual research costs

Only the admin token can record costs and attest cost coverage. Research agents cannot certify their own expenses.

```http
POST /api/v1/research/costs
Authorization: Bearer <admin-token>
Content-Type: application/json

{
  "reference": "provider-invoice-item-unique-id",
  "kind": "cost",
  "amount_eur": "12.50",
  "period_start": "2026-09-17T10:00:00Z",
  "period_end": "2026-09-17T10:00:00Z",
  "note": "Model research usage, converted to EUR at the invoice rate"
}
```

The cost timestamp identifies when the expense occurred. A unique reference prevents duplicate submissions. Amounts must be positive. Future expenses are rejected.

After checking all research expenses for a period, submit a coverage record:

```json
{
  "reference": "coverage-2026-09-17",
  "kind": "coverage",
  "amount_eur": "0",
  "period_start": "2026-09-01T00:00:00Z",
  "period_end": "2026-09-17T12:00:00Z",
  "note": "All research expenses in this period are recorded"
}
```

Missing coverage prevents a successful evaluation. Coverage cannot extend into the future. Consecutive coverage records can cover a longer period.

The standalone candidate report deducts all recorded research expenses within its evaluation window. Reports for different candidates must not be added together. That would count shared expenses repeatedly.

```http
GET /api/v1/research/costs
GET /api/v1/research/budget
```

The budget endpoint shows recorded spending and the remaining monthly allowance. It does not enforce provider spending. Configure provider limits separately. Missing invoices can make recorded spending incomplete.

## Turnover experiment

`plan_rebalance` accepts `weight_buffer`, an absolute fraction of equity around each positive target. Inside the band, no rebalance occurs. Outside the band, trades move to the nearest edge. Zero targets still exit fully.

The momentum-rotation strategy exposes this setting as `rebalance_buffer_pct`. Its default is zero, preserving existing decisions. A nonzero value is a research candidate, not a validated improvement. Adding the parameter changes the strategy behaviour hash. Deploying this code can restart its existing run chain.

Compare candidate and control on identical dates, venues, costs, and risk limits. Register a fixed candidate before its forward test. Do not tune its buffer using the forward window.

## Scope and remaining controls

This release does not automate live promotion, liquidation, or scaling. Existing live limits remain authoritative. It does not certify the EUR 5,000 tolerance as an enforced account-level stop.

The evaluator reports descriptive Sharpe, not a confidence percentage. Its registry preserves prospective trials, including failures. It does not reconstruct every historical research attempt.

Database administrators can change records directly. API immutability is not a cryptographic audit or an external broker attestation.

Agents can write to strategies main on the free GitHub plan. Production mounts an immutable snapshot selected by `deploy/strategies-ref` in the platform repository. Strategy CI cannot dispatch production deployments. Update the pin only after reviewing the candidate. Backtests use the pinned strategy catalog; new strategy code needs a reviewed pin update before cloud testing.
