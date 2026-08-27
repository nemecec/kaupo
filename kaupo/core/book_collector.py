"""Top-of-book collector: forward collection of best bid/ask per universe pair.

A dedicated process (``kaupo run book-collector``) because no public API
serves historical book data: rows accumulate only while this loop runs.
Each cycle polls the top of book of every universe pair (one shared client,
sequential fetches, ccxt rate limiting), upserts the snapshots, and prunes
rows older than the retention window, so the table stays bounded by
construction. A pair that fails is logged and skipped; the cycle continues.
The data is advisory (maker-fill fidelity, spread/depth features): the rest
of the stack starts and runs fine while the collector is down.
"""

import asyncio
import logging
from collections.abc import Sequence
from contextlib import suppress
from datetime import UTC, datetime, timedelta
from typing import Protocol

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from kaupo.config import Settings
from kaupo.data.book import prune_book_snapshots, upsert_book_snapshots
from kaupo.data.universe import KRAKEN_UNIVERSE
from kaupo.db.session import sm_scope
from kaupo.domain import BookSnapshot, Pair

log = logging.getLogger(__name__)


class BookTopClient(Protocol):
    """The slice of the venue client the collector needs (Kraken in production)."""

    exchange_id: str

    async def fetch_book_top(self, pair: Pair) -> BookSnapshot | None: ...


async def collect_cycle(
    client: BookTopClient,
    sessionmaker: async_sessionmaker[AsyncSession],
    pairs: Sequence[Pair],
    retention_days: int,
    *,
    now: datetime | None = None,
) -> tuple[int, int]:
    """One pass over the universe: fetch, upsert, then prune. Returns (stored, pruned)."""
    snapshots: list[BookSnapshot] = []
    for pair in pairs:
        try:
            snapshot = await client.fetch_book_top(pair)
        except Exception:
            log.warning("Book top fetch failed for %s; skipping the pair", pair, exc_info=True)
            continue
        if snapshot is None:  # the venue served no usable bid/ask
            continue
        snapshots.append(snapshot)
    if snapshots:
        async with sm_scope(sessionmaker) as session:
            await upsert_book_snapshots(session, snapshots)
    cutoff = (now or datetime.now(UTC)) - timedelta(days=retention_days)
    pruned = 0
    async with sm_scope(sessionmaker) as session:
        for pair in pairs:
            pruned += await prune_book_snapshots(session, client.exchange_id, str(pair), cutoff)
    return len(snapshots), pruned


async def run_book_collector(
    sessionmaker: async_sessionmaker[AsyncSession],
    settings: Settings,
    stop: asyncio.Event,
    *,
    client: BookTopClient,
    pairs: Sequence[Pair] = KRAKEN_UNIVERSE,
) -> None:
    """Collect top-of-book snapshots until ``stop`` is set; the current cycle finishes first."""
    log.info(
        "Book collector started: %d pairs every %.1fs, retention %d days",
        len(pairs),
        settings.book_poll_seconds,
        settings.book_retention_days,
    )
    while not stop.is_set():
        stored, pruned = await collect_cycle(client, sessionmaker, pairs, settings.book_retention_days)
        log.info("Book cycle done: %d snapshot(s) stored, %d row(s) pruned", stored, pruned)
        with suppress(TimeoutError):
            await asyncio.wait_for(stop.wait(), timeout=settings.book_poll_seconds)
    log.info("Book collector stopped")
