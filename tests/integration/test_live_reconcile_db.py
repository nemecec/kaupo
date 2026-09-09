"""Crash reconciliation against Postgres: the four invariants of spec 6.2.

No double orders, no silently lost fills, ledger equal to the exchange
afterwards, and no secret in any log line.
"""

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from kaupo.core.live_reconcile import ReconciliationRefused, reconcile_live
from kaupo.db.models import FillRow, OrderRow, RunRow
from kaupo.db.session import get_sessionmaker
from kaupo.domain import Pair, Position, RunStatus, Side, new_id
from kaupo.venues.kraken_live import unattributed_order_id
from tests.fake_kraken import FakeKrakenClient

pytestmark = pytest.mark.integration

PAIR = Pair.parse("SOL/EUR")
BASE = datetime(2026, 3, 1, tzinfo=UTC)
# a resumed run: its chain began before any of these trades
CHAIN_START = BASE - timedelta(days=1)


async def _live_run(session: AsyncSession, run_id: str = "run-1") -> str:
    session.add(
        RunRow(
            id=run_id,
            mode="live",
            strategy_id="maker-trend",
            strategy_version="v1",
            started_at=BASE,
            status=RunStatus.RUNNING.value,
            config={"pair": str(PAIR), "timeframe": "4h", "starting_cash": 1000.0},
        )
    )
    await session.flush()
    return run_id


async def _record(
    session: AsyncSession,
    run_id: str,
    order_id: str,
    *,
    txid: str | None,
    size: float,
    price: float,
    ts: datetime,
    side: Side = Side.BUY,
) -> None:
    """An order and its fill, as a healthy live run would have written them."""
    session.add(
        OrderRow(
            id=order_id,
            run_id=run_id,
            ts=ts,
            pair=str(PAIR),
            side=side.value,
            type="limit",
            size=size,
            limit_price=price,
            status="filled",
            filled_price=price,
            filled_ts=ts,
            fee=0.0,
            reason="",
            exchange_order_id=txid,
        )
    )
    await session.flush()  # the fill's foreign key needs the order row first
    session.add(
        FillRow(
            id=new_id(),
            order_id=order_id,
            run_id=run_id,
            ts=ts,
            pair=str(PAIR),
            side=side.value,
            price=price,
            size=size,
            fee=0.0,
        )
    )
    await session.flush()


class TestOpenOrders:
    async def test_every_open_order_is_cancelled_and_none_adopted(self, session: AsyncSession) -> None:
        client = FakeKrakenClient(balances={"EUR": 1000.0, "SOL": 0.0})
        client.open_txids = ["LEFT-1", "LEFT-2"]

        result = await reconcile_live(
            client,
            get_sessionmaker(),
            pair=PAIR,
            positions={},
            cash=Decimal("1000"),
            history_since=CHAIN_START,
            check_quote=True,
        )

        assert set(result.cancelled_txids) == {"LEFT-1", "LEFT-2"}
        assert client.cancelled == ["LEFT-1", "LEFT-2"]
        assert client.open_txids == []
        assert client.placed == []  # reconciliation never places an order


class TestMissedTrades:
    async def test_a_trade_the_database_never_saw_is_recovered(self, session: AsyncSession) -> None:
        run_id = await _live_run(session)
        await _record(session, run_id, "order-1", txid="KRK-1", size=1.0, price=100.0, ts=BASE)
        await session.commit()

        client = FakeKrakenClient(balances={"EUR": 800.0, "SOL": 2.0})
        client.add_trade("KRK-1", price=100.0, size=1.0, fee=0.0, ts=BASE)
        client.add_trade("KRK-2", price=100.0, size=1.0, fee=0.0, ts=BASE + timedelta(minutes=1))

        result = await reconcile_live(
            client,
            get_sessionmaker(),
            pair=PAIR,
            positions={PAIR: Position(pair=PAIR, size=1.0, avg_entry=100.0)},
            cash=Decimal("900"),
            history_since=CHAIN_START,
            check_quote=False,
        )

        assert len(result.missed) == 1
        order, fill = result.missed[0]
        assert order.id == unattributed_order_id("KRK-2")
        assert order.exchange_order_id == "KRK-2"
        assert fill.size == 1.0
        assert fill.price == 100.0

    async def test_a_trade_of_a_known_order_lands_on_that_order(self, session: AsyncSession) -> None:
        """The run placed the order and died before recording its fill."""
        run_id = await _live_run(session)
        session.add(
            OrderRow(
                id="order-9",
                run_id=run_id,
                ts=BASE,
                pair=str(PAIR),
                side="buy",
                type="limit",
                size=1.0,
                limit_price=100.0,
                status="open",
                fee=0.0,
                reason="entry",
                exchange_order_id="KRK-9",
            )
        )
        await session.commit()

        client = FakeKrakenClient(balances={"EUR": 900.0, "SOL": 1.0})
        client.add_trade("KRK-9", price=100.0, size=1.0, fee=0.16, ts=BASE)

        result = await reconcile_live(
            client,
            get_sessionmaker(),
            pair=PAIR,
            positions={},
            cash=Decimal("1000"),
            history_since=CHAIN_START,
            check_quote=False,
        )

        assert len(result.missed) == 1
        order, fill = result.missed[0]
        assert order.id == "order-9"  # not a synthetic id: the order is known
        assert fill.size == 1.0
        assert fill.fee == 0.16

    async def test_only_the_unrecorded_part_of_a_partly_recorded_order_is_recovered(
        self, session: AsyncSession
    ) -> None:
        run_id = await _live_run(session)
        await _record(session, run_id, "order-3", txid="KRK-3", size=0.4, price=100.0, ts=BASE)
        await session.commit()

        client = FakeKrakenClient(balances={"EUR": 900.0, "SOL": 1.0})
        client.add_trade("KRK-3", price=100.0, size=0.4, fee=0.04, ts=BASE, trade_id="T-a")
        client.add_trade("KRK-3", price=100.0, size=0.6, fee=0.06, ts=BASE, trade_id="T-b")

        result = await reconcile_live(
            client,
            get_sessionmaker(),
            pair=PAIR,
            positions={PAIR: Position(pair=PAIR, size=0.4, avg_entry=100.0)},
            cash=Decimal("960"),
            history_since=CHAIN_START,
            check_quote=False,
        )

        assert len(result.missed) == 1
        assert result.missed[0][1].size == pytest.approx(0.6)
        assert result.missed[0][1].fee == pytest.approx(0.06)  # pro-rated by size

    async def test_recovery_is_idempotent_across_reruns(self, session: AsyncSession) -> None:
        run_id = await _live_run(session)
        await session.commit()
        client = FakeKrakenClient(balances={"EUR": 900.0, "SOL": 1.0})
        client.add_trade("KRK-7", price=100.0, size=1.0, fee=0.0, ts=BASE)
        sessionmaker = get_sessionmaker()

        first = await reconcile_live(
            client,
            sessionmaker,
            pair=PAIR,
            positions={},
            cash=Decimal("1000"),
            history_since=CHAIN_START,
            check_quote=False,
        )
        assert len(first.missed) == 1

        # the run recorded what reconciliation found, then crashed again
        order, fill = first.missed[0]
        session.add(
            OrderRow(
                id=order.id,
                run_id=run_id,
                ts=fill.ts,
                pair=str(PAIR),
                side=fill.side.value,
                type="market",
                size=fill.size,
                status="filled",
                filled_price=fill.price,
                filled_ts=fill.ts,
                fee=fill.fee,
                reason=order.reason,
                exchange_order_id=order.exchange_order_id,
            )
        )
        await session.flush()  # the fill's foreign key needs the order row first
        session.add(
            FillRow(
                id=new_id(),
                order_id=order.id,
                run_id=run_id,
                ts=fill.ts,
                pair=str(PAIR),
                side=fill.side.value,
                price=fill.price,
                size=fill.size,
                fee=fill.fee,
            )
        )
        await session.commit()

        second = await reconcile_live(
            client,
            sessionmaker,
            pair=PAIR,
            positions={PAIR: Position(pair=PAIR, size=1.0, avg_entry=100.0)},
            cash=Decimal("900"),
            history_since=CHAIN_START,
            check_quote=False,
        )
        assert second.missed == []  # recorded once, never twice

        fills = (await session.execute(select(FillRow))).scalars().all()
        assert len(fills) == 1


class TestFreshRun:
    """A live run with no chain adopts none of the account's own past."""

    async def test_account_history_is_not_recovered_into_a_fresh_run(self, session: AsyncSession) -> None:
        # the account traded this pair before the platform ever touched it,
        # and the trades net to a flat position, so the base check passes
        client = FakeKrakenClient(balances={"EUR": 1000.0, "SOL": 0.0})
        client.add_trade("OLD-1", price=100.0, size=1.0, fee=0.16, ts=BASE, side=Side.BUY)
        client.add_trade(
            "OLD-2", price=110.0, size=1.0, fee=0.18, ts=BASE + timedelta(hours=1), side=Side.SELL
        )

        result = await reconcile_live(
            client,
            get_sessionmaker(),
            pair=PAIR,
            positions={},
            cash=Decimal("1000"),
            history_since=None,
            check_quote=False,
        )

        assert result.missed == []  # no phantom fills in the new run's books
        assert result.seen_trade_ids == set()
        assert "fetch_my_trades" not in client.calls  # not even read

    async def test_the_trade_cursor_starts_at_the_run_start(self, session: AsyncSession) -> None:
        started = datetime(2026, 3, 2, 12, tzinfo=UTC)
        client = FakeKrakenClient(balances={"EUR": 1000.0, "SOL": 0.0})
        client.add_trade("OLD-1", price=100.0, size=1.0, fee=0.16, ts=BASE, side=Side.BUY)
        client.add_trade("OLD-2", price=100.0, size=1.0, fee=0.16, ts=BASE, side=Side.SELL)

        result = await reconcile_live(
            client,
            get_sessionmaker(),
            pair=PAIR,
            positions={},
            cash=Decimal("1000"),
            history_since=None,
            check_quote=False,
            now=started,
        )

        assert result.trades_cursor_ms == int(started.timestamp() * 1000)

    async def test_an_account_holding_the_base_asset_still_refuses(self, session: AsyncSession) -> None:
        """Skipping history does not skip the safety net."""
        client = FakeKrakenClient(balances={"EUR": 1000.0, "SOL": 3.0})

        with pytest.raises(ReconciliationRefused, match="SOL"):
            await reconcile_live(
                client,
                get_sessionmaker(),
                pair=PAIR,
                positions={},
                cash=Decimal("1000"),
                history_since=None,
                check_quote=False,
            )


class TestHistoryFloor:
    async def test_a_trade_older_than_the_chain_is_left_alone(self, session: AsyncSession) -> None:
        await _live_run(session)
        await session.commit()
        client = FakeKrakenClient(balances={"EUR": 1000.0, "SOL": 0.0})
        client.add_trade("ANCIENT", price=100.0, size=1.0, fee=0.16, ts=CHAIN_START - timedelta(days=5))

        result = await reconcile_live(
            client,
            get_sessionmaker(),
            pair=PAIR,
            positions={},
            cash=Decimal("1000"),
            history_since=CHAIN_START,
            check_quote=True,
        )

        assert result.missed == []


class TestBalanceDrift:
    async def test_dust_drift_is_accepted(self, session: AsyncSession) -> None:
        client = FakeKrakenClient(balances={"EUR": 1000.0000001, "SOL": 1.0000000001})

        result = await reconcile_live(
            client,
            get_sessionmaker(),
            pair=PAIR,
            positions={PAIR: Position(pair=PAIR, size=1.0, avg_entry=100.0)},
            cash=Decimal("1000"),
            history_since=CHAIN_START,
            check_quote=True,
        )

        assert result.balances["SOL"] == pytest.approx(1.0, abs=1e-6)

    async def test_base_drift_beyond_tolerance_refuses_the_start(self, session: AsyncSession) -> None:
        client = FakeKrakenClient(balances={"EUR": 1000.0, "SOL": 5.0})

        with pytest.raises(ReconciliationRefused, match="SOL"):
            await reconcile_live(
                client,
                get_sessionmaker(),
                pair=PAIR,
                positions={PAIR: Position(pair=PAIR, size=1.0, avg_entry=100.0)},
                cash=Decimal("1000"),
                history_since=CHAIN_START,
                check_quote=True,
            )

    async def test_quote_drift_refuses_a_resumed_run(self, session: AsyncSession) -> None:
        # baseline 1000 + no recorded cash movement: the books imply 1000 EUR,
        # the account holds 400 — money left the account outside the books
        client = FakeKrakenClient(balances={"EUR": 400.0, "SOL": 0.0})

        with pytest.raises(ReconciliationRefused, match="EUR"):
            await reconcile_live(
                client,
                get_sessionmaker(),
                pair=PAIR,
                positions={},
                cash=Decimal("1000"),
                history_since=CHAIN_START,
                check_quote=True,
                quote_baseline=1000.0,
                starting_cash=1000.0,
            )

    async def test_quote_check_stands_down_without_a_baseline(self, session: AsyncSession) -> None:
        """A chain that predates the baseline has nothing to compare against.

        The runner adopts the current balance as the baseline instead; the
        next resume checks against it.
        """
        client = FakeKrakenClient(balances={"EUR": 400.0, "SOL": 0.0})

        result = await reconcile_live(
            client,
            get_sessionmaker(),
            pair=PAIR,
            positions={},
            cash=Decimal("1000"),
            history_since=CHAIN_START,
            check_quote=True,
        )

        assert result.balances["EUR"] == 400.0

    async def test_relative_quote_check_passes_on_an_unmatched_account(self, session: AsyncSession) -> None:
        """The dust-pilot shape: born on an account that never held starting_cash."""
        client = FakeKrakenClient(balances={"EUR": 97.6, "SOL": 0.0})

        result = await reconcile_live(
            client,
            get_sessionmaker(),
            pair=PAIR,
            positions={},
            cash=Decimal("100"),
            history_since=CHAIN_START,
            check_quote=True,
            quote_baseline=97.6,
            starting_cash=100.0,
        )

        # expected = 97.6 + (100 - 100) = the account's exact balance
        assert result.balances["EUR"] == 97.6

    async def test_relative_quote_check_catches_money_leaving_the_account(
        self, session: AsyncSession
    ) -> None:
        client = FakeKrakenClient(balances={"EUR": 50.0, "SOL": 0.0})

        with pytest.raises(ReconciliationRefused, match="EUR"):
            await reconcile_live(
                client,
                get_sessionmaker(),
                pair=PAIR,
                positions={},
                cash=Decimal("100"),
                history_since=CHAIN_START,
                check_quote=True,
                quote_baseline=97.6,
                starting_cash=100.0,
            )

    async def test_quote_drift_is_ignored_on_a_fresh_run(self, session: AsyncSession) -> None:
        """A fresh ledger opens at a configured cash figure the account never matched."""
        client = FakeKrakenClient(balances={"EUR": 12.34, "SOL": 0.0})

        result = await reconcile_live(
            client,
            get_sessionmaker(),
            pair=PAIR,
            positions={},
            cash=Decimal("1000"),
            history_since=None,
            check_quote=False,
        )

        assert result.balances["EUR"] == 12.34

    async def test_recovered_fills_count_towards_the_balance_check(self, session: AsyncSession) -> None:
        """The books after recovery must match, not the books before it."""
        await _live_run(session)
        await session.commit()
        client = FakeKrakenClient(balances={"EUR": 900.0, "SOL": 1.0})
        client.add_trade("KRK-5", price=100.0, size=1.0, fee=0.0, ts=BASE)

        result = await reconcile_live(
            client,
            get_sessionmaker(),
            pair=PAIR,
            positions={},  # the books show no position yet
            cash=Decimal("1000"),
            history_since=CHAIN_START,
            check_quote=True,
            quote_baseline=1000.0,
            starting_cash=1000.0,
        )

        # baseline 1000 plus the recovered 100 EUR buy: the books imply 900,
        # matching the account's 900
        assert len(result.missed) == 1


class TestSecrets:
    async def test_no_credential_reaches_the_log(
        self, session: AsyncSession, caplog: pytest.LogCaptureFixture
    ) -> None:
        secret = "test-secret-placeholder"  # noqa: S105 — a placeholder, not a credential
        client = FakeKrakenClient(balances={"EUR": 999.99, "SOL": 0.0001})
        client.open_txids = ["LEFT-1"]
        client.add_trade("KRK-6", price=100.0, size=0.0001, fee=0.0, ts=BASE)

        with caplog.at_level("DEBUG"):
            await reconcile_live(
                client,
                get_sessionmaker(),
                pair=PAIR,
                positions={},
                cash=Decimal("1000"),
                history_since=CHAIN_START,
                check_quote=False,
            )

        assert caplog.text  # the run logged something
        assert secret not in caplog.text
        assert "api_key" not in caplog.text.lower()
