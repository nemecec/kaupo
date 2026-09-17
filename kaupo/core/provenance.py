"""Fingerprint installed execution code and its main runtime dependencies."""

import hashlib
import platform
from functools import lru_cache
from importlib.metadata import version
from pathlib import Path

from kaupo.sdk.loader import _hash_behaviour


@lru_cache(maxsize=1)
def engine_version() -> str:
    root = Path(__file__).resolve().parents[1]
    digest = hashlib.sha256()
    # Settings values affecting execution already live in the frozen run config.
    # Exclude API/auth, reporting, and research-only backtest implementation.
    paths = [root / "domain.py"]
    for directory in ("core", "sdk", "venues", "risk", "ledger", "data"):
        paths.extend(sorted((root / directory).glob("*.py")))
    excluded = {
        "backtest_worker.py",
        "notify.py",
        "live_runner.py",
        "live_reconcile.py",
        "supervisor.py",
        "assignments.py",
        "backtest_jobs.py",
        "settings.py",
        "kraken_live.py",
        "kraken_client.py",
        "binance_archive.py",
    }
    for path in sorted(p for p in paths if p.name not in excluded):
        digest.update(str(path.relative_to(root)).encode())
        digest.update(_hash_behaviour(path.read_bytes()).encode())
    digest.update(platform.python_version().encode())
    for package in ("numpy", "ccxt", "sqlalchemy", "asyncpg", "pydantic"):
        digest.update(f"{package}={version(package)}".encode())
    return digest.hexdigest()
