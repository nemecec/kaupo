"""Perp risk assessment: shorts within caps, cooldown, dust, batch folding."""

from datetime import UTC, datetime

from kaupo.domain import OrderIntent, OrderType, Pair, Side
from kaupo.risk.manager import Decision, RiskConfig, RiskManager, RiskState

PAIR = Pair.parse("BTC/EUR")
NOW = datetime(2026, 1, 1, tzinfo=UTC)


def manager(**overrides: float) -> RiskManager:
    cfg = RiskConfig(instrument="perp", **overrides)
    return RiskManager(cfg)


def intent(side: Side, size: float) -> OrderIntent:
    return OrderIntent(pair=PAIR, side=side, order_type=OrderType.MARKET, size=size)


def state(cash: float = 10_000.0, size: float = 0.0, avg: float = 0.0, price: float = 100.0) -> RiskState:
    from kaupo.domain import Position

    return RiskState(
        ts=NOW,
        cash=cash,
        positions={PAIR: Position(pair=PAIR, size=size, avg_entry=avg)} if size != 0 else {},
        prices={PAIR: price},
        equity=cash + size * price,
    )


def test_short_entry_within_caps_approved() -> None:
    rm = manager(max_position_quote=1_000.0, max_gross_exposure_quote=2_000.0)
    (a,) = rm.assess([intent(Side.SELL, 5.0)], state())  # 500 quote short
    assert a.decision is Decision.APPROVED
    assert a.size == 5.0


def test_short_entry_resized_to_the_cap() -> None:
    rm = manager(max_position_quote=1_000.0, max_gross_exposure_quote=2_000.0)
    (a,) = rm.assess([intent(Side.SELL, 50.0)], state())  # wants 5000, cap 1000
    assert a.decision is Decision.RESIZED
    assert a.size == 10.0  # 1000 quote


def test_short_entry_over_cap_when_already_short() -> None:
    rm = manager(max_position_quote=1_000.0, max_gross_exposure_quote=2_000.0)
    (a,) = rm.assess([intent(Side.SELL, 5.0)], state(size=-10.0, avg=100.0))  # already at cap
    assert a.decision is Decision.REJECTED
    assert "exposure cap" in a.reason


def test_cover_always_allowed_even_at_cap() -> None:
    rm = manager(max_position_quote=1_000.0, max_gross_exposure_quote=2_000.0)
    (a,) = rm.assess([intent(Side.BUY, 10.0)], state(size=-10.0, avg=100.0))  # full cover
    assert a.decision is Decision.APPROVED
    assert a.size == 10.0


def test_flip_counts_the_resulting_magnitude() -> None:
    rm = manager(max_position_quote=1_000.0, max_gross_exposure_quote=2_000.0)
    # long 5 (500 quote); a 20-size sell flips to short 15 (1500 quote) -> resized
    (a,) = rm.assess([intent(Side.SELL, 20.0)], state(size=5.0, avg=100.0))
    assert a.decision is Decision.RESIZED
    assert a.size == 15.0  # close 5, open 10 (1000 quote short)


def test_cooldown_gates_short_entries_but_not_covers() -> None:
    rm = manager(max_position_quote=1_000.0, max_gross_exposure_quote=2_000.0, max_consecutive_losses=1)
    rm.notify_trade_result(-50.0)  # start the cooldown
    (entry,) = rm.assess([intent(Side.SELL, 5.0)], state())
    assert entry.decision is Decision.REJECTED
    assert "cooldown" in entry.reason
    (cover,) = rm.assess([intent(Side.BUY, 5.0)], state(size=-5.0, avg=100.0))
    assert cover.decision is Decision.APPROVED


def test_dust_short_rejected_but_dust_full_close_allowed() -> None:
    rm = manager(max_position_quote=1_000.0, max_gross_exposure_quote=2_000.0, min_order_quote=10.0)
    (dust,) = rm.assess([intent(Side.SELL, 0.05)], state())  # 5 quote
    assert dust.decision is Decision.REJECTED
    (close,) = rm.assess([intent(Side.BUY, 0.05)], state(size=-0.05, avg=100.0))  # full cover
    assert close.decision is Decision.APPROVED


def test_batch_cannot_stack_over_the_cap() -> None:
    rm = manager(max_position_quote=1_000.0, max_gross_exposure_quote=2_000.0)
    first, second = rm.assess([intent(Side.SELL, 8.0), intent(Side.SELL, 8.0)], state())
    assert first.decision is Decision.APPROVED
    assert second.decision is Decision.RESIZED  # 800 + resized to 1000 total
    assert second.size == 2.0


def test_spot_mode_still_clamps_sells_to_the_position() -> None:
    rm = RiskManager(RiskConfig())  # instrument=spot default
    (a,) = rm.assess([intent(Side.SELL, 10.0)], state(size=5.0, avg=100.0))
    assert a.decision is Decision.RESIZED
    assert a.size == 5.0
    assert a.reason == "clamped to position"
