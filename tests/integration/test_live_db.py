"""A live run end to end against Postgres, with a fake Kraken. No real orders.

Covers the wiring the unit tests cannot: the recorder, the ledger, the
control channel, and the shutdown path that has to reach the exchange.
"""

import asyncio
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from kaupo.config import Settings
from kaupo.core.live_reconcile import ReconciliationRefused
from kaupo.core.live_runner import LiveRequest, LiveTradingUnavailable, run_live
from kaupo.core.recorder import SUPERSEDED_HALT_REASON
from kaupo.data.candles import upsert_candles
from kaupo.db.models import EquitySnapshotRow, EventRow, FillRow, OrderRow, RunRow
from kaupo.db.session import get_sessionmaker
from kaupo.domain import Candle, Pair, Side, Timeframe, new_id, utc_now
from kaupo.risk.manager import RiskConfig
from kaupo.sdk.loader import load_strategies
from kaupo.venues.kraken_client import AsyncBridge
from tests.fake_kraken import FakeKrakenClient

pytestmark = pytest.mark.integration

PAIR = Pair.parse("SOL/EUR")
TF = Timeframe.H1

# Placeholder credentials. A real key never appears in this repository.
KEY = "test-key-placeholder"
SECRET = "test-secret-placeholder"  # noqa: S105 — a placeholder, not a credential

# Buys once on its first processed candle, then holds. Enough to exercise
# placement, the fill, the ledger, and the audit trail.
STRATEGY = """
from kaupo.domain import OrderIntent, OrderType, Side
from kaupo.sdk.protocol import StrategyBase


class BuyOnce(StrategyBase):
    id = "buy-once"

    def __init__(self, params=None):
        super().__init__(params)
        self._done = False

    def on_candle(self, ctx):
        if self._done:
            return []
        self._done = True
        return [
            OrderIntent(
                pair=ctx.candle.pair,
                side=Side.BUY,
                size=2.0,
                order_type=OrderType.LIMIT,
                limit_price=100.0,
                reason="test entry",
            )
        ]
"""

HOLD_STRATEGY = """
from kaupo.sdk.protocol import StrategyBase


class Hold(StrategyBase):
    id = "hold"

    def on_candle(self, ctx):
        return []
"""


def hourly(i: int, end: datetime, close: float = 100.0) -> Candle:
    return Candle(
        pair=PAIR,
        timeframe=TF,
        ts=end - timedelta(hours=i),
        open=close,
        high=close * 1.01,
        low=close * 0.99,
        close=close,
        volume=1.0,
    )


class ScriptedCandles:
    """Public market data: backfill pages, then scripted polls, then stop."""

    def __init__(self, history: list[Candle], batches: list[list[Candle]], stop: asyncio.Event) -> None:
        self.history = history
        self.batches = batches
        self.stop = stop
        self.in_backfill = True

    async def fetch_candles(self, pair, timeframe, since=None, limit=720):  # type: ignore[no-untyped-def]
        if self.in_backfill:
            if since is None:
                return []
            page = [c for c in self.history if c.ts >= since]
            if not page:
                self.in_backfill = False
            return page
        if self.batches:
            return self.batches.pop(0)
        self.stop.set()
        return []


def armed_settings(**overrides: object) -> Settings:
    base: dict[str, object] = {
        "live_trading_enabled": True,
        "kraken_api_key": KEY,
        "kraken_api_secret": SECRET,
        "live_max_notional": 500.0,
    }
    base.update(overrides)
    return Settings(**base)  # type: ignore[arg-type]


class FakeFundingClient:
    """Stands in for the Binance funding client; serves an empty series."""

    def __init__(self) -> None:
        self.requested: list[str] = []

    async def fetch_funding_rates(self, base_asset: str, since: datetime | None = None) -> list:
        self.requested.append(base_asset)
        return []


@pytest.fixture
def bridge() -> Iterator[AsyncBridge]:
    b = AsyncBridge(name="live-test")
    yield b
    b.close()


async def _seed_history(session: AsyncSession) -> tuple[datetime, list[Candle]]:
    now = datetime.now(UTC).replace(minute=0, second=0, microsecond=0)
    history = sorted((hourly(i, now - timedelta(hours=1)) for i in range(60)), key=lambda c: c.ts)
    await upsert_candles(session, history)
    await session.commit()
    return now, history


def _request(strategy_dir: Path, strategy_id: str = "buy-once") -> LiveRequest:
    return LiveRequest(
        strategy=load_strategies(strategy_dir)[strategy_id],
        params={},
        pair=PAIR,
        timeframe=TF,
        starting_cash=1000.0,
        warmup=50,
        poll_interval_seconds=0,
        risk=RiskConfig(max_position_quote=500.0),
    )


class TestDisarmed:
    async def test_a_disabled_host_makes_zero_exchange_calls(
        self, session: AsyncSession, tmp_path: Path, bridge: AsyncBridge
    ) -> None:
        (tmp_path / "buy_once.py").write_text(STRATEGY)
        now, history = await _seed_history(session)
        stop = asyncio.Event()
        candles = ScriptedCandles(history, [[hourly(0, now)]], stop)
        trading = FakeKrakenClient(balances={"EUR": 1000.0, "SOL": 0.0})

        with pytest.raises(LiveTradingUnavailable):
            await run_live(
                _request(tmp_path),
                get_sessionmaker(),
                candles,  # type: ignore[arg-type]
                stop=stop,
                trading=trading,
                bridge=bridge,
                settings=armed_settings(live_trading_enabled=False),
            )

        assert trading.calls == []  # nothing was even read from Kraken
        assert (await session.execute(select(RunRow))).scalars().all() == []

    async def test_missing_credentials_make_zero_exchange_calls(
        self, session: AsyncSession, tmp_path: Path, bridge: AsyncBridge
    ) -> None:
        (tmp_path / "buy_once.py").write_text(STRATEGY)
        now, history = await _seed_history(session)
        stop = asyncio.Event()
        trading = FakeKrakenClient()

        with pytest.raises(LiveTradingUnavailable, match="credentials"):
            await run_live(
                _request(tmp_path),
                get_sessionmaker(),
                ScriptedCandles(history, [[hourly(0, now)]], stop),  # type: ignore[arg-type]
                stop=stop,
                trading=trading,
                bridge=bridge,
                settings=armed_settings(kraken_api_secret=""),
            )

        assert trading.calls == []


class TestLiveRun:
    async def test_an_order_reaches_kraken_and_its_fill_reaches_the_ledger(
        self, session: AsyncSession, tmp_path: Path, bridge: AsyncBridge
    ) -> None:
        (tmp_path / "buy_once.py").write_text(STRATEGY)
        now, history = await _seed_history(session)
        stop = asyncio.Event()
        candles = ScriptedCandles(
            history,
            [[hourly(0, now)], [hourly(0, now + timedelta(hours=1))]],
            stop,
        )
        # the limit rests, then trades in full at its own price
        trading = FakeKrakenClient(balances={"EUR": 1000.0, "SOL": 0.0}, auto_fill=1.0)

        result = await run_live(
            _request(tmp_path),
            get_sessionmaker(),
            candles,  # type: ignore[arg-type]
            stop=stop,
            trading=trading,
            bridge=bridge,
            settings=armed_settings(),
        )

        assert result.status.value in ("completed", "halted")
        assert result.num_fills == 1

        placed = trading.placed
        assert len(placed) == 1
        assert placed[0].order_type == "limit"
        assert placed[0].post_only is True  # the maker fee the backtests assume
        assert placed[0].size == 2.0
        assert placed[0].side is Side.BUY

        runs = (await session.execute(select(RunRow))).scalars().all()
        assert [r.mode for r in runs] == ["live"]
        assert runs[0].config["live_max_notional"] == 500.0

        orders = (await session.execute(select(OrderRow))).scalars().all()
        assert len(orders) == 1
        assert orders[0].exchange_order_id == placed[0].txid  # the link to Kraken
        assert orders[0].status == "filled"

        fills = (await session.execute(select(FillRow))).scalars().all()
        assert len(fills) == 1
        assert fills[0].price == 100.0  # the exchange's price, not the candle's
        assert fills[0].size == 2.0
        assert fills[0].fee == pytest.approx(100.0 * 2.0 * 0.0016)
        assert fills[0].order_id == orders[0].id

        snapshots = (await session.execute(select(EquitySnapshotRow))).scalars().all()
        assert len(snapshots) == 2  # one per processed candle, flushed live

    async def test_a_partial_fill_is_recorded_at_the_filled_size(
        self, session: AsyncSession, tmp_path: Path, bridge: AsyncBridge
    ) -> None:
        (tmp_path / "buy_once.py").write_text(STRATEGY)
        now, history = await _seed_history(session)
        stop = asyncio.Event()
        candles = ScriptedCandles(
            history,
            [[hourly(0, now)], [hourly(0, now + timedelta(hours=1))]],
            stop,
        )
        trading = FakeKrakenClient(balances={"EUR": 1000.0, "SOL": 0.0}, auto_fill=0.4)

        result = await run_live(
            _request(tmp_path),
            get_sessionmaker(),
            candles,  # type: ignore[arg-type]
            stop=stop,
            trading=trading,
            bridge=bridge,
            settings=armed_settings(),
        )

        assert result.num_fills == 1
        fills = (await session.execute(select(FillRow))).scalars().all()
        assert fills[0].size == pytest.approx(0.8)  # 40 % of the 2.0 requested
        orders = (await session.execute(select(OrderRow))).scalars().all()
        assert orders[0].size == 2.0  # the order row keeps what was asked for
        assert orders[0].status == "filled"
        # the ledger accepted the smaller fill: cash fell by the traded amount
        assert float(result.final_equity) == pytest.approx(1000.0 - 0.8 * 100.0 * 0.0016, abs=0.01)

    async def test_the_limit_is_cancelled_on_kraken_at_the_candle_close(
        self, session: AsyncSession, tmp_path: Path, bridge: AsyncBridge
    ) -> None:
        (tmp_path / "buy_once.py").write_text(STRATEGY)
        now, history = await _seed_history(session)
        stop = asyncio.Event()
        candles = ScriptedCandles(
            history,
            [[hourly(0, now)], [hourly(0, now + timedelta(hours=1))]],
            stop,
        )
        trading = FakeKrakenClient(balances={"EUR": 1000.0, "SOL": 0.0})  # nothing trades

        result = await run_live(
            _request(tmp_path),
            get_sessionmaker(),
            candles,  # type: ignore[arg-type]
            stop=stop,
            trading=trading,
            bridge=bridge,
            settings=armed_settings(),
        )

        assert result.num_fills == 0
        assert trading.cancelled == [trading.placed[0].txid]
        orders = (await session.execute(select(OrderRow))).scalars().all()
        assert orders[0].status == "cancelled"  # a one-candle lifetime, enforced


class TestKillSwitch:
    async def test_a_kill_command_cancels_the_exchange_orders(
        self, session: AsyncSession, tmp_path: Path, bridge: AsyncBridge
    ) -> None:
        (tmp_path / "buy_once.py").write_text(STRATEGY)
        now, history = await _seed_history(session)
        # the kill command is issued before the run starts; DbControlProbe
        # ignores anything older than the run, so it is dated into the future
        session.add(
            EventRow(
                id=new_id(),
                ts=utc_now() + timedelta(seconds=5),
                level="info",
                source="control",
                message="control command 'kill' issued for run ALL",
                data={"command": "kill", "run_id": None},
            )
        )
        await session.commit()

        stop = asyncio.Event()
        candles = ScriptedCandles(
            history,
            [[hourly(0, now)], [hourly(0, now + timedelta(hours=1))]],
            stop,
        )
        trading = FakeKrakenClient(balances={"EUR": 1000.0, "SOL": 0.0})
        trading.open_txids = ["STRAY-1"]  # an order an earlier process left behind

        result = await run_live(
            _request(tmp_path, strategy_id="buy-once"),
            get_sessionmaker(),
            candles,  # type: ignore[arg-type]
            stop=stop,
            trading=trading,
            bridge=bridge,
            settings=armed_settings(),
        )

        assert result.status.value == "halted"
        assert "killed via control" in result.halt_reason
        # the shutdown path swept the exchange clean, tracked or not
        assert "STRAY-1" in trading.cancelled


class TestReconciliationOnStart:
    async def test_a_fresh_run_adopts_none_of_the_account_history(
        self, session: AsyncSession, tmp_path: Path, bridge: AsyncBridge
    ) -> None:
        """No chain means no history: the account's own past stays out."""
        (tmp_path / "hold.py").write_text(HOLD_STRATEGY)
        now, history = await _seed_history(session)
        stop = asyncio.Event()
        trading = FakeKrakenClient(balances={"EUR": 1000.0, "SOL": 0.0})
        # the account traded this pair before the platform did, netting flat
        trading.add_trade("OLD-1", price=100.0, size=1.0, fee=0.16, ts=utc_now() - timedelta(days=2))
        trading.add_trade(
            "OLD-2",
            price=110.0,
            size=1.0,
            fee=0.18,
            ts=utc_now() - timedelta(days=2) + timedelta(hours=1),
            side=Side.SELL,
        )

        result = await run_live(
            _request(tmp_path, strategy_id="hold"),
            get_sessionmaker(),
            ScriptedCandles(history, [[hourly(0, now)]], stop),  # type: ignore[arg-type]
            stop=stop,
            trading=trading,
            bridge=bridge,
            settings=armed_settings(),
        )

        assert result.num_fills == 0
        assert (await session.execute(select(FillRow))).scalars().all() == []
        assert float(result.final_equity) == 1000.0  # the configured cash, untouched

    async def test_a_fresh_run_writes_the_quote_baseline_into_its_config(
        self, session: AsyncSession, tmp_path: Path, bridge: AsyncBridge
    ) -> None:
        """The relative quote check's reference point, adopted at chain birth."""
        (tmp_path / "hold.py").write_text(HOLD_STRATEGY)
        now, history = await _seed_history(session)
        stop = asyncio.Event()
        trading = FakeKrakenClient(balances={"EUR": 97.6, "SOL": 0.0})

        await run_live(
            _request(tmp_path, strategy_id="hold"),
            get_sessionmaker(),
            ScriptedCandles(history, [[hourly(0, now)]], stop),  # type: ignore[arg-type]
            stop=stop,
            trading=trading,
            bridge=bridge,
            settings=armed_settings(),
        )

        runs = (await session.execute(select(RunRow))).scalars().all()
        assert len(runs) == 1
        assert runs[0].config["quote_baseline"] == 97.6

    async def test_a_resumed_run_recovers_the_trade_its_predecessor_lost(
        self, session: AsyncSession, tmp_path: Path, bridge: AsyncBridge
    ) -> None:
        (tmp_path / "hold.py").write_text(HOLD_STRATEGY)
        now, history = await _seed_history(session)
        request = _request(tmp_path, strategy_id="hold")
        chain_start = utc_now() - timedelta(hours=6)
        # a predecessor of the same config, superseded by a restart
        session.add(
            RunRow(
                id="predecessor",
                mode="live",
                strategy_id="hold",
                strategy_version=request.strategy.version,
                started_at=chain_start,
                ended_at=utc_now() - timedelta(hours=1),
                status="halted",
                metrics={"halt_reason": SUPERSEDED_HALT_REASON},
                config={
                    "pair": str(PAIR),
                    "timeframe": TF.value,
                    "params": {},
                    "starting_cash": 1000.0,
                },
            )
        )
        await session.commit()

        stop = asyncio.Event()
        trading = FakeKrakenClient(balances={"EUR": 799.68, "SOL": 2.0})
        # it bought, then died before the fill was ever recorded
        trading.add_trade("LOST-1", price=100.0, size=2.0, fee=0.32, ts=utc_now() - timedelta(minutes=30))

        result = await run_live(
            request,
            get_sessionmaker(),
            ScriptedCandles(history, [[hourly(0, now)]], stop),  # type: ignore[arg-type]
            stop=stop,
            trading=trading,
            bridge=bridge,
            settings=armed_settings(),
        )

        assert result.status.value in ("completed", "halted")
        fills = (await session.execute(select(FillRow))).scalars().all()
        assert len(fills) == 1
        assert fills[0].order_id == "kraken-LOST-1"
        assert fills[0].size == 2.0
        orders = (await session.execute(select(OrderRow))).scalars().all()
        assert orders[0].exchange_order_id == "LOST-1"
        # 1000 - 200 - 0.32 in cash, plus 2 SOL marked at 100
        assert float(result.final_equity) == pytest.approx(999.68, abs=0.01)
        # the predecessor predates the baseline: the new run adopts the
        # account's balance as the chain's quote reference point
        new_run = (await session.execute(select(RunRow).where(RunRow.id != "predecessor"))).scalars().one()
        assert new_run.config["quote_baseline"] == 799.68

    async def test_a_ledger_that_disagrees_with_the_account_refuses_to_start(
        self, session: AsyncSession, tmp_path: Path, bridge: AsyncBridge
    ) -> None:
        (tmp_path / "hold.py").write_text(HOLD_STRATEGY)
        now, history = await _seed_history(session)
        stop = asyncio.Event()
        # the account holds base the books know nothing about
        trading = FakeKrakenClient(balances={"EUR": 1000.0, "SOL": 5.0})

        with pytest.raises(ReconciliationRefused, match="SOL"):
            await run_live(
                _request(tmp_path, strategy_id="hold"),
                get_sessionmaker(),
                ScriptedCandles(history, [[hourly(0, now)]], stop),  # type: ignore[arg-type]
                stop=stop,
                trading=trading,
                bridge=bridge,
                settings=armed_settings(),
            )

        assert trading.placed == []
        assert (await session.execute(select(RunRow))).scalars().all() == []


class TestFundingParity:
    async def test_a_live_run_is_fed_the_same_funding_series_as_a_shadow_run(
        self, session: AsyncSession, tmp_path: Path, bridge: AsyncBridge
    ) -> None:
        """A funding filter must not go quiet when a strategy goes live."""
        (tmp_path / "hold.py").write_text(HOLD_STRATEGY)
        now, history = await _seed_history(session)
        stop = asyncio.Event()
        funding = FakeFundingClient()

        await run_live(
            _request(tmp_path, strategy_id="hold"),
            get_sessionmaker(),
            ScriptedCandles(history, [[hourly(0, now)]], stop),  # type: ignore[arg-type]
            stop=stop,
            trading=FakeKrakenClient(balances={"EUR": 1000.0, "SOL": 0.0}),
            bridge=bridge,
            settings=armed_settings(),
            funding_client=funding,  # type: ignore[arg-type]
        )

        assert funding.requested == ["SOL"]  # the refresh loop ran for the pair's base
