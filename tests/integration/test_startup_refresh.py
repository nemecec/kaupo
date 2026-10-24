"""Startup uses recent observations across closures and preserves stored history."""

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from kaupo.core.live_runner import _warm_up
from kaupo.data.candles import get_latest_candles, upsert_candles
from kaupo.db.session import get_sessionmaker
from kaupo.domain import Candle, Pair, Timeframe

pytestmark = pytest.mark.integration
PAIR = Pair.parse("BTC/EUR")
TF = Timeframe.H1


@pytest.mark.parametrize("available,fail", [(0, False), (4, False), (60, False), (720, False), (0, True)])
async def test_live_warmup_refreshes_once_and_keeps_older_history(session, available, fail):
    end = datetime.now(UTC).replace(minute=0, second=0, microsecond=0) - timedelta(hours=65)

    def candle(i):
        return Candle(
            pair=PAIR,
            timeframe=TF,
            ts=end - timedelta(hours=i),
            open=100,
            high=101,
            low=99,
            close=100,
            volume=1,
        )

    older = [candle(i) for i in reversed(range(721, 731))]
    await upsert_candles(session, older)
    await session.commit()
    recent = [candle(i) for i in reversed(range(available))]
    calls = []

    class Client:
        async def fetch_candles(self, pair, timeframe, since=None, limit=None):
            calls.append((pair, timeframe, since, limit))
            if fail:
                raise ConnectionError("unavailable")
            return recent

    # A warm-up larger than the OHLC page still uses older imported candles.
    requested = 800 if available == 720 else 50
    request = SimpleNamespace(pair=PAIR, timeframe=TF, warmup=requested, lookback=300)
    result = await _warm_up(request, get_sessionmaker(), Client())
    assert calls == [(PAIR, TF, None, None)]
    assert result == (older + recent)[-requested:]
    stored = await get_latest_candles(session, PAIR, TF, 1000)
    assert stored == older + recent
