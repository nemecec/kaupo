"""Crash reconciliation: make the books match Kraken before a live run trades.

A live run can die anywhere — mid-placement, between a fill and its database
write, during a deploy. On the way back up, three things must be true before
the engine sees a candle:

1. **No double orders.** Every open order on the exchange for the pair is
   cancelled. The platform never adopts an open order: strategies repost
   every candle anyway, so adopting one buys complexity and no fills.
2. **No silently lost fills.** Every trade newer than the last fill in the
   database is recorded, even one that cannot be matched to a known order.
   The money moved; the books must say so.
3. **The ledger equals the exchange.** Replayed positions and cash are
   compared against the account balances. Dust-sized drift is logged and
   accepted; anything larger refuses the start and calls a human.

Deduping is keyed on the exchange order id, never on a timestamp: Kraken
stamps the trades of one order with the same millisecond, so a ``ts >``
cursor alone could replay a trade or skip one. A trade whose txid appears on
a recorded order belongs to that order; any other txid gets the synthetic
order id ``kraken-<txid>``. Either way the recorded size for that order is
compared against the exchange's total, so a re-run records nothing twice and
a half-recorded order contributes only its remainder.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from kaupo.core.notify import send_alert
from kaupo.db.models import FillRow, OrderRow, RunRow
from kaupo.db.session import sm_scope
from kaupo.domain import (
    Fill,
    Order,
    OrderId,
    OrderStatus,
    OrderType,
    Pair,
    Position,
    RunMode,
    utc_now,
)
from kaupo.venues.kraken_client import ExchangeError, ExchangeTrade, TradingClient
from kaupo.venues.kraken_live import aggregate_trades, unattributed_order_id

log = logging.getLogger(__name__)

#: How far the exchange balance may sit from the replayed ledger before the
#: run refuses to start. Fees and lot rounding leave dust behind, so an exact
#: match is not achievable; anything above this is a real discrepancy.
BASE_TOLERANCE = 1e-6  # base-asset units
QUOTE_TOLERANCE = 1.0  # quote currency (EUR)

#: A trade at exactly the last recorded fill's timestamp still has to be
#: inspected (Kraken ties timestamps within an order), so the fetch reaches
#: back a little before the cursor and the order identity does the deduping.
CURSOR_MARGIN_MS = 1000

#: Size below which a difference between the exchange's total for an order
#: and the recorded total counts as float noise, not a missing fill.
SIZE_TOLERANCE = 1e-12


class ReconciliationRefused(Exception):
    """The books and the exchange disagree beyond tolerance; a human decides."""


@dataclass(frozen=True)
class ReconcileResult:
    """What reconciliation did, and what the run must carry into the engine.

    ``missed`` pairs each recovered trade with the synthetic order that
    carries it into the audit trail. The caller applies the fills to the
    resumed ledger and records both once the run row exists.
    """

    cancelled_txids: list[str] = field(default_factory=list)
    missed: list[tuple[Order, Fill]] = field(default_factory=list)
    balances: dict[str, float] = field(default_factory=dict)
    trades_cursor_ms: int | None = None
    #: every trade id this pass read. The venue starts with them marked as
    #: seen, so a trade recovered here is never adopted a second time when
    #: the venue's first poll reaches back to the same cursor.
    seen_trade_ids: set[str] = field(default_factory=set)

    @property
    def missed_fills(self) -> list[Fill]:
        return [fill for _, fill in self.missed]


async def last_recorded_fill_ts(session: AsyncSession, pair: Pair) -> datetime | None:
    """Timestamp of the newest fill any live run recorded for the pair."""
    return (
        await session.execute(
            select(FillRow.ts)
            .join(RunRow, RunRow.id == FillRow.run_id)
            .where(RunRow.mode == RunMode.LIVE.value, FillRow.pair == str(pair))
            .order_by(FillRow.ts.desc())
            .limit(1)
        )
    ).scalar_one_or_none()


async def _order_ids_by_txid(session: AsyncSession, txids: list[str]) -> dict[str, str]:
    """Kraken txid to the platform order id that placed it, for known orders."""
    if not txids:
        return {}
    rows = (
        await session.execute(
            select(OrderRow.exchange_order_id, OrderRow.id).where(OrderRow.exchange_order_id.in_(txids))
        )
    ).all()
    return {str(txid): str(order_id) for txid, order_id in rows}


async def _recorded_sizes(session: AsyncSession, order_ids: list[str]) -> dict[str, float]:
    """Total filled size already recorded per order id."""
    if not order_ids:
        return {}
    rows = (
        await session.execute(
            select(FillRow.order_id, func.sum(FillRow.size))
            .where(FillRow.order_id.in_(order_ids))
            .group_by(FillRow.order_id)
        )
    ).all()
    return {str(order_id): float(total or 0.0) for order_id, total in rows}


def _order_for(pair: Pair, txid: str, fill: Fill, *, known: bool) -> Order:
    """The order row a recovered fill hangs off.

    For a txid the platform placed, this is a shell carrying the same id:
    the recorder upserts only status, fill price, fill time, fee, and the
    exchange id, so the original size, side, and reason survive. For an
    unknown txid it is the synthetic order that carries the trade into the
    audit trail.
    """
    order = Order(
        pair=pair,
        side=fill.side,
        order_type=OrderType.MARKET,
        size=fill.size,
        reason="" if known else f"recovered Kraken order {txid}",
        id=OrderId(fill.order_id),
        created_ts=fill.ts,
        exchange_order_id=txid,
    )
    order.status = OrderStatus.FILLED
    order.filled_price = fill.price
    order.filled_ts = fill.ts
    order.fee = fill.fee
    return order


def _remainder_fill(fill: Fill, remainder: float) -> Fill:
    """The unrecorded part of a partly recorded order, priced the same.

    The fee is pro-rated by size: the exchange charges per executed volume,
    so the share that belongs to the missing part is its share of the size.
    """
    share = Decimal(str(remainder)) / Decimal(str(fill.size))
    return Fill(
        order_id=fill.order_id,
        pair=fill.pair,
        side=fill.side,
        ts=fill.ts,
        price=fill.price,
        size=remainder,
        fee=float(Decimal(str(fill.fee)) * share),
    )


def balance_drift(
    balances: dict[str, float],
    pair: Pair,
    positions: dict[Pair, Position],
    cash: Decimal,
) -> tuple[float, float]:
    """(base drift, quote drift) between the exchange and the replayed ledger.

    Positive means the exchange holds more than the books say.
    """
    position = positions.get(pair)
    ledger_base = position.size if position is not None else 0.0
    base_drift = balances.get(pair.base, 0.0) - ledger_base
    quote_drift = balances.get(pair.quote, 0.0) - float(cash)
    return base_drift, quote_drift


async def reconcile_live(
    client: TradingClient,
    sessionmaker: async_sessionmaker[AsyncSession],
    *,
    pair: Pair,
    positions: dict[Pair, Position],
    cash: Decimal,
    history_since: datetime | None,
    check_quote: bool,
    now: datetime | None = None,
    base_tolerance: float = BASE_TOLERANCE,
    quote_tolerance: float = QUOTE_TOLERANCE,
) -> ReconcileResult:
    """Bring the exchange and the books into agreement, or refuse to start.

    ``positions`` and ``cash`` are the state the resume machinery replayed
    from the recorded fills.

    ``history_since`` is the earliest moment whose trades belong to this
    run's chain — the chain root's start. ``None`` means there is no chain:
    a fresh run recovers no history at all, because every trade on the
    account predates the platform's involvement with the pair, and adopting
    one would invent fills the run never made. Its trade cursor is seeded at
    ``now`` instead, so only what this run does from here counts.

    ``check_quote`` follows the same split: a fresh run's ledger opens at a
    configured starting cash, which the account balance has no reason to
    match. The base-asset check always applies — that one is comparable in
    every case, and it is what catches an account that already holds the
    base asset.

    Raises :class:`ReconciliationRefused` when the drift is beyond tolerance.
    """
    now = now if now is not None else utc_now()
    cancelled = await _cancel_open_orders(client, pair)
    trades: list[ExchangeTrade] = []
    missed: list[tuple[Order, Fill]] = []
    cursor_ms = int(now.timestamp() * 1000)
    if history_since is not None:
        async with sm_scope(sessionmaker) as session:
            recorded_until = await last_recorded_fill_ts(session, pair)
        # never reach back past the chain root: older trades are not this
        # run's, however empty the chain's fill history is
        since = max(history_since, recorded_until) if recorded_until is not None else history_since
        trades = await _fetch_since(client, pair, since)
        missed = await _recover_missed(sessionmaker, pair, trades)
        cursor_ms = max(
            (int(t.ts.timestamp() * 1000) for t in trades),
            default=int(since.timestamp() * 1000),
        )
    else:
        log.info(
            "Fresh live run on %s: no chain to recover trades into, so the account's own "
            "history stays out of the books; the trade cursor starts at %s",
            pair,
            now,
        )

    for order, fill in missed:
        log.warning(
            "Recovered unrecorded Kraken trade %s: %s %s at %s (fee %s)",
            order.id,
            fill.side.value,
            fill.size,
            fill.price,
            fill.fee,
        )
    if missed:
        await send_alert(
            f"Live reconciliation recovered {len(missed)} unrecorded {pair} trade(s) from Kraken; "
            "they are now in the run's books."
        )

    balances = await _fetch_balances(client)
    carried = _with_missed(pair, positions, missed)
    carried_cash = cash + sum((_cash_delta(fill) for fill in (f for _, f in missed)), Decimal(0))
    base_drift, quote_drift = balance_drift(balances, pair, carried, carried_cash)
    await _check_drift(
        pair,
        base_drift,
        quote_drift,
        check_quote=check_quote,
        base_tolerance=base_tolerance,
        quote_tolerance=quote_tolerance,
    )
    return ReconcileResult(
        cancelled_txids=cancelled,
        missed=missed,
        balances=balances,
        trades_cursor_ms=cursor_ms,
        seen_trade_ids={t.id for t in trades},
    )


async def _cancel_open_orders(client: TradingClient, pair: Pair) -> list[str]:
    """Cancel every open order for the pair. Restart never adopts an order."""
    txids = await client.fetch_open_order_ids(pair)
    for txid in txids:
        await client.cancel_order(txid, pair)
        log.warning("Reconciliation cancelled open %s order %s", pair, txid)
    if txids:
        await send_alert(
            f"Live reconciliation cancelled {len(txids)} open {pair} order(s) left behind by a restart."
        )
    return txids


async def _fetch_since(client: TradingClient, pair: Pair, since: datetime | None) -> list[ExchangeTrade]:
    since_ms = int(since.timestamp() * 1000) - CURSOR_MARGIN_MS if since is not None else None
    return await client.fetch_my_trades(pair, since_ms)


async def _recover_missed(
    sessionmaker: async_sessionmaker[AsyncSession],
    pair: Pair,
    trades: list[ExchangeTrade],
) -> list[tuple[Order, Fill]]:
    """The fills the database is missing, one per exchange order.

    Every trade is attributed through the exchange order id: to the platform
    order that placed it when the audit trail knows the txid, otherwise to a
    synthetic ``kraken-<txid>`` order. An order whose recorded fills already
    cover the exchange's total is skipped, and one that is only partly
    recorded contributes the remainder. That makes a re-run a no-op and
    leaves no trade behind.
    """
    by_txid: dict[str, list[ExchangeTrade]] = {}
    for trade in trades:
        by_txid.setdefault(trade.txid, []).append(trade)
    if not by_txid:
        return []
    async with sm_scope(sessionmaker) as session:
        known = await _order_ids_by_txid(session, sorted(by_txid))
        targets = {txid: known.get(txid, str(unattributed_order_id(txid))) for txid in by_txid}
        recorded = await _recorded_sizes(session, sorted(set(targets.values())))

    missed: list[tuple[Order, Fill]] = []
    for txid, group in sorted(by_txid.items()):
        order_id = targets[txid]
        fill = aggregate_trades(OrderId(order_id), pair, group)
        remainder = fill.size - recorded.get(order_id, 0.0)
        if remainder <= SIZE_TOLERANCE:
            continue  # already in the books
        if remainder < fill.size:
            fill = _remainder_fill(fill, remainder)
        missed.append((_order_for(pair, txid, fill, known=txid in known), fill))
    return missed


async def _fetch_balances(client: TradingClient) -> dict[str, float]:
    try:
        return await client.fetch_balances()
    except ExchangeError as exc:
        raise ReconciliationRefused(f"Could not read Kraken balances: {exc}") from exc


def _cash_delta(fill: Fill) -> Decimal:
    """What a recovered fill did to quote cash: buys spend, sells receive."""
    amount = Decimal(str(fill.price)) * Decimal(str(fill.size))
    fee = Decimal(str(fill.fee))
    return -(amount + fee) if fill.side.value == "buy" else amount - fee


def _with_missed(
    pair: Pair, positions: dict[Pair, Position], missed: list[tuple[Order, Fill]]
) -> dict[Pair, Position]:
    """The replayed positions with the recovered fills folded in."""
    current = positions.get(pair)
    size = Decimal(str(current.size)) if current is not None else Decimal(0)
    for _, fill in missed:
        delta = Decimal(str(fill.size))
        size += delta if fill.side.value == "buy" else -delta
    merged = dict(positions)
    merged[pair] = Position(
        pair=pair,
        size=float(size),
        avg_entry=current.avg_entry if current is not None else 0.0,
    )
    return merged


async def _check_drift(
    pair: Pair,
    base_drift: float,
    quote_drift: float,
    *,
    check_quote: bool,
    base_tolerance: float,
    quote_tolerance: float,
) -> None:
    """Accept dust, refuse anything larger. The human resolves real drift."""
    log.info(
        "Reconciliation drift for %s: base %+.10f %s, quote %+.4f %s (quote checked: %s)",
        pair,
        base_drift,
        pair.base,
        quote_drift,
        pair.quote,
        check_quote,
    )
    problems = []
    if abs(base_drift) > base_tolerance:
        problems.append(f"{pair.base} off by {base_drift:+.10f} (tolerance {base_tolerance})")
    if check_quote and abs(quote_drift) > quote_tolerance:
        problems.append(f"{pair.quote} off by {quote_drift:+.4f} (tolerance {quote_tolerance})")
    if not problems:
        return
    detail = "; ".join(problems)
    await send_alert(f"Live run refused to start on {pair}: exchange and ledger disagree — {detail}")
    raise ReconciliationRefused(f"Exchange and ledger disagree on {pair}: {detail}")
