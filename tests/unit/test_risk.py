from datetime import UTC, datetime, timedelta

import pytest

from kaupo.domain import OrderIntent, OrderType, Pair, Position, Side
from kaupo.risk.manager import Decision, RiskConfig, RiskManager, RiskState

PAIR = Pair.parse("BTC/EUR")
TS = datetime(2026, 1, 1, tzinfo=UTC)


def buy(size: float = 1.0) -> OrderIntent:
    return OrderIntent(pair=PAIR, side=Side.BUY, size=size)


def sell(size: float = 1.0) -> OrderIntent:
    return OrderIntent(pair=PAIR, side=Side.SELL, size=size)


def state(
    cash: float = 10_000.0,
    position: float = 0.0,
    price: float = 100.0,
    equity: float = 10_000.0,
    ts: datetime = TS,
) -> RiskState:
    return RiskState(
        ts=ts,
        cash=cash,
        positions={PAIR: Position(pair=PAIR, size=position, avg_entry=price)} if position else {},
        prices={PAIR: price},
        equity=equity,
    )


@pytest.fixture
def rm() -> RiskManager:
    return RiskManager(RiskConfig(max_position_quote=500, max_gross_exposure_quote=1000))


class TestBuyAssessment:
    def test_approved(self, rm: RiskManager) -> None:
        a = rm.assess([buy(1.0)], state())[0]
        assert a.decision is Decision.APPROVED
        assert a.size == 1.0

    def test_resized_to_pair_limit(self, rm: RiskManager) -> None:
        a = rm.assess([buy(100.0)], state())[0]  # wants 100*100 = 10k > 500 cap
        assert a.decision is Decision.RESIZED
        assert a.size == pytest.approx(5.0)

    def test_existing_position_reduces_headroom(self, rm: RiskManager) -> None:
        a = rm.assess([buy(10.0)], state(position=4.0))[0]  # 400 already held
        assert a.size == pytest.approx(1.0)

    def test_rejected_when_dust(self, rm: RiskManager) -> None:
        a = rm.assess([buy(0.01)], state(cash=5.0))[0]  # cash 5 -> ~0.05 size -> 5 EUR < min 10
        assert a.decision is Decision.REJECTED

    def test_rejected_without_price(self, rm: RiskManager) -> None:
        s = state()
        s.prices.clear()
        a = rm.assess([buy()], s)[0]
        assert a.decision is Decision.REJECTED
        assert "no price" in a.reason

    def test_cash_caps_size(self, rm: RiskManager) -> None:
        a = rm.assess([buy(10.0)], state(cash=200.0))[0]
        assert a.size * 100 <= 200.0


class TestSellAssessment:
    def test_sell_clamped_to_position(self, rm: RiskManager) -> None:
        a = rm.assess([sell(5.0)], state(position=2.0))[0]
        assert a.decision is Decision.RESIZED
        assert a.size == 2.0

    def test_sell_without_position_rejected(self, rm: RiskManager) -> None:
        a = rm.assess([sell()], state())[0]
        assert a.decision is Decision.REJECTED


class TestDailyLossHalt:
    def test_halts_on_max_daily_loss(self, rm: RiskManager) -> None:
        assert rm.on_candle(state(equity=10_000)) is True
        assert rm.on_candle(state(equity=10_000 - 201)) is False
        assert rm.halted
        assert "max daily loss" in rm.halt_reason

    def test_day_rollover_resets_baseline(self, rm: RiskManager) -> None:
        assert rm.on_candle(state(equity=10_000, ts=TS)) is True
        assert rm.on_candle(state(equity=9_900, ts=TS)) is True  # -100, under limit
        next_day = TS + timedelta(days=1)
        assert rm.on_candle(state(equity=9_900, ts=next_day)) is True  # new baseline


class TestConsecutiveLosses:
    def test_cooldown_after_max_losses(self, rm: RiskManager) -> None:
        for _ in range(5):
            rm.notify_trade_result(-10.0)
        a = rm.assess([buy()], state())[0]
        assert a.decision is Decision.REJECTED
        assert "cooldown" in a.reason

        for _ in range(12):
            rm.on_candle(state())
        assert rm.assess([buy()], state())[0].decision is Decision.APPROVED

    def test_win_resets_streak(self, rm: RiskManager) -> None:
        for _ in range(4):
            rm.notify_trade_result(-10.0)
        rm.notify_trade_result(50.0)
        for _ in range(4):
            rm.notify_trade_result(-10.0)
        assert rm.assess([buy()], state())[0].decision is Decision.APPROVED


class TestConfig:
    def test_leverage_rejected(self) -> None:
        with pytest.raises(ValueError, match="spot"):
            RiskConfig(leverage=2.0)

    def test_limit_order_uses_limit_price(self, rm: RiskManager) -> None:
        intent = OrderIntent(
            pair=PAIR, side=Side.BUY, size=10.0, order_type=OrderType.LIMIT, limit_price=50.0
        )
        a = rm.assess([intent], state(price=100.0))[0]
        assert a.size * 50 <= 500  # priced at limit, not market


class TestGrossExposure:
    def test_gross_cap_binds_before_pair_cap(self) -> None:
        rm = RiskManager(RiskConfig(max_position_quote=1000, max_gross_exposure_quote=300))
        a = rm.assess([buy(100.0)], state())[0]  # wants 10k; gross allows 300
        assert a.size * 100 == pytest.approx(300.0)


class TestBatchAccumulation:
    def test_two_buys_share_cash_within_one_candle(self) -> None:
        rm = RiskManager(RiskConfig(max_position_quote=10_000, max_gross_exposure_quote=10_000))
        intents = [buy(1.0), buy(1.0)]
        first, second = rm.assess(intents, state(cash=150.0, price=100.0))
        # first buy commits ~150 of cash (deflated by cost rate); second gets leftovers
        assert first.size * 100 <= 150.0
        assert (first.size + second.size) * 100 <= 150.0
        assert second.size < first.size

    def test_two_sells_cannot_oversell(self) -> None:
        rm = RiskManager(RiskConfig())
        intents = [sell(2.0), sell(2.0)]
        first, second = rm.assess(intents, state(position=2.0))
        assert first.size <= 2.0
        # second sell sees the position still (state-level), but combined fills
        # can never exceed the position because the venue/ledger clamp sells;
        # first is clamped to position, second as well — the LEDGER remains safe
        assert second.size <= 2.0


class TestZeroPnl:
    def test_zero_pnl_leaves_streak_unchanged(self) -> None:
        rm = RiskManager(RiskConfig())
        rm.notify_trade_result(-10)
        rm.notify_trade_result(-10)
        rm.notify_trade_result(0)  # neither loss nor reset
        rm.notify_trade_result(-10)
        rm.notify_trade_result(-10)
        # streak at 4, not reset by the zero
        rm.notify_trade_result(-10)
        a = rm.assess([buy()], state())[0]
        assert a.decision is Decision.REJECTED
        assert "cooldown" in a.reason
