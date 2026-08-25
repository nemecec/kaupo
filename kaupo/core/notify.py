"""Best-effort push alerts via ntfy. Alerts are nice-to-have: they never raise."""

import logging

import aiohttp
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from kaupo.config import get_settings
from kaupo.db.models import EventRow
from kaupo.domain import RunId, new_id, utc_now

log = logging.getLogger(__name__)

NTFY_URL = "https://ntfy.sh"


async def _post(topic: str, message: str) -> None:
    timeout = aiohttp.ClientTimeout(total=10)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        await session.post(f"{NTFY_URL}/{topic}", data=message.encode())


async def send_alert(message: str) -> None:
    """Push one alert to the configured ntfy topic. No-op without a topic; never raises."""
    topic = get_settings().notify_ntfy_topic
    if not topic:
        return
    try:
        await _post(topic, message)
    except Exception:
        log.warning("ntfy alert failed", exc_info=True)


async def record_halt(
    sessionmaker: async_sessionmaker[AsyncSession],
    run_id: RunId,
    strategy_id: str,
    halt_reason: str,
) -> None:
    """Write a halt event to the audit log and push an alert. Never raises."""
    try:
        async with sessionmaker() as session:
            session.add(
                EventRow(
                    id=new_id(),
                    ts=utc_now(),
                    level="warning",
                    source="engine",
                    message=f"run {run_id} halted: {halt_reason}",
                    data={
                        "run_id": str(run_id),
                        "strategy_id": strategy_id,
                        "halt_reason": halt_reason,
                    },
                )
            )
            await session.commit()
    except Exception:
        log.warning("halt event write failed", exc_info=True)
    await send_alert(f"Shadow run halted ({strategy_id}): {halt_reason}")
