"""Resume eligibility and fill replay: pure logic, no DB."""

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from kaupo.core.recorder import SUPERSEDED_HALT_REASON
from kaupo.core.resume import config_hash, is_resumable, replay_fills
from kaupo.db.models import RunRow
from kaupo.domain import Fill, OrderId, Pair, Side

BTC = Pair.parse("BTC/EUR")
SOL = Pair.parse("SOL/EUR")
TS = datetime(2026, 8, 1, tzinfo=UTC)

CONFIG = {"pair": "BTC/EUR", "timeframe": "1h", "params": {"fast": 10}, "starting_cash": 10_000.0}
HASH = config_hash("sma", "BTC/EUR", "1h", {"fast": 10})


def run_row(**overrides: object) -> RunRow:
    fields: dict[str, object] = {
        "id": "run1",
        "mode": "shadow",
        "strategy_id": "sma",
        "strategy_version": "v1",
        "started_at": TS,
        "ended_at": TS + timedelta(hours=1),
        "status": "halted",
        "config": dict(CONFIG),
        "metrics": {"halt_reason": SUPERSEDED_HALT_REASON},
    }
    fields.update(overrides)
    return RunRow(**fields)  # type: ignore[arg-type]


def resumable(row: RunRow, new_hash: str = HASH, version: str = "v1") -> bool:
    return is_resumable(row, new_config_hash=new_hash, new_strategy_version=version)


class TestIsResumable:
    def test_superseded_same_config_is_resumable(self) -> None:
        assert resumable(run_row())

    def test_portfolio_universe_matches(self) -> None:
        config = {
            "pair": "BTC/EUR,SOL/EUR",
            "pairs": ["BTC/EUR", "SOL/EUR"],
            "timeframe": "1h",
            "params": {},
        }
        row = run_row(config=config)
        new_hash = config_hash("sma", "BTC/EUR,SOL/EUR", "1h", {}, ["BTC/EUR", "SOL/EUR"])
        assert resumable(row, new_hash)

    def test_running_row_is_not_resumable(self) -> None:
        assert not resumable(run_row(status="running", ended_at=None, metrics=None))

    def test_completed_row_is_not_resumable(self) -> None:
        assert not resumable(run_row(status="completed", metrics=None))

    def test_failed_row_is_not_resumable(self) -> None:
        assert not resumable(run_row(status="failed", metrics=None))

    def test_rail_halt_is_not_resumable(self) -> None:
        # engine halts (risk rail, kill switch, external stop) persist no metrics
        assert not resumable(run_row(metrics=None))

    def test_orphan_halt_is_not_resumable(self) -> None:
        assert not resumable(run_row(metrics={"halt_reason": "no matching assignment"}))

    def test_other_halt_reason_is_not_resumable(self) -> None:
        assert not resumable(run_row(metrics={"halt_reason": "stopped externally"}))

    def test_ended_at_required(self) -> None:
        assert not resumable(run_row(ended_at=None))

    def test_strategy_version_change_is_not_resumable(self) -> None:
        assert not resumable(run_row(), version="v2")

    def test_params_change_is_not_resumable(self) -> None:
        assert not resumable(run_row(), config_hash("sma", "BTC/EUR", "1h", {"fast": 11}))

    def test_pair_change_is_not_resumable(self) -> None:
        assert not resumable(run_row(), config_hash("sma", "SOL/EUR", "1h", {"fast": 10}))

    def test_timeframe_change_is_not_resumable(self) -> None:
        assert not resumable(run_row(), config_hash("sma", "BTC/EUR", "4h", {"fast": 10}))

    def test_strategy_change_is_not_resumable(self) -> None:
        assert not resumable(run_row(), config_hash("other", "BTC/EUR", "1h", {"fast": 10}))

    def test_universe_change_is_not_resumable(self) -> None:
        config = {
            "pair": "BTC/EUR,SOL/EUR",
            "pairs": ["BTC/EUR", "SOL/EUR"],
            "timeframe": "1h",
            "params": {},
        }
        row = run_row(config=config)
        new_hash = config_hash("sma", "ADA/EUR,BTC/EUR", "1h", {}, ["ADA/EUR", "BTC/EUR"])
        assert not resumable(row, new_hash)


def fill(side: Side, price: float, size: float, fee: float, pair: Pair = BTC, order: str = "o1") -> Fill:
    return Fill(order_id=OrderId(order), pair=pair, side=side, ts=TS, price=price, size=size, fee=fee)


class TestReplayFills:
    def test_no_fills_keeps_starting_state(self) -> None:
        ledger = replay_fills("EUR", 10_000.0, TS, [])
        assert ledger.cash == Decimal("10000")
        assert ledger.open_positions == {}

    def test_buys_fold_fees_into_cost_basis(self) -> None:
        ledger = replay_fills(
            "EUR",
            10_000.0,
            TS,
            [
                fill(Side.BUY, 100.0, 0.1, 0.26),
                fill(Side.BUY, 110.0, 0.1, 0.286),
            ],
        )
        assert ledger.cash == Decimal("9978.454")
        pos = ledger.open_positions[BTC]
        assert pos.size == 0.2
        assert pos.avg_entry == pytest.approx(107.73)

    def test_round_trip_gives_exact_cash_and_positions(self) -> None:
        ledger = replay_fills(
            "EUR",
            10_000.0,
            TS,
            [
                fill(Side.BUY, 100.0, 0.1, 0.26),
                fill(Side.BUY, 110.0, 0.1, 0.286),
                fill(Side.SELL, 120.0, 0.05, 0.156),
            ],
        )
        assert ledger.cash == Decimal("9984.298")
        pos = ledger.open_positions[BTC]
        assert pos.size == 0.15
        assert pos.avg_entry == pytest.approx(107.73)  # a sell keeps the basis
        # realized PnL covers both the entry fee (via the basis) and the exit fee
        assert ledger.realized_pnl == Decimal("0.4575")

    def test_closed_positions_do_not_carry(self) -> None:
        ledger = replay_fills(
            "EUR",
            1_000.0,
            TS,
            [
                fill(Side.BUY, 100.0, 1.0, 0.26),
                fill(Side.SELL, 100.0, 1.0, 0.26),
            ],
        )
        assert ledger.open_positions == {}

    def test_replay_spans_pairs_on_a_shared_quote(self) -> None:
        ledger = replay_fills(
            "EUR",
            1_000.0,
            TS,
            [
                fill(Side.BUY, 100.0, 1.0, 0.26, pair=BTC),
                fill(Side.BUY, 50.0, 2.0, 0.26, pair=SOL),
            ],
        )
        assert set(ledger.open_positions) == {BTC, SOL}
        assert ledger.cash == Decimal("799.48")
