"""Paper venue: deterministic simulated execution against candles.

Execution model (identical in backtest and shadow):

- market orders fill at the *next* candle's open, worsened by slippage, taker fee
- limit orders fill when a candle's range touches the limit (buy: low <= limit,
  sell: high >= limit), at the limit price (or the open if it gapped through),
  maker fee
- stop-loss / take-profit attached to an order become active once that order
  fills and are evaluated on every subsequent candle; if both trigger in one
  candle the stop-loss wins (conservative)
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
        )
        self._orders[order.id] = order
        self._new_orders.append(order)
        return self._make_fill(order, candle.ts, self._slipped(candle.close, Side.SELL), self._taker)

    def cancel_all(self) -> list[Order]:
        cancelled = self._market_queue + self._limit_open
        for order in cancelled:
            order.status = OrderStatus.CANCELLED
        self._market_queue = []
        self._limit_open = []
        return cancelled

    def on_candle(self, candle: Candle) -> list[Fill]:
        fills: list[Fill] = []

        # 1. protective stops/take-profits from earlier fills (stop wins ties)
        remaining_protections: list[tuple[Order, _Protection]] = []
        for order, prot in self._protections:
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

        return fills

    # -- internals ---------------------------------------------------------

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
        """Build an exit order for the protected position if SL/TP triggers."""
        exit_side = Side.SELL if order.side is Side.BUY else Side.BUY
        exit_order = Order(
            pair=order.pair,
            side=exit_side,
            order_type=OrderType.MARKET,
            size=order.size,
            reason=f"protection for {order.id}",
        )
        # stop-loss first (conservative on ties)
        if prot.stop_loss is not None:
            hit = candle.low <= prot.stop_loss if order.side is Side.BUY else candle.high >= prot.stop_loss
            if hit:
                self._orders[exit_order.id] = exit_order
                self._new_orders.append(exit_order)
                price = self._slipped(min(prot.stop_loss, candle.open), exit_side)
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
        return None
