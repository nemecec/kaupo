"""Read-only Kraken balance evidence with explicit xStocks units."""

from decimal import Decimal, InvalidOperation

import ccxt.async_support as ccxt


async def read_balances(api_key: str, api_secret: str, *, units: str) -> dict[str, str]:
    """Preserve exchange asset IDs and decimal strings; do not infer spendable funds."""
    if units not in ("rebased", "base"):
        raise ValueError("Balance units must be rebased or base")
    if not api_key or not api_secret:
        raise ValueError("Kraken API credentials are missing")
    exchange = ccxt.kraken({"apiKey": api_key, "secret": api_secret, "enableRateLimit": True})
    try:
        try:
            response = await exchange.private_post_balance({"rebase_multiplier": units})
        except Exception as exc:
            raise ValueError(f"Kraken balance request failed ({type(exc).__name__})") from None
        if not isinstance(response, dict) or response.get("error") != []:
            raise ValueError("Kraken rejected the balance request")
        result = response.get("result")
        if not isinstance(result, dict):
            raise ValueError("Kraken returned an invalid balance response")
        balances: dict[str, str] = {}
        for asset, value in result.items():
            if not isinstance(asset, str) or not asset.strip() or not isinstance(value, str):
                raise ValueError("Kraken returned an invalid balance entry")
            try:
                amount = Decimal(value)
            except InvalidOperation:
                raise ValueError("Kraken returned an invalid balance amount") from None
            if not amount.is_finite():
                raise ValueError("Kraken returned a non-finite balance amount")
            balances[asset] = value
        return balances
    finally:
        try:
            await exchange.close()
        except Exception:
            raise ValueError("Could not close the Kraken balance client") from None
