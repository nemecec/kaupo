# Research costs and isolated execution

Research has a EUR 100 monthly budget. Hosting is excluded.
`recorded_spend_eur` is the sum of imported usage charges. Without complete coverage, `actual_spend_eur` and `remaining_eur` are null.
A prepaid deposit or current provider balance is not a usage charge.

Moonshot documents a current balance endpoint. It does not provide historical usage charges through that endpoint.
The operator must supply a billing export or actual charge totals for missing periods.
The import does not infer historical expenses from balances, token estimates, or deposits.

## Import actual charges

1. Export actual Kaupo usage charges from the provider billing page.
2. Normalize the export to the following CSV columns.
3. Supply an explicit EUR conversion rate for each non-EUR charge.
4. Run `python scripts/import_research_costs.py charges.csv` to preview the records.
5. Set `KAUPO_API_URL` and `KAUPO_ADMIN_TOKEN` in the operator environment.
6. Run `python scripts/import_research_costs.py charges.csv --apply` to import the charges.
7. Attest coverage through `/api/v1/research/costs` only after every charge in that period is included.

```csv
reference,kind,occurred_at,amount,currency,eur_per_unit,source
provider-charge-id,usage,2026-09-17T12:00:00Z,10.00,EUR,1,provider billing export
```

The example is fictional. Do not import it as an actual expense.
Provider references must be unique. Duplicate imports fail rather than double-count charges.
The import stops on an error. Already imported rows remain recorded, so remove them before retrying a partial batch.
Coverage uses `kind: coverage`, `amount_eur: 0`, explicit period boundaries, and a source note.
Research credentials cannot import charges or attest coverage.

## Release isolation

`deploy/trading-ref` fixes the supervisor image. `deploy/strategies-ref` fixes the strategy files that it loads.
An API deployment can add research features without changing either pin.
A trading pin change still requires a deliberate release and a review of active trial contracts.

The private strategy repository contains the `Research experiment` workflow and its contract guide.
Each candidate runs in an offline container against a separate disposable database.
Only bounded public market data enters that container. Production credentials remain in separate preparation and reporting jobs.
The registry preserves contracts, dataset snapshots, result summaries, and evidence hashes. Cloud artifacts retain full evidence for 90 days.
Historical researcher reports do not authorize live trading or prove future profit.
