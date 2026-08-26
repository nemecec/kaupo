"""Portfolio shadow request validation + the multi-base funding refresh loop."""

import asyncio
from datetime import UTC, datetime

import pytest

from kaupo.core import runner
from kaupo.core.runner import PortfolioShadowRequest
from kaupo.domain import Pair, Timeframe
from kaupo.sdk.protocol import LoadedStrategy, PortfolioStrategyBase

BTC = Pair.parse("BTC/EUR")
SOL = Pair.parse("SOL/EUR")
BASE = datetime(2026, 1, 1, tzinfo=UTC)


class _Dummy(PortfolioStrategyBase):
    id = "dummy"

    def on_candle(self, ctx):
        return []


STRATEGY = LoadedStrategy(id="dummy", cls=_Dummy, source_hash="x" * 64, path="/dev/null")


def request(pairs: list[Pair]) -> PortfolioShadowRequest:
    return PortfolioShadowRequest(strategy=STRATEGY, params={}, pairs=pairs, timeframe=Timeframe.H1)


class TestPortfolioShadowRequest:
    def test_valid_universe_is_sorted_canonically(self) -> None:
        assert request([SOL, BTC]).pairs == [BTC, SOL]

    def test_mixed_quotes_rejected(self) -> None:
        with pytest.raises(ValueError, match="one quote currency"):
            request([BTC, Pair.parse("SOL/USD")])

    def test_single_pair_rejected(self) -> None:
        with pytest.raises(ValueError, match="at least 2 pairs"):
            request([BTC])

    def test_duplicate_pairs_rejected(self) -> None:
        with pytest.raises(ValueError, match="Duplicate pairs"):
            request([BTC, SOL, BTC])


class FakeSession:
    async def commit(self) -> None:
        pass

    async def rollback(self) -> None:
        pass

    async def close(self) -> None:
        pass


class TestFundingRefreshLoop:
    async def test_one_loop_covers_all_bases_and_survives_failures(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        calls: list[str] = []
        upserts: list[int] = []

        class FakeFundingClient:
            async def fetch_funding_rates(self, base_asset: str, since=None):
                calls.append(base_asset)
                if base_asset == "BAD":
                    raise RuntimeError("boom")
                return [1, 2]

        async def fake_upsert(session, rates) -> int:
            upserts.append(len(rates))
            return len(rates)

        monkeypatch.setattr(runner, "upsert_funding_rates", fake_upsert)

        stop = asyncio.Event()
        task = asyncio.create_task(
            runner._funding_refresh_loop(
                FakeFundingClient(),  # type: ignore[arg-type]
                lambda: FakeSession(),  # type: ignore[arg-type]
                ["BTC", "BAD", "SOL"],
                0.02,
                stop,
            )
        )
        await asyncio.sleep(0.1)  # several refresh iterations
        stop.set()
        await asyncio.wait_for(task, timeout=2.0)

        assert calls[:3] == ["BTC", "BAD", "SOL"]  # one failure does not block the others
        assert len(upserts) >= 2  # BTC and SOL upserted each round
