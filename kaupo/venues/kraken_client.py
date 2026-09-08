"""Authenticated Kraken spot client, and the sync/async bridge the venue needs.

Two problems are solved here, both of them plumbing:

*The protocol mismatch.* ``kaupo.venues.venue.Venue`` is synchronous — the
engine calls ``on_candle`` and ``cancel_all`` from inside an async body
without awaiting them — while the exchange client is ``ccxt.async_support``.
:class:`AsyncBridge` runs one private event loop on its own thread: the
venue's sync methods block on a future, and the reconciler, which lives on
the run's own loop, awaits the same future without blocking it. One bridge
per run means one client and one rate-limit bucket per run.

*The ccxt surface.* :class:`TradingClient` is the small typed protocol the
venue and the reconciler actually use — place, cancel, list, read trades,
read balances. :class:`KrakenTradingClient` is the only implementation that
touches the network; tests inject a fake with the same shape, so no test
can reach Kraken.

Credentials arrive from settings and are never logged. Exception text from
ccxt is logged, so callers must keep keys out of request payloads (they
travel in headers, which ccxt does not echo into error messages).
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import logging
import math
import threading
from collections.abc import Coroutine
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import ROUND_DOWN, Decimal
from typing import Any, Protocol, Self, TypeVar

import ccxt.async_support as ccxt

from kaupo.domain import Pair, Side

log = logging.getLogger(__name__)

T = TypeVar("T")

# Exchange calls retry a few times inside one candle body. The engine's
# candle-body watchdog fires at 120s (kaupo/core/engine.py), so the whole
# retry budget of one call must stay well under it: 1s + 2s + 4s at worst.
RETRY_ATTEMPTS = 3
RETRY_BASE_SECONDS = 1.0
MAX_BACKOFF_SECONDS = 30.0

# how long a sync protocol method waits for the bridge before giving up
CALL_TIMEOUT_SECONDS = 90.0


class ExchangeError(Exception):
    """Any failure of an exchange call that the venue must handle, not crash on."""


class PostOnlyRejected(ExchangeError):
    """Kraken refused a post-only limit because it would have taken liquidity.

    Paper semantics fill such an order; live semantics skip it and let the
    strategy re-decide on the next candle (task spec section 6.1).
    """


@dataclass(frozen=True)
class MarketMeta:
    """The pair's trading rules, as the venue needs them.

    ``amount_step`` is the lot precision: sizes are rounded DOWN to a
    multiple of it, never up. ``min_amount`` and ``min_cost`` are the
    exchange minimums below which an order is skipped.
    """

    amount_step: Decimal
    price_step: Decimal
    min_amount: float
    min_cost: float


@dataclass(frozen=True)
class ExchangeTrade:
    """One executed trade as the exchange reports it: the only source of fills.

    ``txid`` is the Kraken order id the trade belongs to; one order can
    produce several trades (partial fills).
    """

    id: str
    txid: str
    ts: datetime
    side: Side
    price: float
    size: float
    fee: float
    fee_currency: str


class TradingClient(Protocol):
    """The authenticated exchange surface the live path uses. Fakes implement it."""

    async def load_market(self, pair: Pair) -> MarketMeta: ...

    async def create_order(
        self,
        pair: Pair,
        side: Side,
        order_type: str,
        size: float,
        price: float | None = None,
        *,
        post_only: bool = False,
    ) -> str:
        """Place an order; returns the exchange order id (Kraken txid).

        Raises :class:`PostOnlyRejected` when a post-only limit would have
        taken, and :class:`ExchangeError` for anything else.
        """
        ...

    async def cancel_order(self, txid: str, pair: Pair) -> None:
        """Cancel one order. Cancelling an order that is already gone is a no-op."""
        ...

    async def fetch_open_order_ids(self, pair: Pair) -> list[str]: ...

    async def fetch_my_trades(self, pair: Pair, since_ms: int | None = None) -> list[ExchangeTrade]: ...

    async def fetch_balances(self) -> dict[str, float]:
        """Total balance per asset code (free + held)."""
        ...

    async def close(self) -> None: ...


def retry_delay(failures: int, base: float = RETRY_BASE_SECONDS, cap: float = MAX_BACKOFF_SECONDS) -> float:
    """Seconds to wait after ``failures`` consecutive failures.

    Doubles per failure from ``base``, capped at ``cap`` — the same shape as
    ``LiveCandlePoller._retry_delay`` in ``kaupo/data/ingest.py``, with a
    budget small enough to fit inside one candle body.
    """
    steps = min(max(failures - 1, 0), 20)  # a long outage must not overflow the exponent
    return min(base * 2.0**steps, cap)


def floor_to_step(size: float, step: Decimal) -> float:
    """Round ``size`` DOWN to a multiple of ``step``.

    Sizes are never rounded up: an order the strategy did not ask for is
    worse than no order at all.
    """
    if step <= 0:
        return size
    quantized = (Decimal(str(size)) / step).to_integral_value(rounding=ROUND_DOWN) * step
    return float(quantized)


class AsyncBridge:
    """A private event loop on its own thread, callable from sync and async code.

    ``run_sync`` blocks the calling thread (the Venue protocol's methods);
    ``run`` awaits without blocking the caller's loop (the reconciler). Both
    execute on the bridge's loop, so everything the bridge owns — the ccxt
    client and its aiohttp session — stays on one loop, as ccxt requires.
    """

    def __init__(self, name: str = "kraken-venue") -> None:
        self._loop = asyncio.new_event_loop()
        self._thread = threading.Thread(target=self._serve, name=name, daemon=True)
        self._thread.start()
        self._closed = False

    def _serve(self) -> None:
        asyncio.set_event_loop(self._loop)
        self._loop.run_forever()

    def _submit(self, coro: Coroutine[Any, Any, T]) -> concurrent.futures.Future[T]:
        if self._closed:
            coro.close()
            raise ExchangeError("The exchange bridge is closed")
        return asyncio.run_coroutine_threadsafe(coro, self._loop)

    def run_sync(self, coro: Coroutine[Any, Any, T], timeout: float = CALL_TIMEOUT_SECONDS) -> T:
        return self._submit(coro).result(timeout)

    async def run(self, coro: Coroutine[Any, Any, T]) -> T:
        return await asyncio.wrap_future(self._submit(coro))

    def close(self) -> None:
        """Stop the loop and join the thread. Safe to call twice."""
        if self._closed:
            return
        self._closed = True
        self._loop.call_soon_threadsafe(self._loop.stop)
        self._thread.join(timeout=10.0)
        if not self._thread.is_alive():
            self._loop.close()


class BridgedClient:
    """A :class:`TradingClient` whose calls execute on the bridge's loop.

    The venue owns the exchange client on the bridge, but the reconciler
    also does database work, and SQLAlchemy's connection pool belongs to the
    run's own loop. This adapter lets the reconciler stay where its database
    handles live while the exchange round trip hops to the bridge, so the
    run still has exactly one exchange client and one rate-limit bucket.
    """

    def __init__(self, inner: TradingClient, bridge: AsyncBridge) -> None:
        self._inner = inner
        self._bridge = bridge

    async def load_market(self, pair: Pair) -> MarketMeta:
        return await self._bridge.run(self._inner.load_market(pair))

    async def create_order(
        self,
        pair: Pair,
        side: Side,
        order_type: str,
        size: float,
        price: float | None = None,
        *,
        post_only: bool = False,
    ) -> str:
        return await self._bridge.run(
            self._inner.create_order(pair, side, order_type, size, price, post_only=post_only)
        )

    async def cancel_order(self, txid: str, pair: Pair) -> None:
        await self._bridge.run(self._inner.cancel_order(txid, pair))

    async def fetch_open_order_ids(self, pair: Pair) -> list[str]:
        return await self._bridge.run(self._inner.fetch_open_order_ids(pair))

    async def fetch_my_trades(self, pair: Pair, since_ms: int | None = None) -> list[ExchangeTrade]:
        return await self._bridge.run(self._inner.fetch_my_trades(pair, since_ms))

    async def fetch_balances(self) -> dict[str, float]:
        return await self._bridge.run(self._inner.fetch_balances())

    async def close(self) -> None:
        await self._bridge.run(self._inner.close())


def _is_post_only_rejection(exc: BaseException) -> bool:
    """Kraken reports a would-be-marketable post-only order as a plain error.

    ccxt has no exact mapping for ``EOrder:Post only order``, so the message
    is the only signal; ``OrderImmediatelyFillable`` covers the venues that
    do map it.
    """
    if isinstance(exc, ccxt.OrderImmediatelyFillable):
        return True
    text = str(exc).lower()
    return "post only" in text or "post-only" in text


def _as_float(value: Any, default: float = 0.0) -> float:
    if value is None or not isinstance(value, (int, float)) or not math.isfinite(value):
        return default
    return float(value)


class KrakenTradingClient:
    """Authenticated Kraken spot client over ``ccxt.async_support``.

    The only class in the live path that opens a socket. It normalizes ccxt
    structures into the domain shapes the venue understands and translates
    ccxt exceptions into :class:`ExchangeError`, so no ccxt type leaks past
    this boundary.
    """

    def __init__(self, api_key: str, api_secret: str) -> None:
        if not api_key or not api_secret:
            raise ExchangeError("Kraken API credentials are missing")
        self._exchange = ccxt.kraken({"apiKey": api_key, "secret": api_secret, "enableRateLimit": True})
        self._markets_loaded = False

    async def close(self) -> None:
        await self._exchange.close()

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.close()

    async def _ensure_markets(self) -> None:
        if not self._markets_loaded:
            await self._exchange.load_markets()
            self._markets_loaded = True

    async def load_market(self, pair: Pair) -> MarketMeta:
        try:
            await self._ensure_markets()
            market = self._exchange.market(str(pair))
        except Exception as exc:
            raise ExchangeError(f"Could not load the {pair} market: {exc}") from exc
        precision = market.get("precision") or {}
        limits = market.get("limits") or {}
        amount_limits = limits.get("amount") or {}
        cost_limits = limits.get("cost") or {}
        # ccxt runs Kraken in TICK_SIZE mode: precision values are step sizes
        return MarketMeta(
            amount_step=Decimal(str(precision.get("amount") or "0.00000001")),
            price_step=Decimal(str(precision.get("price") or "0.00000001")),
            min_amount=_as_float(amount_limits.get("min")),
            min_cost=_as_float(cost_limits.get("min")),
        )

    async def create_order(
        self,
        pair: Pair,
        side: Side,
        order_type: str,
        size: float,
        price: float | None = None,
        *,
        post_only: bool = False,
    ) -> str:
        # oflags=post is Kraken's own post-only flag: it guarantees the maker
        # fee the backtests assume (task spec section 5.1)
        params: dict[str, Any] = {"oflags": "post"} if post_only else {}
        try:
            await self._ensure_markets()
            order = await self._exchange.create_order(str(pair), order_type, side.value, size, price, params)
        except Exception as exc:
            if post_only and _is_post_only_rejection(exc):
                raise PostOnlyRejected(str(exc)) from exc
            raise ExchangeError(f"create_order {order_type} {side.value} {pair} failed: {exc}") from exc
        txid = order.get("id")
        if not txid:
            raise ExchangeError(f"create_order {order_type} {side.value} {pair} returned no order id")
        return str(txid)

    async def cancel_order(self, txid: str, pair: Pair) -> None:
        try:
            await self._exchange.cancel_order(txid, str(pair))
        except ccxt.OrderNotFound:
            # already filled or already cancelled: the desired state holds
            log.info("Cancel of %s found no open order; treating it as cancelled", txid)
        except Exception as exc:
            raise ExchangeError(f"cancel_order {txid} failed: {exc}") from exc

    async def fetch_open_order_ids(self, pair: Pair) -> list[str]:
        try:
            orders = await self._exchange.fetch_open_orders(str(pair))
        except Exception as exc:
            raise ExchangeError(f"fetch_open_orders {pair} failed: {exc}") from exc
        return [str(o["id"]) for o in orders if o.get("id")]

    async def fetch_my_trades(self, pair: Pair, since_ms: int | None = None) -> list[ExchangeTrade]:
        try:
            raw = await self._exchange.fetch_my_trades(str(pair), since=since_ms)
        except Exception as exc:
            raise ExchangeError(f"fetch_my_trades {pair} failed: {exc}") from exc
        trades = []
        for entry in raw:
            trade = _parse_trade(entry, pair)
            if trade is not None:
                trades.append(trade)
        return sorted(trades, key=lambda t: (t.ts, t.id))

    async def fetch_balances(self) -> dict[str, float]:
        try:
            balance = await self._exchange.fetch_balance()
        except Exception as exc:
            raise ExchangeError(f"fetch_balance failed: {exc}") from exc
        total = balance.get("total") or {}
        return {str(asset): _as_float(amount) for asset, amount in total.items()}


def _parse_trade(entry: dict[str, Any], pair: Pair) -> ExchangeTrade | None:
    """Normalize one ccxt trade; a malformed row is dropped with a warning.

    A dropped row is a real hazard (a lost fill), so it is logged loudly;
    the reconciler's balance check is the backstop that catches the damage.
    """
    trade_id = entry.get("id")
    txid = entry.get("order")
    ts_ms = entry.get("timestamp")
    price = entry.get("price")
    amount = entry.get("amount")
    side = entry.get("side")
    if not trade_id or not txid or not ts_ms or side not in ("buy", "sell"):
        log.warning("Dropping malformed %s trade row: %s", pair, entry)
        return None
    if price is None or amount is None or not math.isfinite(price) or not math.isfinite(amount):
        log.warning("Dropping malformed %s trade row: %s", pair, entry)
        return None
    fee = entry.get("fee") or {}
    return ExchangeTrade(
        id=str(trade_id),
        txid=str(txid),
        ts=datetime.fromtimestamp(ts_ms / 1000, tz=UTC),
        side=Side(side),
        price=float(price),
        size=float(amount),
        fee=_as_float(fee.get("cost")),
        fee_currency=str(fee.get("currency") or pair.quote),
    )
