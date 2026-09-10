"""Paper venue: deterministic simulated execution against candles.

Execution model (identical in backtest and shadow):

- market orders fill at the *next* candle's open, worsened by slippage, taker fee
- limit orders fill when a candle's range touches the limit (buy: low <= limit,
  sell: high >= limit), at the limit price (or the open if it gapped through),
  maker fee, no slippage
- a limit order lives for ONE candle only: submitted after a strategy decision,
  it is eligible on the next candle and expires unfilled at that candle's
  close (status cancelled; reported via drain_expired)
- a limit that is MARKETABLE AT POSTING — one that would take liquidity the
  moment it rests on the book (buy: limit >= open, sell: limit <= open) — is
  handled by ``marketable_limit``; see :func:`is_marketable_at_posting`
- stop-loss / take-profit attached to an order become active once that order
  fills and are evaluated on every subsequent candle; if both trigger in one
  candle the stop-loss wins (conservative)

Protections are position-aware: the venue tracks the net position from its own
fills, drops protections when the position is closed by other means (e.g. a
strategy exit), and clamps protection exits to the remaining position.

**Fee-tier fidelity on marketable limits (kaupo#36).** Charging the maker fee
on a limit that would have taken liquidity flatters a maker strategy: the live
venue posts every limit post-only, so Kraken rejects such an order outright and
no fill happens at all. ``marketable_limit`` selects which of the three
plausible venues this one imitates. Only limits marketable at posting are
affected; every other limit keeps the touch rule, the maker fee, and its
one-candle life in all three modes.
"""

import logging
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Literal, get_args

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

log = logging.getLogger(__name__)

#: How the venue treats a limit order that is marketable at posting.
#:
#: - ``maker``: fill at limit (or the open on gap-through) and charge the
#:   maker fee. The legacy model, and the default, so every recorded backtest
#:   number stays comparable.
#: - ``taker``: fill at the same price and charge the TAKER fee, no slippage
#:   (the limit bounds the price). Models a plain limit order on a real venue.
#: - ``skip``: no fill at all; the order expires and the strategy re-decides
#:   next candle. Models Kraken post-only, which is what the live venue posts.
MarketableLimit = Literal["maker", "taker", "skip"]

MARKETABLE_LIMIT_MODES: tuple[MarketableLimit, ...] = get_args(MarketableLimit)

#: Legacy behaviour: what an ad-hoc backtest gets unless it opts out.
DEFAULT_MARKETABLE_LIMIT: MarketableLimit = "maker"

#: The mode that mirrors the live venue (post-only limits, kaupo#36).
#: Shadow runs and the rolling-origin re-backtests of those runs must BOTH
#: read this constant, never the literal: the moment they disagree, the
#: triage compares two different venue models and its verdicts are noise.
LIVE_MIRROR_MARKETABLE_LIMIT: MarketableLimit = "skip"


def is_marketable_at_posting(side: Side, limit_price: float, open_price: float) -> bool:
    """True when the limit would take liquidity the moment it is posted.

    The strategy posts within seconds of the decision candle's close, so the
    fill candle's open is the closest deterministic proxy for the book at
    posting that a candle model has. A buy at or above the open crosses the
    ask; a sell at or below it crosses the bid.

    Note that a marketable limit always fills under the touch rule: a buy
    with ``limit >= open >= low`` is touched by construction, and a sell with
    ``limit <= open <= high`` likewise. So the modes only ever change what
    happens to an order that would otherwise have filled.
    """
    return limit_price >= open_price if side is Side.BUY else limit_price <= open_price


@dataclass
class _Protection:
    stop_loss: float | None
    take_profit: float | None


class PaperVenue:
    def __init__(
        self,
        taker_fee_bps: float,
        maker_fee_bps: float,
        slippage_bps: float,
        *,
        marketable_limit: MarketableLimit = DEFAULT_MARKETABLE_LIMIT,
    ) -> None:
        if marketable_limit not in MARKETABLE_LIMIT_MODES:
            valid = ", ".join(MARKETABLE_LIMIT_MODES)
            raise ValueError(f"Unknown marketable_limit {marketable_limit!r}; valid: {valid}")
        self._taker = taker_fee_bps / 10_000
        self._maker = maker_fee_bps / 10_000
        self._slip = slippage_bps / 10_000
        self._marketable_limit = marketable_limit
        self._market_queue: list[Order] = []
        self._limit_open: list[Order] = []
        self._expired: list[Order] = []
        self._protections: list[tuple[Order, _Protection]] = []
        # armed this candle; evaluated from the NEXT candle onward (the entry
        # candle's low may precede the fill — intracandle order is unknowable)
        self._newly_armed: list[tuple[Order, _Protection]] = []
        # exit order id -> (entry order, protection), for void re-arming
        self._prot_by_exit: dict[OrderId, tuple[Order, _Protection]] = {}
        self._orders: dict[OrderId, Order] = {}
        self._new_orders: list[Order] = []
        self._positions: dict[Pair, Decimal] = {}

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

    def drain_expired(self) -> list[Order]:
        orders, self._expired = self._expired, []
        return orders

    def liquidate(self, pair: Pair, size: float, candle: Candle) -> Fill:
        # size is signed: a positive size sells the long, a negative size
        # buys the short back (perp runs can end short)
        side = Side.SELL if size > 0 else Side.BUY
        order = Order(
            pair=pair,
            side=side,
            order_type=OrderType.MARKET,
            size=abs(size),
            reason="end-of-run liquidation",
            created_ts=candle.ts,
        )
        self._orders[order.id] = order
        self._new_orders.append(order)
        fill = self._make_fill(order, candle.ts, self._slipped(candle.close, side), self._taker)
        self._track_position(fill)
        return fill

    def void_fill(self, fill: Fill) -> None:
        """Roll back a fill the ledger rejected: untrack the position change,
        mark the order rejected, and reconcile protections so venue, ledger,
        and audit agree."""
        delta = Decimal(str(fill.size)) if fill.side is Side.BUY else -Decimal(str(fill.size))
        self._positions[fill.pair] = self._positions.get(fill.pair, Decimal(0)) - delta
        order = self._orders.get(fill.order_id)
        if order is not None:
            order.status = OrderStatus.REJECTED
            order.filled_price = None
            order.filled_ts = None
            order.fee = 0.0
            # a voided entry must not leave its stop/take-profit armed
            self._protections = [(o, p) for o, p in self._protections if o.id != order.id]
            self._newly_armed = [(o, p) for o, p in self._newly_armed if o.id != order.id]
        # a voided protection EXIT must re-arm the entry's protection
        protected = self._prot_by_exit.pop(fill.order_id, None)
        if protected is not None:
            self._protections.append(protected)

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

        # Chronological order within the candle:
        # 1. market orders fill at the OPEN (they were submitted last candle)
        # 2. limit orders fill when the candle's range touches their price;
        #    still untouched at the close, they expire (one-candle lifetime)
        # 3. protections (SL/TP) trigger intracandle, AFTER positions are known
        # Positions are tracked incrementally so later fills see earlier fills.

        market = self._market_queue
        self._market_queue = []
        for order in market:
            fill = self._fill_market(order, candle)
            fills.append(fill)
            self._track_position(fill)
            self._arm_protection(order)

        limits = self._limit_open
        self._limit_open = []
        for order in limits:
            skipped = self._skips_as_marketable(order, candle)
            limit_fill = None if skipped else self._try_limit(order, candle)
            if limit_fill is None:
                order.status = OrderStatus.CANCELLED
                self._expired.append(order)
                log.info(
                    "Limit order %s (%s %s @ %s) %s on %s",
                    order.id,
                    order.side.value,
                    order.pair,
                    order.limit_price,
                    f"was marketable at the open {candle.open} and post-only would have rejected it"
                    if skipped
                    else "expired unfilled",
                    candle.ts,
                )
            else:
                fills.append(limit_fill)
                self._track_position(limit_fill)
                self._arm_protection(order)

        remaining_protections: list[tuple[Order, _Protection]] = []
        for order, prot in self._protections:
            if self._positions.get(order.pair, Decimal(0)) <= 0:
                continue  # disarm: position closed elsewhere (e.g. strategy exit)
            prot_fill = self._check_protection(order, prot, candle)
            if prot_fill is None:
                remaining_protections.append((order, prot))
            else:
                fills.append(prot_fill)
                self._track_position(prot_fill)
                # successful fire: map entry kept until the ledger accepts the
                # fill (void_fill pops it); pruned here once it's redundant
                if prot_fill.side is Side.SELL and self._positions.get(order.pair, Decimal(0)) > 0:
                    pass  # partial exit — entry's protection was consumed by design
        self._protections = remaining_protections
        # protections armed by this candle's fills go live next candle
        self._protections.extend(self._newly_armed)
        self._newly_armed = []

        return fills

    # -- internals ---------------------------------------------------------

    def _track_position(self, fill: Fill) -> None:
        """Net position per pair from this venue's own fills.

        Decimal(str()) arithmetic, exactly like the ledger, so the two
        never drift apart on fractional sizes (0.1 + 0.2 + 0.3 ...)."""
        delta = Decimal(str(fill.size)) if fill.side is Side.BUY else -Decimal(str(fill.size))
        self._positions[fill.pair] = self._positions.get(fill.pair, Decimal(0)) + delta
        if self._positions[fill.pair] <= 0:
            # flat at any point -> stale protections for this pair are dropped,
            # even if a same-candle re-entry brings the position back up
            self._protections = [(o, p) for o, p in self._protections if o.pair != fill.pair]
            self._newly_armed = [(o, p) for o, p in self._newly_armed if o.pair != fill.pair]

    def _prune_orders(self) -> None:
        """Drop closed orders to bound memory in long-lived runs."""
        self._orders = {
            oid: o
            for oid, o in self._orders.items()
            if o.status not in (OrderStatus.FILLED, OrderStatus.CANCELLED)
        }

    def _arm_protection(self, order: Order) -> None:
        # protections only make sense on entries (BUYs in a long-only spot system)
        if (
            order.side is Side.BUY
            and order.status is OrderStatus.FILLED
            and (order.stop_loss or order.take_profit)
        ):
            self._newly_armed.append((order, _Protection(order.stop_loss, order.take_profit)))

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

    def _skips_as_marketable(self, order: Order, candle: Candle) -> bool:
        """True when ``skip`` mode drops this limit: it was marketable at posting.

        Only ``skip`` mode removes a fill. The other two modes let the touch
        rule decide and differ solely in the fee tier.
        """
        if self._marketable_limit != "skip" or order.limit_price is None:
            return False
        return is_marketable_at_posting(order.side, order.limit_price, candle.open)

    def _limit_fee_rate(self, order: Order, candle: Candle) -> float:
        """Maker, unless ``taker`` mode meets a limit marketable at posting."""
        assert order.limit_price is not None
        if self._marketable_limit == "taker" and is_marketable_at_posting(
            order.side, order.limit_price, candle.open
        ):
            return self._taker
        return self._maker

    def _try_limit(self, order: Order, candle: Candle) -> Fill | None:
        assert order.limit_price is not None
        limit = order.limit_price
        # a marketable limit's fill price is the open in both filling modes:
        # buy min(limit, open) and sell max(limit, open) both collapse to the
        # open once the limit is on the far side of it, so the mode changes
        # the fee tier and nothing else
        fee_rate = self._limit_fee_rate(order, candle)
        if order.side is Side.BUY and candle.low <= limit:
            price = min(limit, candle.open)  # gapped through -> get the open
            return self._make_fill(order, candle.ts, price, fee_rate)
        if order.side is Side.SELL and candle.high >= limit:
            price = max(limit, candle.open)
            return self._make_fill(order, candle.ts, price, fee_rate)
        return None

    def _check_protection(self, order: Order, prot: _Protection, candle: Candle) -> Fill | None:
        """Build an exit order if SL/TP triggers and a position remains.

        The caller drops protections when the net position is gone; exits
        are clamped to the remaining position.
        """
        remaining = self._positions.get(order.pair, Decimal(0))
        exit_size = float(min(Decimal(str(order.size)), remaining))
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
                self._prot_by_exit[exit_order.id] = (order, prot)
                price = self._slipped(min(prot.stop_loss, candle.open), exit_order.side)
                return self._make_fill(exit_order, candle.ts, price, self._taker)
        if prot.take_profit is not None:
            hit = (
                candle.high >= prot.take_profit if order.side is Side.BUY else candle.low <= prot.take_profit
            )
            if hit:
                self._orders[exit_order.id] = exit_order
                self._new_orders.append(exit_order)
                self._prot_by_exit[exit_order.id] = (order, prot)
                price = max(prot.take_profit, candle.open)
                return self._make_fill(exit_order, candle.ts, price, self._maker)
        return None  # still armed
