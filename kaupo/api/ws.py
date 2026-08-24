"""Live dashboard updates over WebSocket.

Simple poll-based stream: every few seconds, sends active runs with their
latest equity. (Postgres LISTEN/NOTIFY push is a later optimization.)
"""

import asyncio
import json
import logging
from typing import Any

from fastapi import WebSocket, WebSocketDisconnect
from sqlalchemy import select

from kaupo.api.deps import check_token
from kaupo.config import get_settings
from kaupo.db.models import EquitySnapshotRow, RunRow
from kaupo.db.session import get_sessionmaker

log = logging.getLogger(__name__)

POLL_SECONDS = 5.0


async def _snapshot() -> dict[str, Any]:
    async with get_sessionmaker()() as session:
        runs = (
            (
                await session.execute(
                    select(RunRow).where(RunRow.status == "running").order_by(RunRow.started_at)
                )
            )
            .scalars()
            .all()
        )
        payload = []
        for run in runs:
            latest = (
                (
                    await session.execute(
                        select(EquitySnapshotRow)
                        .where(EquitySnapshotRow.run_id == run.id)
                        .order_by(EquitySnapshotRow.ts.desc())
                        .limit(1)
                    )
                )
                .scalars()
                .first()
            )
            payload.append(
                {
                    "run_id": run.id,
                    "mode": run.mode,
                    "strategy_id": run.strategy_id,
                    "started_at": run.started_at.isoformat(),
                    "latest_equity": (
                        {"ts": latest.ts.isoformat(), "equity": latest.equity} if latest else None
                    ),
                }
            )
        return {"type": "status", "runs": payload}


async def live_ws(websocket: WebSocket) -> None:
    """Clients pass the token as ?token=... (browsers can't set WS headers)."""
    settings = get_settings()
    if check_token(websocket.query_params.get("token", ""), settings) is None:
        await websocket.close(code=4401)
        return
    await websocket.accept()
    log.debug("WS client connected")
    try:
        while True:
            await websocket.send_text(json.dumps(await _snapshot()))
            await asyncio.sleep(POLL_SECONDS)
    except WebSocketDisconnect:
        log.debug("WS client disconnected")
