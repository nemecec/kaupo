"""GET /api/v1/book input validation (direct handler calls, no DB)."""

from datetime import UTC, datetime, timedelta

import pytest
from fastapi import HTTPException

from kaupo.api.deps import Principal
from kaupo.api.routes.data import book

BASE = datetime(2026, 1, 1, tzinfo=UTC)


async def test_book_endpoint_rejects_a_bad_pair() -> None:
    with pytest.raises(HTTPException) as exc_info:
        await book(
            Principal(admin=False),
            None,  # type: ignore[arg-type]  # unused: pair parsing fails first
            pair="bogus",
            start=BASE,
            end=BASE + timedelta(hours=1),
            limit=100,
            exchange="kraken",
        )
    assert exc_info.value.status_code == 422
