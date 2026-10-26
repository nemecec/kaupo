"""Public rule checks and an optional authenticated, validation-only request."""

from datetime import datetime, timedelta
from decimal import ROUND_DOWN, ROUND_UP, Decimal, InvalidOperation, localcontext
from typing import Any

import ccxt.async_support as ccxt

from kaupo.data.xstocks import XStockMarket
from kaupo.domain import Side


def _positive(value: object, name: str) -> Decimal:
    try:
        number = Decimal(str(value))
    except InvalidOperation:
        raise ValueError(f"{name} must be a positive finite decimal") from None
    if not number.is_finite() or not Decimal("1e-18") <= number <= Decimal("1e18"):
        raise ValueError(f"{name} must be between 1e-18 and 1e18")
    if len(number.as_tuple().digits) > 36:
        raise ValueError(f"{name} must have at most 36 significant digits")
    return number


def validation_order(
    market: XStockMarket,
    side: Side,
    size: Decimal,
    price: Decimal,
    expires_at: datetime,
    *,
    now: datetime,
) -> dict[str, Any]:
    """Build a spot post-only GTD validation request without guessed market rules."""
    if market.status not in ("online", "post_only", "limit_only"):
        raise ValueError(f"Market status {market.status!r} does not support this order check")
    if market.lot != "unit" or type(market.lot_multiplier) is not int or market.lot_multiplier != 1:
        raise ValueError("Order checks require unit lots with lot_multiplier=1")
    decimals = market.lot_decimals
    if type(decimals) is not int or not 0 <= decimals <= 18:
        raise ValueError("lot_decimals must be an integer from 0 through 18")
    tick = _positive(market.tick_size, "tick_size")
    minimum = _positive(market.order_min, "ordermin")
    cost_minimum = _positive(market.cost_min, "costmin")
    amount = _positive(size, "size")
    limit = _positive(price, "price")
    if not isinstance(side, Side):
        raise ValueError("side must be buy or sell")
    with localcontext() as context:
        context.prec = 100
        step = Decimal(1).scaleb(-decimals)
        amount = (amount / step).to_integral_value(rounding=ROUND_DOWN) * step
        rounding = ROUND_DOWN if side is Side.BUY else ROUND_UP
        limit = (limit / tick).to_integral_value(rounding=rounding) * tick
        if amount <= 0 or limit <= 0 or amount < minimum:
            raise ValueError("Rounded order is below the minimum amount or has a zero price")
        if amount * limit < cost_minimum:
            raise ValueError("Rounded order is below the minimum cost")
    if expires_at.utcoffset() is None or now.utcoffset() is None:
        raise ValueError("Order expiry and current time must include a timezone")
    expiry = int(expires_at.timestamp())
    # A conservative bound within Kraken's one-month GTD limit.
    if not now.timestamp() + 5 <= expiry <= (now + timedelta(days=28)).timestamp():
        raise ValueError("Order expiry must be from 5 seconds through 28 days in the future")
    if not market.api_pair or "/" in market.api_pair:
        raise ValueError("Missing canonical Kraken order symbol")
    return {
        "pair": market.api_pair,
        "asset_class": "tokenized_asset",
        "type": side.value,
        "ordertype": "limit",
        "volume": format(amount, "f"),
        "price": format(limit, "f"),
        "oflags": "post",
        "timeinforce": "GTD",
        "expiretm": str(expiry),
        "validate": True,
    }


async def validate_with_kraken(
    api_key: str,
    api_secret: str,
    market: XStockMarket,
    side: Side,
    size: Decimal,
    price: Decimal,
    expires_at: datetime,
    *,
    now: datetime,
) -> dict[str, Any]:
    """Use ccxt only for signing/transport; never load its crypto market universe."""
    if not api_key or not api_secret:
        raise ValueError("Kraken API credentials are missing")
    payload = validation_order(market, side, size, price, expires_at, now=now)
    exchange = ccxt.kraken({"apiKey": api_key, "secret": api_secret, "enableRateLimit": True})
    try:
        try:
            # Raw ccxt form encoding renders Python True as "True". Send the
            # documented lowercase boolean explicitly on the wire.
            response = await exchange.private_post_addorder({**payload, "validate": "true"})
        except Exception as exc:
            # Do not include transport exception bodies, which can contain signed requests.
            raise ValueError(f"Kraken validation request failed ({type(exc).__name__})") from None
        if not isinstance(response, dict) or response.get("error") != []:
            raise ValueError("Kraken rejected the validation request")
        result = response.get("result")
        if not isinstance(result, dict) or result.get("txid"):
            raise ValueError("Kraken returned an unexpected validation response")
        description = result.get("descr")
        if not isinstance(description, dict) or not isinstance(description.get("order"), str):
            raise ValueError("Kraken validation response has no order description")
        return {"request": payload, "description": description["order"]}
    finally:
        try:
            await exchange.close()
        except Exception:
            raise ValueError("Could not close the Kraken validation client") from None
