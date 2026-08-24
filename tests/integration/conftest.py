"""Integration test fixtures: real Postgres via testcontainers, migrated with Alembic."""

import os
import subprocess
import sys
from collections.abc import AsyncIterator, Iterator
from pathlib import Path

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession
from testcontainers.community.postgres import PostgresContainer

from kaupo.config import get_settings
from kaupo.db.session import dispose_engine, session_scope

REPO_ROOT = Path(__file__).resolve().parents[2]

pytestmark = pytest.mark.integration


@pytest.fixture(scope="session")
def pg_url() -> Iterator[str]:
    with PostgresContainer("postgres:16-alpine") as pg:
        url = pg.get_connection_url()
        if "asyncpg" not in url:
            url = url.replace("postgresql+psycopg2://", "postgresql+asyncpg://")
            if url.startswith("postgresql://"):
                url = url.replace("postgresql://", "postgresql+asyncpg://", 1)
        yield url


@pytest.fixture(scope="session")
def migrated_url(pg_url: str) -> str:
    """Run the real Alembic migrations against the container, via subprocess
    so the settings cache in this process is irrelevant."""
    env = {**os.environ, "KAUPO_DATABASE_URL": pg_url}
    subprocess.run(
        [sys.executable, "-m", "alembic", "upgrade", "head"],
        check=True,
        env=env,
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
    )
    return pg_url


@pytest.fixture
async def session(migrated_url: str) -> AsyncIterator[AsyncSession]:
    previous_url = os.environ.get("KAUPO_DATABASE_URL")
    os.environ["KAUPO_DATABASE_URL"] = migrated_url
    get_settings.cache_clear()
    await dispose_engine()
    from kaupo.db.models import Base

    tables = [t.name for t in Base.metadata.sorted_tables if t.name != "alembic_version"]
    async with session_scope() as s:
        # isolate tests: wipe all tables (committed so the TRUNCATE locks
        # don't block the code under test writing from other connections)
        await s.execute(text(f"TRUNCATE {', '.join(tables)} CASCADE"))
        await s.commit()
        yield s
    await dispose_engine()
    if previous_url is None:
        os.environ.pop("KAUPO_DATABASE_URL", None)
    else:
        os.environ["KAUPO_DATABASE_URL"] = previous_url
    get_settings.cache_clear()
