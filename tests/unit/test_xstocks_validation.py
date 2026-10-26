"""Order checks cannot become live orders or silently invent exchange rules."""

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from unittest.mock import AsyncMock

import ccxt.async_support as ccxt
import pytest

from kaupo.data.xstocks import XStockMarket
from kaupo.domain import Pair, Side
from kaupo.venues.xstocks_validation import validate_with_kraken, validation_order

NOW = datetime(2026, 10, 26, 12, tzinfo=UTC)
MARKET = XStockMarket(Pair.parse("AAPLX/USD"), "AAPLxUSD", "online", "0.05", "0.01", "1", 3, 1, "unit")


def args(**changes):
    return (
        dict(
            market=MARKET,
            side=Side.BUY,
            size=Decimal("1.2349"),
            price=Decimal("100.079"),
            expires_at=NOW + timedelta(hours=1),
            now=NOW,
        )
        | changes
    )


@pytest.mark.parametrize("side,price", [(Side.BUY, "100.05"), (Side.SELL, "100.10")])
def test_request_rounds_conservatively_and_forces_validation(side, price):
    payload = validation_order(**args(side=side))
    assert payload == {
        "pair": "AAPLxUSD",
        "asset_class": "tokenized_asset",
        "type": side.value,
        "ordertype": "limit",
        "volume": "1.234",
        "price": price,
        "oflags": "post",
        "timeinforce": "GTD",
        "expiretm": str(int((NOW + timedelta(hours=1)).timestamp())),
        "validate": True,
    }


@pytest.mark.parametrize(
    "changes",
    [
        {"lot_decimals": None},
        {"lot_decimals": True},
        {"lot_decimals": -1},
        {"lot_decimals": 19},
        {"lot_multiplier": 2},
        {"lot_multiplier": True},
        {"lot": None},
        {"tick_size": "NaN"},
        {"tick_size": "0"},
        {"order_min": None},
        {"cost_min": "-1"},
        {"status": "cancel_only"},
        {"status": "offline"},
        {"api_pair": ""},
    ],
)
def test_missing_or_invalid_metadata_rejected(changes):
    with pytest.raises(ValueError):
        validation_order(**args(market=replace(MARKET, **changes)))


@pytest.mark.parametrize(
    "changes",
    [
        {"size": Decimal("0.0099")},
        {"price": Decimal("0.01")},
        {"size": Decimal("0.01"), "price": Decimal("99.999")},
        {"size": Decimal("NaN")},
        {"price": Decimal("Infinity")},
        {"expires_at": NOW},
        {"expires_at": NOW + timedelta(days=29)},
        {"expires_at": NOW.replace(tzinfo=None)},
    ],
)
def test_invalid_orders_and_post_rounding_minimums(changes):
    with pytest.raises(ValueError):
        validation_order(**args(**changes))


def test_small_lots_do_not_round_amount_up_due_to_decimal_context():
    market = replace(MARKET, lot_decimals=18)
    size = Decimal("999999999999999999.999999999999999999")
    payload = validation_order(**args(market=market, size=size))
    assert Decimal(payload["volume"]) == size


@pytest.mark.parametrize("status", ["post_only", "limit_only"])
def test_post_only_limit_allowed_under_restricted_market_status(status):
    assert validation_order(**args(market=replace(MARKET, status=status)))["oflags"] == "post"


async def test_authenticated_call_uses_raw_endpoint_and_always_closes(monkeypatch):
    class Exchange:
        private_post_addorder = AsyncMock(
            return_value={"error": [], "result": {"descr": {"order": "buy validated"}}}
        )
        close = AsyncMock()

    exchange = Exchange()
    monkeypatch.setattr(ccxt, "kraken", lambda config: exchange)
    result = await validate_with_kraken("key", "secret", **args())
    payload = exchange.private_post_addorder.call_args.args[0]
    assert payload["validate"] == "true"
    assert payload["asset_class"] == "tokenized_asset"
    assert payload["pair"] == "AAPLxUSD"
    assert result["request"] == {**payload, "validate": True}
    assert result["description"] == "buy validated"
    exchange.close.assert_awaited_once()


@pytest.mark.parametrize(
    "response", [{}, {"error": ["failure"]}, {"result": {"txid": ["unexpected-order"]}}, {"result": {}}]
)
async def test_malformed_response_cannot_claim_validation(monkeypatch, response):
    exchange = type(
        "Exchange", (), {"private_post_addorder": AsyncMock(return_value=response), "close": AsyncMock()}
    )()
    monkeypatch.setattr(ccxt, "kraken", lambda config: exchange)
    with pytest.raises(ValueError):
        await validate_with_kraken("key", "secret", **args())
    exchange.close.assert_awaited_once()


async def test_transport_error_hides_credentials_and_does_not_retry(monkeypatch):
    exchange = type(
        "Exchange",
        (),
        {
            "private_post_addorder": AsyncMock(side_effect=RuntimeError("key secret signed-request")),
            "close": AsyncMock(),
        },
    )()
    monkeypatch.setattr(ccxt, "kraken", lambda config: exchange)
    with pytest.raises(ValueError, match="failed") as failure:
        await validate_with_kraken("key", "secret", **args())
    assert "secret" not in str(failure.value)
    assert "signed-request" not in str(failure.value)
    exchange.private_post_addorder.assert_awaited_once()
    exchange.close.assert_awaited_once()


async def test_invalid_rules_or_credentials_never_construct_private_client(monkeypatch):
    def forbidden(config):
        pytest.fail("private client constructed before validation")

    monkeypatch.setattr(ccxt, "kraken", forbidden)
    with pytest.raises(ValueError, match="credentials"):
        await validate_with_kraken("", "", **args())
    with pytest.raises(ValueError, match="lot_decimals"):
        await validate_with_kraken("key", "secret", **args(market=replace(MARKET, lot_decimals=None)))


async def test_installed_ccxt_serializes_validate_flag_on_raw_endpoint(monkeypatch):
    from urllib.parse import parse_qs

    exchange = ccxt.kraken({"apiKey": "test", "secret": "c2VjcmV0"})
    fetch = AsyncMock(return_value={"error": [], "result": {"descr": {"order": "validated"}}})
    monkeypatch.setattr(exchange, "fetch", fetch)
    monkeypatch.setattr(ccxt, "kraken", lambda config: exchange)
    await validate_with_kraken("test", "c2VjcmV0", **args())
    fetch.assert_awaited_once()
    call = fetch.call_args
    assert call.args[0] == "https://api.kraken.com/0/private/AddOrder"
    body = parse_qs(call.args[3])
    assert body["validate"] == ["true"]
    assert body["asset_class"] == ["tokenized_asset"]
    assert body["pair"] == ["AAPLxUSD"]
    assert body["oflags"] == ["post"]
    assert body["timeinforce"] == ["GTD"]
