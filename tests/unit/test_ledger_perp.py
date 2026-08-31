"""Perp ledger: shorts, flips, realized PnL, funding, liquidation force."""

from datetime import UTC, datetime
from decimal import Decimal

import pytest

from kaupo.domain import Fill, OrderId, Pair, Side
from kaupo.ledger.ledger import InsufficientFunds, InsufficientPosition, Ledger

PAIR = Pair.parse("BTC/EUR")
BASE = datetime(2026, 1, 1, tzinfo=UTC)


def fill(side: Side, size: float, price: float, fee: float = 0.0) -> Fill:
    return Fill(order_id=OrderId("o"), pair=PAIR, side=side, ts=BASE, price=price, size=size, fee=fee)


def ledger() -> Ledger:
    return Ledger("EUR", 10_000.0, BASE, perp=True)


def test_open_short_and_equity_both_directions() -> None:
    led = ledger()
    led.apply_fill(fill(Side.SELL, 1.0, 100.0, fee=1.0))

    pos = led.position(PAIR)
    assert pos.size == -1.0
    # the short's cost basis is the effective sell price (fee out)
    assert pos.avg_entry == 99.0
    assert led.cash == Decimal(10_000) + Decimal(100) - Decimal(1)
    # equity is exact in both directions: 10099 cash + (-1 * mark)
    assert led.equity({PAIR: 100.0}) == Decimal(10_000) - Decimal(1)  # the fee
    assert led.equity({PAIR: 110.0}) == Decimal(10_000) - Decimal(1) - Decimal(10)
    assert led.equity({PAIR: 90.0}) == Decimal(10_000) - Decimal(1) + Decimal(10)


def test_cover_realizes_pnl_with_both_fees() -> None:
    led = ledger()
    led.apply_fill(fill(Side.SELL, 1.0, 100.0, fee=1.0))  # basis 99
    realized = led.apply_fill(fill(Side.BUY, 1.0, 90.0, fee=1.0))

    assert realized == Decimal(1) * (Decimal(99) - Decimal(90)) - Decimal(1)  # 8
    pos = led.position(PAIR)
    assert pos.size == 0
    assert pos.avg_entry == 0.0
    assert led.equity({PAIR: 100.0}) == led.cash == Decimal(10_000) + Decimal(8)


def test_short_add_averages_the_basis_down() -> None:
    led = ledger()
    led.apply_fill(fill(Side.SELL, 1.0, 100.0))  # basis 100
    led.apply_fill(fill(Side.SELL, 1.0, 120.0))  # basis (100 + 120) / 2

    pos = led.position(PAIR)
    assert pos.size == -2.0
    assert pos.avg_entry == 110.0
    realized = led.apply_fill(fill(Side.BUY, 2.0, 100.0))
    assert realized == Decimal(2) * (Decimal(110) - Decimal(100))


def test_flip_short_to_long_is_one_fill() -> None:
    led = ledger()
    led.apply_fill(fill(Side.SELL, 1.0, 100.0))  # short 1 @ 100
    realized = led.apply_fill(fill(Side.BUY, 3.0, 90.0))  # cover 1 @ 90, long 2 @ 90

    assert realized == Decimal(1) * (Decimal(100) - Decimal(90))  # the covered part
    pos = led.position(PAIR)
    assert pos.size == 2.0
    assert pos.avg_entry == 90.0  # the long part starts a fresh basis


def test_flip_long_to_short_is_one_fill() -> None:
    led = ledger()
    led.apply_fill(fill(Side.BUY, 1.0, 100.0))
    realized = led.apply_fill(fill(Side.SELL, 3.0, 110.0))  # close 1 @ 110, short 2 @ 110

    assert realized == Decimal(1) * (Decimal(110) - Decimal(100))
    pos = led.position(PAIR)
    assert pos.size == -2.0
    assert pos.avg_entry == 110.0


def test_buy_beyond_cash_still_rejected_without_force() -> None:
    led = ledger()
    with pytest.raises(InsufficientFunds):
        led.apply_fill(fill(Side.BUY, 200.0, 100.0))  # 20k cost, 10k cash
    # but a forced liquidation fill executes regardless
    led.apply_fill(fill(Side.SELL, 100.0, 100.0))  # short 10k notional
    led.apply_liquidation_fill(fill(Side.BUY, 100.0, 220.0))  # cover at 22k > cash
    assert led.position(PAIR).size == 0


def test_funding_moves_cash_and_leaves_an_entry() -> None:
    led = ledger()
    led.apply_fill(fill(Side.SELL, 1.0, 100.0))
    led.apply_funding(BASE, Decimal("-5.00"))

    assert led.cash == Decimal(10_000) + Decimal(100) - Decimal(5)
    entries = led.drain_entries()
    funding = [e for e in entries if e.reason == "funding"]
    assert len(funding) == 1
    assert funding[0].amount == Decimal("-5.00")


def test_spot_ledger_still_rejects_oversell() -> None:
    led = Ledger("EUR", 10_000.0, BASE)  # perp=False default
    led.apply_fill(fill(Side.BUY, 1.0, 100.0))
    with pytest.raises(InsufficientPosition):
        led.apply_fill(fill(Side.SELL, 2.0, 100.0))
