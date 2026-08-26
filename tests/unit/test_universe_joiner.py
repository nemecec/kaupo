"""Universe candle joiner: per-pair live streams joined into universe steps.

Hard cases: arrival order permutations, a missing pair (grace expiry emits a
partial step), duplicate candles, late candles after emission, and gap
refill overlap. Emitted steps must have strictly increasing timestamps and
sorted pair keys — the same shape as the backtest's ``joined_steps``.
"""

import asyncio
from datetime import UTC, datetime, timedelta
from itertools import permutations

import pytest

from kaupo.core.runner import UniverseCandleJoiner
from kaupo.domain import Candle, Pair, Timeframe

BTC = Pair.parse("BTC/EUR")
SOL = Pair.parse("SOL/EUR")
ADA = Pair.parse("ADA/EUR")
BASE = datetime(2026, 1, 1, tzinfo=UTC)


def candle(pair: Pair, i: int, price: float = 100.0) -> Candle:
    return Candle(
        pair=pair,
        timeframe=Timeframe.H1,
        ts=BASE + timedelta(hours=i),
        open=price,
        high=price + 1,
        low=price - 1,
        close=price,
        volume=1.0,
    )


class QueuePoller:
    """LiveCandlePoller double: the test feeds candles through a queue."""

    def __init__(self) -> None:
        self.queue: asyncio.Queue[Candle] = asyncio.Queue()

    async def feed(self, c: Candle) -> None:
        await self.queue.put(c)

    async def stream(self, stop: asyncio.Event | None = None):
        while stop is None or not stop.is_set():
            try:
                c = await asyncio.wait_for(self.queue.get(), timeout=0.02)
            except TimeoutError:
                continue
            yield c


async def collect(
    joiner: UniverseCandleJoiner, stop: asyncio.Event, seconds: float = 1.0
) -> list[tuple[datetime, dict[Pair, Candle]]]:
    """All steps the joiner emits until the timeout (the stream never ends on its own)."""
    steps: list[tuple[datetime, dict[Pair, Candle]]] = []

    async def run() -> None:
        async for ts, step in joiner.stream(stop):
            steps.append((ts, step))

    with pytest.raises(TimeoutError):
        await asyncio.wait_for(run(), timeout=seconds)
    return steps


def joiner_for(pairs: list[Pair], grace: float = 5.0) -> tuple[UniverseCandleJoiner, dict[Pair, QueuePoller]]:
    pollers = {pair: QueuePoller() for pair in pairs}
    return UniverseCandleJoiner(pollers, grace_seconds=grace), pollers  # type: ignore[arg-type]


class TestJoinerConstruction:
    def test_single_pair_rejected(self) -> None:
        with pytest.raises(ValueError, match="at least 2 pairs"):
            UniverseCandleJoiner({BTC: QueuePoller()})  # type: ignore[arg-type]


class TestArrivalOrder:
    @pytest.mark.parametrize(
        "order", list(permutations([BTC, SOL, ADA])), ids=lambda o: ",".join(map(str, o))
    )
    async def test_any_arrival_order_emits_the_full_step(self, order: tuple[Pair, ...]) -> None:
        joiner, pollers = joiner_for([BTC, SOL, ADA])
        stop = asyncio.Event()
        for pair in order:
            await pollers[pair].feed(candle(pair, 0, price={BTC: 1.0, SOL: 2.0, ADA: 3.0}[pair]))
        steps = await collect(joiner, stop, seconds=0.5)
        stop.set()

        assert len(steps) == 1
        ts, step = steps[0]
        assert ts == BASE
        assert list(step) == [ADA, BTC, SOL]  # sorted pair-string order
        assert step[SOL].close == 2.0

    async def test_multiple_timestamps_emit_in_order(self) -> None:
        joiner, pollers = joiner_for([BTC, SOL])
        stop = asyncio.Event()
        # interleaved feeding across timestamps
        await pollers[BTC].feed(candle(BTC, 0))
        await pollers[BTC].feed(candle(BTC, 1))
        await pollers[SOL].feed(candle(SOL, 1))
        await pollers[SOL].feed(candle(SOL, 0))
        steps = await collect(joiner, stop, seconds=0.5)
        stop.set()

        assert [ts for ts, _ in steps] == [BASE, BASE + timedelta(hours=1)]
        assert all(set(step) == {BTC, SOL} for _, step in steps)


class TestMissingPair:
    async def test_partial_step_after_grace_then_full_step(self) -> None:
        joiner, pollers = joiner_for([BTC, SOL, ADA], grace=0.05)
        stop = asyncio.Event()
        # ADA never delivers ts 0: the step goes out without it after the grace
        await pollers[BTC].feed(candle(BTC, 0))
        await pollers[SOL].feed(candle(SOL, 0))
        # everyone delivers ts 1
        for pair in (BTC, SOL, ADA):
            await pollers[pair].feed(candle(pair, 1))
        steps = await collect(joiner, stop, seconds=0.5)
        stop.set()

        assert [ts for ts, _ in steps] == [BASE, BASE + timedelta(hours=1)]
        assert set(steps[0][1]) == {BTC, SOL}  # ADA skipped the tick
        assert set(steps[1][1]) == {BTC, SOL, ADA}

    async def test_pair_missing_for_a_whole_gap_then_recovering(self) -> None:
        joiner, pollers = joiner_for([BTC, SOL], grace=0.02)
        stop = asyncio.Event()
        for i in range(3):
            await pollers[BTC].feed(candle(BTC, i))
        await pollers[SOL].feed(candle(SOL, 2))  # SOL missed ts 0 and 1 entirely
        steps = await collect(joiner, stop, seconds=0.5)
        stop.set()

        assert [ts for ts, _ in steps] == [BASE + timedelta(hours=i) for i in range(3)]
        assert set(steps[0][1]) == {BTC}
        assert set(steps[1][1]) == {BTC}
        assert set(steps[2][1]) == {BTC, SOL}


class TestDuplicateAndLateCandles:
    async def test_duplicate_before_completion_keeps_the_first(self) -> None:
        joiner, pollers = joiner_for([BTC, SOL])
        stop = asyncio.Event()
        await pollers[BTC].feed(candle(BTC, 0, price=100.0))
        await pollers[BTC].feed(candle(BTC, 0, price=999.0))  # duplicate: dropped
        await pollers[SOL].feed(candle(SOL, 0))
        steps = await collect(joiner, stop, seconds=0.5)
        stop.set()

        assert len(steps) == 1
        assert steps[0][1][BTC].close == 100.0

    async def test_late_candle_after_emission_is_dropped(self) -> None:
        joiner, pollers = joiner_for([BTC, SOL], grace=0.05)
        stop = asyncio.Event()
        steps: list[tuple[datetime, dict[Pair, Candle]]] = []

        async def run() -> None:
            async for ts, step in joiner.stream(stop):
                steps.append((ts, step))

        task = asyncio.create_task(run())  # the joiner runs while the test feeds
        try:
            await pollers[BTC].feed(candle(BTC, 0))  # SOL misses ts 0 -> partial step after grace
            await asyncio.sleep(0.2)
            assert [ts for ts, _ in steps] == [BASE]
            assert set(steps[0][1]) == {BTC}

            await pollers[SOL].feed(candle(SOL, 0))  # too late: its step is gone
            await pollers[BTC].feed(candle(BTC, 1))
            await pollers[SOL].feed(candle(SOL, 1))
            await asyncio.sleep(0.2)
            # strictly increasing timestamps; the late candle never appears
            assert [ts for ts, _ in steps] == [BASE, BASE + timedelta(hours=1)]
            assert set(steps[1][1]) == {BTC, SOL}
        finally:
            stop.set()
            await asyncio.wait_for(task, timeout=2.0)

    async def test_gap_refill_overlap_is_dropped(self) -> None:
        """A poller refilling a gap must not re-emit already-emitted timestamps."""
        joiner, pollers = joiner_for([BTC, SOL], grace=0.02)
        stop = asyncio.Event()
        for pair in (BTC, SOL):
            await pollers[pair].feed(candle(pair, 0))
            await pollers[pair].feed(candle(pair, 1))
        await asyncio.sleep(0.1)  # both steps emitted
        # SOL's poller refills a gap: candles for ts 0 and 1 arrive again, then ts 2
        await pollers[SOL].feed(candle(SOL, 0))
        await pollers[SOL].feed(candle(SOL, 1))
        await pollers[SOL].feed(candle(SOL, 2))
        await pollers[BTC].feed(candle(BTC, 2))
        steps = await collect(joiner, stop, seconds=0.5)
        stop.set()

        assert [ts for ts, _ in steps] == [BASE + timedelta(hours=i) for i in range(3)]
        assert all(set(step) == {BTC, SOL} for _, step in steps)


class TestStop:
    async def test_stop_ends_the_stream(self) -> None:
        joiner, _ = joiner_for([BTC, SOL])
        stop = asyncio.Event()
        steps: list[tuple[datetime, dict[Pair, Candle]]] = []

        async def run() -> None:
            async for ts, step in joiner.stream(stop):
                steps.append((ts, step))

        task = asyncio.create_task(run())
        await asyncio.sleep(0.05)
        stop.set()
        await asyncio.wait_for(task, timeout=2.0)
        assert steps == []
