# ---- Build stage ----
FROM python:3.12-slim@sha256:78387bc3881b8273120a12ebe6c1ab22b018ccc2c9adf565ae1ac9b536e184ea AS builder

COPY --from=ghcr.io/astral-sh/uv:0.11.3 /uv /usr/local/bin/uv

WORKDIR /app

COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project

COPY kaupo ./kaupo
COPY examples ./examples
COPY README.md ./
RUN uv sync --frozen --no-dev

# ---- Runtime stage ----
FROM python:3.12-slim@sha256:78387bc3881b8273120a12ebe6c1ab22b018ccc2c9adf565ae1ac9b536e184ea

RUN useradd --create-home --uid 1000 kaupo

WORKDIR /app

COPY --from=builder /app/.venv /app/.venv
COPY --from=builder /app/kaupo ./kaupo
COPY --from=builder /app/examples ./examples
COPY alembic.ini ./

RUN chown -R kaupo:kaupo /app
USER kaupo

ENV PATH="/app/.venv/bin:$PATH" \
    PYTHONUNBUFFERED=1

EXPOSE 8000

# Default: run the API. Override with e.g. `kaupo run shadow ...` for a trading container.
CMD ["uvicorn", "kaupo.api.app:app", "--host", "0.0.0.0", "--port", "8000"]
