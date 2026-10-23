"""The live client's pure helpers and the sync/async bridge. No network."""

import asyncio
from datetime import UTC, datetime
from decimal import Decimal

import ccxt.async_support as ccxt
import pytest

from kaupo.domain import Pair, Side
from kaupo.venues.kraken_client import (
    MAX_BACKOFF_SECONDS,
    AsyncBridge,
    ExchangeError,
    KrakenTradingClient,
    _is_post_only_rejection,
    _parse_trade,
    floor_to_step,
    retry_delay,
)

PAIR = Pair.parse("SOL/EUR")


class TestFloorToStep:
    def test_rounds_down_never_up(self) -> None:
        assert floor_to_step(1.2399, Decimal("0.01")) == 1.23
        assert floor_to_step(0.9, Decimal("1")) == 0.0

    def test_an_exact_multiple_is_unchanged(self) -> None:
        assert floor_to_step(2.5, Decimal("0.5")) == 2.5

    def test_a_zero_step_leaves_the_size_alone(self) -> None:
        assert floor_to_step(1.2345, Decimal("0")) == 1.2345


class TestRetryDelay:
    def test_doubles_per_failure(self) -> None:
        assert retry_delay(1, base=1.0) == 1.0
        assert retry_delay(2, base=1.0) == 2.0
        assert retry_delay(3, base=1.0) == 4.0

    def test_capped(self) -> None:
        assert retry_delay(50, base=1.0) == MAX_BACKOFF_SECONDS


class TestPostOnlyDetection:
    def test_reads_kraken_message_text(self) -> None:
        assert _is_post_only_rejection(Exception("EOrder:Post only order"))
        assert _is_post_only_rejection(Exception("post-only order rejected"))

    def test_maps_the_ccxt_type_too(self) -> None:
        assert _is_post_only_rejection(ccxt.OrderImmediatelyFillable("x"))

    def test_other_errors_pass_through(self) -> None:
        assert not _is_post_only_rejection(Exception("EOrder:Insufficient funds"))


class TestParseTrade:
    def test_normalizes_a_ccxt_trade(self) -> None:
        trade = _parse_trade(
            {
                "id": "T1",
                "order": "KRK-1",
                "timestamp": 1767225600000,
                "side": "buy",
                "price": 101.5,
                "amount": 0.4,
                "fee": {"cost": 0.06, "currency": "EUR"},
            },
            PAIR,
        )
        assert trade is not None
        assert trade.txid == "KRK-1"
        assert trade.side is Side.BUY
        assert trade.price == 101.5
        assert trade.size == 0.4
        assert trade.fee == 0.06
        assert trade.ts == datetime(2026, 1, 1, tzinfo=UTC)

    def test_a_row_without_an_order_id_is_dropped(self) -> None:
        row = {"id": "T1", "timestamp": 1, "side": "buy", "price": 1, "amount": 1}
        assert _parse_trade(row, PAIR) is None

    def test_a_missing_fee_becomes_zero(self) -> None:
        trade = _parse_trade(
            {"id": "T1", "order": "K", "timestamp": 1, "side": "sell", "price": 1.0, "amount": 1.0},
            PAIR,
        )
        assert trade is not None
        assert trade.fee == 0.0
        assert trade.fee_currency == "EUR"


class TestCredentials:
    def test_missing_credentials_never_build_a_client(self) -> None:
        with pytest.raises(ExchangeError):
            KrakenTradingClient("", "")
        with pytest.raises(ExchangeError):
            KrakenTradingClient("key-placeholder", "")


class TestAsyncBridge:
    def test_run_sync_returns_the_result(self) -> None:
        bridge = AsyncBridge(name="t1")
        try:

            async def work() -> int:
                await asyncio.sleep(0)
                return 42

            assert bridge.run_sync(work()) == 42
        finally:
            bridge.close()

    def test_run_sync_propagates_exceptions(self) -> None:
        bridge = AsyncBridge(name="t2")
        try:

            async def boom() -> None:
                raise ExchangeError("nope")

            with pytest.raises(ExchangeError):
                bridge.run_sync(boom())
        finally:
            bridge.close()

    async def test_run_awaits_without_blocking_the_caller_loop(self) -> None:
        bridge = AsyncBridge(name="t3")
        try:

            async def work() -> str:
                await asyncio.sleep(0.01)
                return "done"

            # the caller's own loop stays free while the bridge works
            ticked = 0

            async def tick() -> None:
                nonlocal ticked
                for _ in range(3):
                    await asyncio.sleep(0)
                    ticked += 1

            result, _ = await asyncio.gather(bridge.run(work()), tick())
            assert result == "done"
            assert ticked == 3
        finally:
            bridge.close()

    def test_a_closed_bridge_refuses_work(self) -> None:
        bridge = AsyncBridge(name="t4")
        bridge.close()

        async def work() -> int:
            return 1

        with pytest.raises(ExchangeError):
            bridge.run_sync(work())

    def test_closing_twice_is_safe(self) -> None:
        bridge = AsyncBridge(name="t5")
        bridge.close()
        bridge.close()


@pytest.mark.parametrize("order_type", ["limit", "market"])
async def test_order_expiry_payload(monkeypatch, order_type):
    from unittest.mock import AsyncMock

    exchange = type("Exchange", (), {})()
    exchange.load_markets = AsyncMock()
    exchange.create_order = AsyncMock(return_value={"id": "K1"})
    monkeypatch.setattr(ccxt, "kraken", lambda config: exchange)
    client = KrakenTradingClient("fake", "fake")
    expiry = datetime(2026, 1, 2, 22, tzinfo=UTC) if order_type == "limit" else None
    assert (
        await client.create_order(
            PAIR,
            Side.BUY,
            order_type,
            1,
            90 if expiry else None,
            post_only=bool(expiry),
            expires_at=expiry,
        )
        == "K1"
    )
    params = exchange.create_order.call_args.args[-1]
    if expiry:
        assert params == {"oflags": "post", "timeinforce": "GTD", "expiretm": str(int(expiry.timestamp()))}
    else:
        assert params == {}


@pytest.mark.parametrize(
    "order_type,expiry",
    [
        ("market", datetime(2026, 1, 2, tzinfo=UTC)),
        ("limit", datetime(2026, 1, 2)),
        ("limit", datetime(1970, 1, 1, tzinfo=UTC)),
    ],
)
async def test_invalid_expiry_never_reaches_exchange(monkeypatch, order_type, expiry):
    from unittest.mock import AsyncMock

    exchange = type("Exchange", (), {})()
    exchange.create_order = AsyncMock()
    monkeypatch.setattr(ccxt, "kraken", lambda config: exchange)
    client = KrakenTradingClient("fake", "fake")
    with pytest.raises(ExchangeError):
        await client.create_order(PAIR, Side.BUY, order_type, 1, 90, expires_at=expiry)
    exchange.create_order.assert_not_called()


async def test_bridged_client_preserves_expiry():
    from kaupo.venues.kraken_client import BridgedClient
    from tests.fake_kraken import FakeKrakenClient

    inner = FakeKrakenClient()
    bridge = AsyncBridge(name="expiry-bridge")
    try:
        client = BridgedClient(inner, bridge)
        expiry = datetime(2026, 1, 2, 22, tzinfo=UTC)
        await client.create_order(PAIR, Side.BUY, "limit", 1, 90, post_only=True, expires_at=expiry)
        assert inner.placed[0].expires_at == expiry
        assert inner.placed[0].post_only
    finally:
        bridge.close()


async def test_installed_ccxt_preserves_native_gtd_parameters(monkeypatch):
    exchange = ccxt.kraken()
    try:
        monkeypatch.setattr(exchange, "price_to_precision", lambda symbol, price: str(price))
        params = {"oflags": "post", "timeinforce": "GTD", "expiretm": "1767391200"}
        request, remaining = exchange.order_request("createOrder", str(PAIR), "limit", {}, 1, 90, params)
        payload = exchange.extend(request, remaining)
        assert payload["oflags"] == "post"
        assert payload["timeinforce"] == "GTD"
        assert payload["expiretm"] == "1767391200"
    finally:
        await exchange.close()
