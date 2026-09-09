"""Live venue: real Kraken spot orders behind the synchronous Venue protocol.

The paper venue simulates the platform's execution model; this one enacts it
on the exchange, keeping the same candle-driven shape:

- a market order is placed the moment the strategy's intent is approved, so
  it executes at the price the next candle opens around
- a limit order is placed post-only (Kraken ``oflags=post``), which
  guarantees the maker fee the backtests assume, and lives for exactly one
  candle: at the next candle's close it is cancelled on the exchange, and
  the strategy reposts if it still wants the trade
- Kraken refusing a post-only limit as would-be-marketable is not an error.
  Paper semantics would have filled it; live semantics skip it and report it
  as expired, and the strategy re-decides next candle

**The exchange is the truth for fills.** Every :class:`Fill` this venue emits
is built from Kraken trade data — real price, real size, real fee — never
from a local estimate. One candle's trades of one order are aggregated into
one fill at their size-weighted average price, so a limit that filled 40 % and
then expired emits one fill of 40 % of the size.

Ordering inside ``on_candle`` matters: open orders are cancelled *first*, and
only then are trades polled. After a cancel returns, no further fill can land
for that order, so the poll that follows sees the final truth. Polling first
would leave a window in which a fill lands between the poll and the cancel.

Sizes round DOWN to the pair's lot precision and are clamped to the
configured notional ceiling; an order below the exchange minimum is skipped,
logged, and alerted, never rounded up.

**One live run per pair.** ``cancel_all`` sweeps every open order the
account holds on the pair, not only the ones this venue placed, because the
kill switch must leave nothing behind that a lost tracking table hides. Two
live runs on the same pair would therefore cancel each other's orders. The
supervisor allows one run per (mode, strategy, pair, timeframe) slot, and
live runs are single-pair, so this is a limitation to respect rather than a
race to defend against.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable, Coroutine
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Any, TypeVar

from kaupo.core.notify import send_alert
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
from kaupo.venues.kraken_client import (
    CALL_TIMEOUT_SECONDS,
    RETRY_ATTEMPTS,
    RETRY_BASE_SECONDS,
    AsyncBridge,
    ExchangeError,
    ExchangeTrade,
    MarketMeta,
    PostOnlyRejected,
    TradingClient,
    floor_to_step,
    retry_delay,
)

log = logging.getLogger(__name__)

T = TypeVar("T")

AlertSink = Callable[[str], Coroutine[Any, Any, None]]

# how long liquidate() waits for its market order to show up as trades
LIQUIDATION_POLL_ATTEMPTS = 6

#: order id prefix for a trade the venue cannot attribute to an order it
#: placed; the reconciler uses the same shape for missed trades
UNATTRIBUTED_PREFIX = "kraken-"


def unattributed_order_id(txid: str) -> OrderId:
    """The synthetic order id for a Kraken txid this platform did not place.

    Deterministic on purpose: recording the same txid twice is then a
    detectable duplicate rather than a second row.
    """
    return OrderId(f"{UNATTRIBUTED_PREFIX}{txid}")


@dataclass
class _LiveOrder:
    """One order this venue placed and still tracks on the exchange."""

    order: Order
    txid: str
    placed_size: float  # what was actually sent, after rounding and clamping


def aggregate_trades(order_id: OrderId, pair: Pair, trades: list[ExchangeTrade]) -> Fill:
    """One fill from one order's trades: weighted average price, summed fee.

    Decimal money math, like the ledger, so a run of partial fills does not
    drift against the accounting that consumes them.
    """
    size = sum((Decimal(str(t.size)) for t in trades), Decimal(0))
    notional = sum((Decimal(str(t.price)) * Decimal(str(t.size)) for t in trades), Decimal(0))
    fee = sum((Decimal(str(t.fee)) for t in trades), Decimal(0))
    return Fill(
        order_id=order_id,
        pair=pair,
        side=trades[0].side,
        ts=max(t.ts for t in trades),
        price=float(notional / size) if size > 0 else trades[0].price,
        size=float(size),
        fee=float(fee),
    )


class KrakenVenue:
    """Kraken spot execution behind the synchronous ``Venue`` protocol.

    Single pair, long-only, no exchange-side stops: protection logic stays
    in strategy code, evaluated per candle, exactly as in paper runs.
    """

    def __init__(
        self,
        pair: Pair,
        client: TradingClient,
        bridge: AsyncBridge,
        *,
        max_notional: float,
        trades_since_ms: int | None = None,
        seen_trade_ids: set[str] | None = None,
        alert: AlertSink | None = None,
        call_timeout: float = CALL_TIMEOUT_SECONDS,
        retry_base_seconds: float = RETRY_BASE_SECONDS,
    ) -> None:
        self._pair = pair
        self._client = client
        self._bridge = bridge
        self._max_notional = max_notional
        self._alert = alert if alert is not None else send_alert
        self._call_timeout = call_timeout
        self._retry_base = retry_base_seconds
        self._market: MarketMeta | None = None
        self._orders: dict[OrderId, Order] = {}
        self._open: dict[OrderId, _LiveOrder] = {}
        self._order_by_txid: dict[str, OrderId] = {}
        self._expired: list[Order] = []
        self._new_orders: list[Order] = []
        self._last_price: float | None = None
        # forward cursor into the account's trade history; ids at the cursor
        # timestamp are remembered so a tie cannot replay a trade
        self._trades_cursor_ms = trades_since_ms
        # trades reconciliation already accounted for: the first poll reaches
        # back to the same cursor and must not adopt them a second time
        self._seen_at_cursor: set[str] = set(seen_trade_ids or ())

    # -- Venue protocol ----------------------------------------------------

    def submit(self, order: Order) -> None:
        """Place the order on Kraken now; its fills arrive on a later candle."""
        self._orders[order.id] = order
        sized = self._size_for_exchange(order)
        if sized is None:
            return
        post_only = order.order_type is OrderType.LIMIT
        try:
            # deliberately NOT retried: a retry after an ambiguous timeout can
            # place a second real order. The strategy reposts next candle.
            txid = self._call(
                lambda: self._client.create_order(
                    self._pair,
                    order.side,
                    order.order_type.value,
                    sized,
                    order.limit_price,
                    post_only=post_only,
                ),
                attempts=1,
            )
        except PostOnlyRejected as exc:
            # would have taken liquidity: paper fills it, live skips it
            order.status = OrderStatus.CANCELLED
            self._expired.append(order)
            log.info(
                "Post-only %s %s @ %s rejected as marketable; expiring it (%s)",
                order.side.value,
                self._pair,
                order.limit_price,
                exc,
            )
            return
        except ExchangeError as exc:
            order.status = OrderStatus.REJECTED
            self._expired.append(order)
            self._warn(
                f"Kraken rejected a {order.side.value} {order.order_type.value} order on {self._pair}: {exc}"
            )
            return
        order.exchange_order_id = txid  # the audit trail's link to Kraken
        self._open[order.id] = _LiveOrder(order=order, txid=txid, placed_size=sized)
        self._order_by_txid[txid] = order.id
        log.info(
            "Placed %s %s %s size %s (requested %s)%s as %s",
            order.order_type.value,
            order.side.value,
            self._pair,
            sized,
            order.size,
            f" @ {order.limit_price}" if order.limit_price is not None else "",
            txid,
        )

    def on_candle(self, candle: Candle) -> list[Fill]:
        """Close out the candle: cancel what is still open, then read the truth."""
        self._last_price = candle.close
        self._prune_orders()
        still_open = self._cancel_tracked()
        trades = self._poll_trades()
        fills = self._fills_from(trades)
        self._finalize(still_open)
        return fills

    def drain_new_orders(self) -> list[Order]:
        orders, self._new_orders = self._new_orders, []
        return orders

    def drain_expired(self) -> list[Order]:
        orders, self._expired = self._expired, []
        return orders

    def get_order(self, order_id: OrderId) -> Order | None:
        return self._orders.get(order_id)

    def void_fill(self, fill: Fill) -> None:
        """The ledger rejected an exchange-truth fill: the books now disagree.

        Nothing is rolled back — the trade happened, the money moved, and
        pretending otherwise would put the ledger further from the exchange.
        This must never fire (the risk manager sizes orders to be affordable
        and the ledger accepts what the exchange executed), so it shouts.
        """
        log.error(
            "Ledger rejected exchange fill %s %s %s @ %s (order %s); the ledger now "
            "disagrees with Kraken and needs a human",
            fill.side.value,
            fill.size,
            fill.pair,
            fill.price,
            fill.order_id,
        )
        self._warn(
            f"LEDGER DIVERGENCE on {fill.pair}: the ledger rejected a real Kraken fill of "
            f"{fill.size} at {fill.price}. The books no longer match the exchange."
        )

    def liquidate(self, pair: Pair, size: float, candle: Candle) -> Fill:
        """Close ``size`` at market now and return the fill the exchange gave.

        ``size`` is signed, like the paper venue: positive sells the long.
        Live runs never end on their own (``liquidate_end`` is false), so
        this is the manual close-out path.

        The returned fill covers the trades the first successful poll saw.
        A liquidation that fills across several polls leaves the remainder
        to the ordinary trade poll, or to the next run's reconciliation —
        the exchange is still the truth, the fill simply lands later.
        """
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
        self._last_price = candle.close
        sized = self._size_for_exchange(order)
        if sized is None:
            raise ExchangeError(f"Liquidation of {size} {pair} is not placeable on the exchange")
        txid = self._call(
            lambda: self._client.create_order(pair, side, OrderType.MARKET.value, sized),
            attempts=1,
        )
        order.exchange_order_id = txid
        self._order_by_txid[txid] = order.id
        for attempt in range(1, LIQUIDATION_POLL_ATTEMPTS + 1):
            # the poll advances the shared cursor, so trades of other orders
            # must not be dropped on the floor: they are converted to fills
            # and reported, even though the run is already winding down
            fills = self._fills_from(self._poll_trades())
            mine = [f for f in fills if f.order_id == order.id]
            for stray in fills:
                if stray.order_id != order.id:
                    self._warn(
                        f"Trade of order {stray.order_id} on {self._pair} arrived during "
                        f"liquidation ({stray.size} at {stray.price}) and is not in the run's books"
                    )
            if mine:
                return mine[0]
            self._sleep(retry_delay(attempt, base=self._retry_base))
        raise ExchangeError(f"Liquidation order {txid} on {pair} produced no trades")

    def cancel_all(self) -> list[Order]:
        """Cancel everything this venue has open, then sweep the pair clean.

        The kill switch and every shutdown path land here, so it must leave
        no live order behind — including one whose local tracking was lost.
        The sweep is account-wide for the pair, so only one live run may
        trade a pair at a time (see the module docstring).
        """
        cancelled: list[Order] = []
        for live in list(self._open.values()):
            self._cancel_one(live.txid)
            live.order.status = OrderStatus.CANCELLED
            cancelled.append(live.order)
        self._open.clear()
        try:
            leftovers = self._call(lambda: self._client.fetch_open_order_ids(self._pair))
        except ExchangeError as exc:
            self._warn(f"Could not list open {self._pair} orders while cancelling all: {exc}")
            return cancelled
        for txid in leftovers:
            log.warning("Cancelling untracked open order %s on %s", txid, self._pair)
            self._cancel_one(txid)
        return cancelled

    def close(self) -> None:
        """Release the exchange client. The bridge belongs to whoever made it."""
        try:
            self._bridge.run_sync(self._client.close(), timeout=self._call_timeout)
        except Exception:
            log.warning("Closing the Kraken client failed", exc_info=True)

    # -- internals ---------------------------------------------------------

    def _call(self, factory: Callable[[], Coroutine[Any, Any, T]], attempts: int = RETRY_ATTEMPTS) -> T:
        """Run one exchange call on the bridge, retrying idempotent failures.

        ``attempts=1`` is mandatory for order placement: a retry after an
        ambiguous timeout can place a second real order.
        """
        last: Exception | None = None
        for attempt in range(1, attempts + 1):
            try:
                return self._bridge.run_sync(factory(), timeout=self._call_timeout)
            except PostOnlyRejected:
                raise
            except Exception as exc:
                last = exc
                if attempt < attempts:
                    delay = retry_delay(attempt, base=self._retry_base)
                    log.warning(
                        "Exchange call failed (%d of %d); retrying in %.0fs: %s",
                        attempt,
                        attempts,
                        delay,
                        exc,
                    )
                    self._sleep(delay)
        assert last is not None
        if isinstance(last, ExchangeError):
            raise last
        raise ExchangeError(str(last)) from last

    def _sleep(self, seconds: float) -> None:
        """Back off between attempts, on the bridge loop so the thread stays idle."""
        self._bridge.run_sync(asyncio.sleep(seconds), timeout=seconds + self._call_timeout)

    def _warn(self, message: str) -> None:
        """Log and push an ntfy alert. Alerting must never break trading."""
        log.warning(message)
        try:
            self._bridge.run_sync(self._alert(message), timeout=self._call_timeout)
        except Exception:
            log.warning("Alert delivery failed", exc_info=True)

    def _meta(self) -> MarketMeta:
        if self._market is None:
            self._market = self._call(lambda: self._client.load_market(self._pair))
        return self._market

    def _size_for_exchange(self, order: Order) -> float | None:
        """The size to send, or None when the order must be skipped.

        Clamps to the notional ceiling, rounds DOWN to the lot step, and
        refuses anything under the exchange minimums. A skipped order is
        marked rejected and reported through ``drain_expired`` so the audit
        trail keeps it.
        """
        price = order.limit_price if order.limit_price is not None else self._last_price
        if price is None or price <= 0:
            self._skip(order, "no reference price for the order notional")
            return None
        try:
            meta = self._meta()
        except ExchangeError as exc:
            self._skip(order, f"the {self._pair} market rules are unavailable: {exc}")
            return None

        size = order.size
        if size * price > self._max_notional:
            size = self._max_notional / price
            self._warn(
                f"Live notional cap: {order.side.value} {order.size} {self._pair} at {price} "
                f"exceeds {self._max_notional}; sending {size} instead"
            )
        size = floor_to_step(size, meta.amount_step)
        if size <= 0:
            self._skip(order, f"size rounds down to zero at a lot step of {meta.amount_step}")
            return None
        if meta.min_amount and size < meta.min_amount:
            self._skip(order, f"size {size} is below the exchange minimum of {meta.min_amount}")
            return None
        if meta.min_cost and size * price < meta.min_cost:
            self._skip(order, f"notional {size * price:.2f} is below the exchange minimum of {meta.min_cost}")
            return None
        return size

    def _skip(self, order: Order, reason: str) -> None:
        order.status = OrderStatus.REJECTED
        self._expired.append(order)
        self._warn(f"Skipped {order.side.value} {order.size} {self._pair}: {reason}")

    def _cancel_one(self, txid: str) -> bool:
        """Cancel one exchange order; False when it could not be cancelled."""
        try:
            self._call(lambda: self._client.cancel_order(txid, self._pair))
        except ExchangeError as exc:
            self._warn(f"Could not cancel {self._pair} order {txid}: {exc}")
            return False
        return True

    def _cancel_tracked(self) -> dict[OrderId, _LiveOrder]:
        """Cancel every tracked open order; returns the ones still open after it.

        An order that could not be cancelled stays tracked: it can still
        fill, so the next candle retries the cancel and keeps polling it.
        """
        stuck: dict[OrderId, _LiveOrder] = {}
        for order_id, live in self._open.items():
            if not self._cancel_one(live.txid):
                stuck[order_id] = live
        return stuck

    def _poll_trades(self) -> list[ExchangeTrade]:
        """New account trades since the cursor, deduplicated across ties."""
        try:
            trades = self._call(lambda: self._client.fetch_my_trades(self._pair, self._trades_cursor_ms))
        except ExchangeError as exc:
            self._warn(f"Could not read {self._pair} trades from Kraken: {exc}")
            return []
        fresh = [t for t in trades if t.id not in self._seen_at_cursor]
        if not fresh:
            return []
        newest = max(t.ts for t in fresh)
        newest_ms = int(newest.timestamp() * 1000)
        if newest_ms == self._trades_cursor_ms:
            self._seen_at_cursor |= {t.id for t in fresh}
        else:
            self._trades_cursor_ms = newest_ms
            self._seen_at_cursor = {t.id for t in fresh if t.ts == newest}
        return fresh

    def _fills_from(self, trades: list[ExchangeTrade]) -> list[Fill]:
        """Group trades by exchange order and turn each group into one fill."""
        by_txid: dict[str, list[ExchangeTrade]] = {}
        for trade in trades:
            by_txid.setdefault(trade.txid, []).append(trade)
        fills: list[Fill] = []
        for txid, group in by_txid.items():
            order_id = self._order_by_txid.get(txid)
            if order_id is None:
                fills.append(self._adopt_unattributed(txid, group))
                continue
            fill = aggregate_trades(order_id, self._pair, group)
            order = self._orders.get(order_id)
            if order is not None:
                self._apply_fill_to_order(order, fill)
            fills.append(fill)
        return fills

    def _adopt_unattributed(self, txid: str, trades: list[ExchangeTrade]) -> Fill:
        """Record a trade of an order this venue did not place.

        The money moved, so the books must reflect it: a synthetic order
        carries the fill into the audit trail, and a human is told.
        """
        order_id = unattributed_order_id(txid)
        fill = aggregate_trades(order_id, self._pair, trades)
        order = Order(
            pair=self._pair,
            side=fill.side,
            order_type=OrderType.MARKET,
            size=fill.size,
            reason=f"unattributed Kraken order {txid}",
            id=order_id,
            created_ts=fill.ts,
            exchange_order_id=txid,
        )
        self._apply_fill_to_order(order, fill)
        self._orders[order_id] = order
        self._order_by_txid[txid] = order_id
        self._new_orders.append(order)
        self._warn(
            f"Unattributed Kraken trade on {self._pair}: order {txid} filled "
            f"{fill.size} at {fill.price}. Recorded against the run; check the account."
        )
        return fill

    @staticmethod
    def _apply_fill_to_order(order: Order, fill: Fill) -> None:
        """Stamp the exchange's truth onto the order the engine will record.

        A partially filled order is FILLED, not CANCELLED: the order row
        keeps the requested size, the fill row carries what executed, so the
        audit trail shows both numbers.
        """
        order.status = OrderStatus.FILLED
        order.filled_price = fill.price
        order.filled_ts = fill.ts
        order.fee = fill.fee

    def _finalize(self, still_open: dict[OrderId, _LiveOrder]) -> None:
        """Retire this candle's orders: filled ones are done, the rest expired."""
        for order_id, live in self._open.items():
            if order_id in still_open:
                continue  # cancel failed: keep tracking it into the next candle
            if live.order.status is OrderStatus.FILLED:
                continue
            live.order.status = OrderStatus.CANCELLED
            self._expired.append(live.order)
            log.info(
                "Limit order %s (%s %s @ %s) expired unfilled and was cancelled on Kraken",
                live.order.id,
                live.order.side.value,
                self._pair,
                live.order.limit_price,
            )
        self._open = still_open

    def _prune_orders(self) -> None:
        """Drop closed orders to bound memory in long-lived runs."""
        keep = set(self._open)
        self._orders = {
            oid: o
            for oid, o in self._orders.items()
            if oid in keep or o.status not in (OrderStatus.FILLED, OrderStatus.CANCELLED)
        }
        live_txids = {live.txid for live in self._open.values()}
        self._order_by_txid = {
            txid: oid
            for txid, oid in self._order_by_txid.items()
            if txid in live_txids or oid in self._orders
        }


def last_trade_cursor_ms(ts: datetime | None) -> int | None:
    """Milliseconds cursor for the trade poll, or None to start from now."""
    return int(ts.timestamp() * 1000) if ts is not None else None
