# Inspect xStocks account balances

Use this command to inspect the default Kraken wallet before developing or validating account reconciliation.
It requires `KAUPO_KRAKEN_API_KEY` and `KAUPO_KRAKEN_API_SECRET` with Funds Query permission.
Use the existing secret configuration. Do not put credentials in command arguments.

Choose the xStocks unit explicitly:

```sh
kaupo run xstocks-balances --units rebased
kaupo run xstocks-balances --units base
```

Kraken defines `rebased` as underlying-equity units and `base` as SPV-token units.
The command sends this choice as `rebase_multiplier` to the private Balance endpoint.
See the [Kraken Balance API](https://docs.kraken.com/api-reference/account-data/get-account-balance).

The JSON output contains the request time interval, unit setting, wallet scope, and balances.
Values retain their original decimal strings. Asset identifiers remain unchanged, including `.T` and Earn suffixes.
The response includes all returned assets, including currencies. It does not filter balances by guessed symbol suffixes.
Missing assets remain absent. An empty successful response produces an empty balance object.
Malformed amounts, non-finite values, API errors, and transport failures cause a nonzero exit.
Transport error details are suppressed because they can contain signed requests.

This is a read-only inspection command. It does not place or cancel orders, write database records, or start a runner.
Balance values are net of pending withdrawals; they do not establish available order funds or account trading eligibility.
The command does not combine Earn balances, map asset identifiers to pairs, or convert units.
Separate invocations are separate observations and can differ if the account changes between requests.
Treat saved output as private account data.

Tests verify signing and request encoding with a mocked transport. No authenticated account request was made during implementation.
xStocks live assignments remain disabled. Open-order reads, fill pagination, symbol mapping, and corporate-action handling need further work.
