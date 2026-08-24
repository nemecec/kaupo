"""Paper venue: deterministic simulated execution against candles.

Execution model (identical in backtest and shadow):

- market orders fill at the *next* candle's open, worsened by slippage, taker fee
- limit orders fill when a candle's range touches the limit (buy: low <= limit,
  sell: high >= limit), at the limit price (or the open if it gapped through),
  maker fee
- stop-loss / take-profit attached to an order become active once that order
  fills and are evaluated on every subsequent candle; if both trigger in one
  candle the stop-loss wins (conservative)

Protections are position-aware: the venue tracks the net position from its own
fills, drops protections when the position is closed by other means (e.g. a
strategy exit), and clamps protection exits to the remaining position.
"""

from dataclasses import dataclass
from datetime import datetime

from kaupo.domain import (
    Candle,
    Fill,
    Order,
    OrderId,
    OrderStatus,
    OrderType,
    Pair,
    Side,
)


@dataclass
class _Protection:
    stop_loss: float | None
    take_profit: float | None


class PaperVenue:
    def __init__(self, taker_fee_bps: float, maker_fee_bps: float, slippage_bps: float) -> None:
        self._taker = taker_fee_bps / 10_000
        self._maker = maker_fee_bps / 10_000
        self._slip = slippage_bps / 10_000
        self._market_queue: list[Order] = []
        self._limit_open: list[Order] = []
        self._protections: list[tuple[Order, _Protection]] = []
        self._orders: dict[OrderId, Order] = {}
        self._new_orders: list[Order] = []
        self._positions: dict[Pair, float] = {}

    def submit(self, order: Order) -> None:
        self._orders[order.id] = order
        if order.order_type is OrderType.MARKET:
            self._market_queue.append(order)
        else:
            self._limit_open.append(order)

    def get_order(self, order_id: OrderId) -> Order | None:
        return self._orders.get(order_id)

    def drain_new_orders(self) -> list[Order]:
        orders, self._new_orders = self._new_orders, []
        return orders

    def liquidate(self, pair: Pair, size: float, candle: Candle) -> Fill:
        order = Order(
            pair=pair,
            side=Side.SELL,
            order_type=OrderType.MARKET,
            size=size,
            reason="end-of-run liquidation",
            created_ts=candle.ts,
        )
        self._orders[order.id] = order
        self._new_orders.append(order)
        fill = self._make_fill(order, candle.ts, self._slipped(candle.close, Side.SELL), self._taker)
        self._track_position(fill)
        return fill

    def cancel_all(self) -> list[Order]:
        cancelled = self._market_queue + self._limit_open
        for order in cancelled:
            order.status = OrderStatus.CANCELLED
        self._market_queue = []
        self._limit_open = []
        self._protections = []
        return cancelled

    def on_candle(self, candle: Candle) -> list[Fill]:
        self._prune_orders()
        fills: list[Fill] = []

        # 1. protective stops/take-profits from earlier fills (stop wins ties)
        remaining_protections: list[tuple[Order, _Protection]] = []
        for order, prot in self._protections:
            if self._positions.get(order.pair, 0.0) <= 0:
                continue  # disarm: position closed elsewhere (e.g. strategy exit)
            fill = self._check_protection(order, prot, candle)
            if fill is None:
                remaining_protections.append((order, prot))
            else:
                fills.append(fill)
        self._protections = remaining_protections

        # 2. resting limit orders
        still_open: list[Order] = []
        for order in self._limit_open:
            fill = self._try_limit(order, candle)
            if fill is None:
                still_open.append(order)
            else:
                fills.append(fill)
                self._arm_protection(order)
        self._limit_open = still_open

        # 3. market orders at this candle's open
        market = self._market_queue
        self._market_queue = []
        for order in market:
            fills.append(self._fill_market(order, candle))
            self._arm_protection(order)

        for fill in fills:
            self._track_position(fill)
        return fills

    # -- internals ---------------------------------------------------------

    def _track_position(self, fill: Fill) -> None:
        """Net position per pair from this venue's own fills."""
        delta = fill.size if fill.side is Side.BUY else -fill.size
        self._positions[fill.pair] = self._positions.get(fill.pair, 0.0) + delta

    def _prune_orders(self) -> None:
        """Drop closed orders to bound memory in long-lived runs."""
        self._orders = {
            oid: o
            for oid, o in self._orders.items()
            if o.status not in (OrderStatus.FILLED, OrderStatus.CANCELLED)
        }

    def _arm_protection(self, order: Order) -> None:
        if order.status is OrderStatus.FILLED and (order.stop_loss or order.take_profit):
            self._protections.append((order, _Protection(order.stop_loss, order.take_profit)))

    def _slipped(self, price: float, side: Side) -> float:
        return price * (1 + self._slip) if side is Side.BUY else price * (1 - self._slip)

    def _make_fill(self, order: Order, ts: datetime, price: float, fee_rate: float) -> Fill:
        order.status = OrderStatus.FILLED
        order.filled_price = price
        order.filled_ts = ts
        order.fee = price * order.size * fee_rate
        return Fill(
            order_id=order.id,
            pair=order.pair,
            side=order.side,
            ts=ts,
            price=price,
            size=order.size,
            fee=order.fee,
        )

    def _fill_market(self, order: Order, candle: Candle) -> Fill:
        return self._make_fill(order, candle.ts, self._slipped(candle.open, order.side), self._taker)

    def _try_limit(self, order: Order, candle: Candle) -> Fill | None:
        assert order.limit_price is not None
        limit = order.limit_price
        if order.side is Side.BUY and candle.low <= limit:
            price = min(limit, candle.open)  # gapped through -> get the open
            return self._make_fill(order, candle.ts, price, self._maker)
        if order.side is Side.SELL and candle.high >= limit:
            price = max(limit, candle.open)
            return self._make_fill(order, candle.ts, price, self._maker)
        return None

    def _check_protection(self, order: Order, prot: _Protection, candle: Candle) -> Fill | None:
        """Build an exit order if SL/TP triggers and a position remains.

        The caller drops protections when the net position is gone; exits
        are clamped to the remaining position.
        """
        remaining = self._positions.get(order.pair, 0.0)
        exit_size = min(order.size, remaining)
        exit_order = Order(
            pair=order.pair,
            side=Side.SELL if order.side is Side.BUY else Side.BUY,
            order_type=OrderType.MARKET,
            size=exit_size,
            reason=f"protection for {order.id}",
            created_ts=candle.ts,
        )
        # stop-loss first (conservative on ties)
        if prot.stop_loss is not None:
            hit = candle.low <= prot.stop_loss if order.side is Side.BUY else candle.high >= prot.stop_loss
            if hit:
                self._orders[exit_order.id] = exit_order
                self._new_orders.append(exit_order)
                price = self._slipped(min(prot.stop_loss, candle.open), exit_order.side)
                return self._make_fill(exit_order, candle.ts, price, self._taker)
        if prot.take_profit is not None:
            hit = (
                candle.high >= prot.take_profit if order.side is Side.BUY else candle.low <= prot.take_profit
            )
            if hit:
                self._orders[exit_order.id] = exit_order
                self._new_orders.append(exit_order)
                price = max(prot.take_profit, candle.open)
                return self._make_fill(exit_order, candle.ts, price, self._maker)
        return None  # still armed
