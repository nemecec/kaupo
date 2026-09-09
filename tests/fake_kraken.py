"""A fake authenticated Kraken client. No test ever reaches the real exchange.

It implements ``kaupo.venues.kraken_client.TradingClient`` and keeps every
call it received, so tests assert on what the venue actually sent: the
post-only flag, the size after rounding, the cancel that closes a candle.
Trades are scripted, because the exchange is the only source of fills.

Credentials never appear here. The fake needs none, which is the point.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal

from kaupo.domain import Pair, Side
from kaupo.venues.kraken_client import ExchangeError, ExchangeTrade, MarketMeta, PostOnlyRejected


@dataclass
class Placed:
    """One create_order call, exactly as the venue made it."""

    txid: str
    pair: Pair
    side: Side
    order_type: str
    size: float
    price: float | None
    post_only: bool


DEFAULT_MARKET = MarketMeta(
    amount_step=Decimal("0.01"),
    price_step=Decimal("0.01"),
    min_amount=0.05,
    min_cost=5.0,
)


class FakeKrakenClient:
    """Scriptable stand-in for Kraken's private endpoints."""

    def __init__(
        self,
        market: MarketMeta = DEFAULT_MARKET,
        balances: dict[str, float] | None = None,
        *,
        reject_post_only: bool = False,
        auto_fill: float | None = None,
        auto_fill_fee_rate: float = 0.0016,
    ) -> None:
        self.market = market
        self.balances = dict(balances or {})
        self.reject_post_only = reject_post_only
        # fraction of each placed order that trades straight away; None
        # leaves every order resting, as an untouched limit would
        self.auto_fill = auto_fill
        self.auto_fill_fee_rate = auto_fill_fee_rate
        self.auto_fill_price: float | None = None
        self.placed: list[Placed] = []
        self.cancelled: list[str] = []
        self.open_txids: list[str] = []
        self.trades: list[ExchangeTrade] = []
        self.calls: list[str] = []
        self.closed = False
        self._failures: dict[str, deque[Exception]] = {}
        self._next_txid = 0

    # -- scripting ---------------------------------------------------------

    def fail_next(self, method: str, error: Exception, times: int = 1) -> None:
        """Make the next ``times`` calls of ``method`` raise ``error``."""
        queue = self._failures.setdefault(method, deque())
        for _ in range(times):
            queue.append(error)

    def add_trade(
        self,
        txid: str,
        *,
        price: float,
        size: float,
        fee: float,
        ts: datetime,
        side: Side = Side.BUY,
        trade_id: str | None = None,
    ) -> ExchangeTrade:
        """Script one executed trade, as Kraken would report it."""
        trade = ExchangeTrade(
            id=trade_id or f"T{len(self.trades) + 1}",
            txid=txid,
            ts=ts,
            side=side,
            price=price,
            size=size,
            fee=fee,
            fee_currency="EUR",
        )
        self.trades.append(trade)
        return trade

    def fill_last(self, *, price: float, size: float | None = None, fee: float = 0.0, ts: datetime) -> None:
        """Fill the most recently placed order, wholly or in part."""
        placed = self.placed[-1]
        self.add_trade(
            placed.txid,
            price=price,
            size=placed.size if size is None else size,
            fee=fee,
            ts=ts,
            side=placed.side,
        )

    # -- TradingClient -----------------------------------------------------

    def _check(self, method: str) -> None:
        self.calls.append(method)
        queue = self._failures.get(method)
        if queue:
            raise queue.popleft()

    async def load_market(self, pair: Pair) -> MarketMeta:
        self._check("load_market")
        return self.market

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
        self._check("create_order")
        if post_only and self.reject_post_only:
            raise PostOnlyRejected("EOrder:Post only order")
        self._next_txid += 1
        txid = f"KRK-{self._next_txid}"
        self.placed.append(
            Placed(
                txid=txid,
                pair=pair,
                side=side,
                order_type=order_type,
                size=size,
                price=price,
                post_only=post_only,
            )
        )
        self.open_txids.append(txid)
        if self.auto_fill:
            filled = size * self.auto_fill
            fill_price = self.auto_fill_price or price or 0.0
            self.add_trade(
                txid,
                price=fill_price,
                size=filled,
                fee=fill_price * filled * self.auto_fill_fee_rate,
                ts=datetime.now(UTC),
                side=side,
            )
        return txid

    async def cancel_order(self, txid: str, pair: Pair) -> None:
        self._check("cancel_order")
        self.cancelled.append(txid)
        if txid in self.open_txids:
            self.open_txids.remove(txid)

    async def fetch_open_order_ids(self, pair: Pair) -> list[str]:
        self._check("fetch_open_orders")
        return list(self.open_txids)

    async def fetch_my_trades(self, pair: Pair, since_ms: int | None = None) -> list[ExchangeTrade]:
        self._check("fetch_my_trades")
        if since_ms is None:
            return list(self.trades)
        cutoff = datetime.fromtimestamp(since_ms / 1000, tz=UTC)
        return [t for t in self.trades if t.ts >= cutoff]

    async def fetch_balances(self) -> dict[str, float]:
        self._check("fetch_balances")
        return dict(self.balances)

    async def close(self) -> None:
        self.closed = True


@dataclass
class AlertSpy:
    """Collects the venue's ntfy alerts instead of pushing them."""

    messages: list[str] = field(default_factory=list)

    async def __call__(self, message: str) -> None:
        self.messages.append(message)

    def matching(self, needle: str) -> list[str]:
        return [m for m in self.messages if needle.lower() in m.lower()]


def unavailable() -> ExchangeError:
    """A generic exchange failure, for error-path tests."""
    return ExchangeError("Kraken is unavailable")
