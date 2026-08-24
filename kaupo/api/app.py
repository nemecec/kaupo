"""FastAPI application factory."""

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from kaupo.api.routes import backtests, data, runs, settings, status, strategies
from kaupo.api.ws import live_ws
from kaupo.config import get_settings
from kaupo.db.session import dispose_engine

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)-7s %(name)s: %(message)s")
log = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(_: FastAPI) -> AsyncIterator[None]:
    settings = get_settings()
    if settings.auth_disabled:
        log.warning("API auth DISABLED (no tokens configured) — local development only!")
    yield
    await dispose_engine()


app = FastAPI(title="Kaupo", version="0.1.0", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=get_settings().cors_origins,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(status.router)
app.include_router(runs.router)
app.include_router(backtests.router)
app.include_router(data.router)
app.include_router(strategies.router)
app.include_router(settings.router)

app.websocket("/ws/live")(live_ws)
