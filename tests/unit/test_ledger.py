from datetime import UTC, datetime
from decimal import Decimal

import pytest

from kaupo.domain import Fill, OrderId, Pair, Side
from kaupo.ledger.ledger import InsufficientFunds, InsufficientPosition, Ledger

PAIR = Pair.parse("BTC/EUR")
TS = datetime(2026, 1, 1, tzinfo=UTC)


def fill(side: Side, price: float, size: float, fee: float = 0.0) -> Fill:
    return Fill(order_id=OrderId("o1"), pair=PAIR, side=side, ts=TS, price=price, size=size, fee=fee)


def test_initial_deposit() -> None:
    ledger = Ledger("EUR", 1000.0, TS)
    assert ledger.cash == Decimal("1000")
    assert len(ledger.entries) == 1
    assert ledger.entries[0].reason == "deposit"


def test_buy_updates_cash_position_and_entries() -> None:
    ledger = Ledger("EUR", 1000.0, TS)
    realized = ledger.apply_fill(fill(Side.BUY, price=100.0, size=2.0, fee=1.0))

    assert realized == 0
    assert ledger.cash == Decimal("799")
    pos = ledger.position(PAIR)
    assert pos.size == 2.0
    assert pos.avg_entry == 100.0

    entries = ledger.entries[1:]  # skip deposit
    assert [(e.asset, e.amount) for e in entries] == [
        ("EUR", Decimal("-201")),
        ("BTC", Decimal("2")),
    ]
    # balance_after for BTC entry reflects the updated position
    assert entries[1].balance_after == Decimal("2")


def test_avg_entry_on_multiple_buys() -> None:
    ledger = Ledger("EUR", 1000.0, TS)
    ledger.apply_fill(fill(Side.BUY, 100.0, 1.0))
    ledger.apply_fill(fill(Side.BUY, 200.0, 1.0))
    pos = ledger.position(PAIR)
    assert pos.avg_entry == pytest.approx(150.0)


def test_sell_realizes_pnl_and_returns_cash() -> None:
    ledger = Ledger("EUR", 1000.0, TS)
    ledger.apply_fill(fill(Side.BUY, 100.0, 2.0))
    realized = ledger.apply_fill(fill(Side.SELL, 150.0, 2.0, fee=2.0))

    assert realized == Decimal("98")  # 2*(150-100) - 2 fee
    assert ledger.realized_pnl == Decimal("98")
    assert ledger.cash == Decimal("1098")
    assert ledger.position(PAIR).size == 0
    assert ledger.position(PAIR).avg_entry == 0.0


def test_partial_sell_keeps_avg_entry() -> None:
    ledger = Ledger("EUR", 1000.0, TS)
    ledger.apply_fill(fill(Side.BUY, 100.0, 2.0))
    ledger.apply_fill(fill(Side.SELL, 150.0, 1.0))
    pos = ledger.position(PAIR)
    assert pos.size == 1.0
    assert pos.avg_entry == 100.0


def test_insufficient_funds() -> None:
    ledger = Ledger("EUR", 100.0, TS)
    with pytest.raises(InsufficientFunds):
        ledger.apply_fill(fill(Side.BUY, 100.0, 2.0))


def test_insufficient_position() -> None:
    ledger = Ledger("EUR", 100.0, TS)
    with pytest.raises(InsufficientPosition):
        ledger.apply_fill(fill(Side.SELL, 100.0, 1.0))


def test_equity_marks_positions_to_market() -> None:
    ledger = Ledger("EUR", 1000.0, TS)
    ledger.apply_fill(fill(Side.BUY, 100.0, 2.0))  # cash 800, 2 BTC
    assert ledger.equity({PAIR: 120.0}) == Decimal("1040")
    assert ledger.equity({}) == Decimal("800")  # no price -> positions valued at 0


def test_drain_entries() -> None:
    ledger = Ledger("EUR", 1000.0, TS)
    ledger.apply_fill(fill(Side.BUY, 100.0, 1.0))
    entries = ledger.drain_entries()
    assert len(entries) == 3  # deposit + 2 trade entries
    assert ledger.drain_entries() == []
