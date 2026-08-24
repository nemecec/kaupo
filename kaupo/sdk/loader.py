"""Discover and load strategy plugins from a directory.

Each ``*.py`` file (not starting with ``_``) in the directory is imported;
every :class:`StrategyBase` subclass defined *in that file* is registered.
Duplicate strategy ids are an error.

Loaded modules are cached by content hash: unchanged files are not
re-executed on repeated loads (the API calls this per request).
"""

import hashlib
import importlib.util
import inspect
import sys
from pathlib import Path

from kaupo.sdk.protocol import LoadedStrategy, StrategyBase

# (path, content_hash) -> strategies defined in that file version
_cache: dict[tuple[str, str], list[LoadedStrategy]] = {}


def _hash_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def load_strategies(directory: Path) -> dict[str, LoadedStrategy]:
    directory = Path(directory)
    if not directory.is_dir():
        raise FileNotFoundError(f"Strategies directory not found: {directory}")

    loaded: dict[str, LoadedStrategy] = {}
    for path in sorted(directory.glob("*.py")):
        if path.name.startswith("_"):
            continue
        content_hash = _hash_file(path)
        cache_key = (str(path), content_hash)
        strategies = _cache.get(cache_key)
        if strategies is None:
            strategies = _load_file(path, content_hash)
            # keep only the latest version of each path: evict older hashes
            for key in [k for k in _cache if k[0] == str(path)]:
                del _cache[key]
            _cache[cache_key] = strategies
        for strat in strategies:
            if strat.id in loaded:
                raise ValueError(f"Duplicate strategy id {strat.id!r}: {path} and {loaded[strat.id].path}")
            loaded[strat.id] = strat
    return loaded


def _load_file(path: Path, content_hash: str) -> list[LoadedStrategy]:
    module_name = f"kaupo_plugin_{path.stem}_{content_hash[:8]}"
    spec = importlib.util.spec_from_file_location(module_name, path)
    if spec is None or spec.loader is None:
        raise ImportError(f"Cannot load strategy module {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    try:
        spec.loader.exec_module(module)
    finally:
        # keep sys.modules bounded; LoadedStrategy holds the class refs
        sys.modules.pop(module_name, None)

    strategies = []
    for _, obj in inspect.getmembers(module, inspect.isclass):
        if obj is StrategyBase or not issubclass(obj, StrategyBase):
            continue
        if obj.__module__ != module_name:
            continue  # only classes defined in this file
        strategy_id = getattr(obj, "id", None)
        if not strategy_id:
            raise ValueError(f"Strategy class {obj.__name__} in {path} has no 'id'")
        strategies.append(
            LoadedStrategy(
                id=strategy_id,
                cls=obj,
                source_hash=content_hash,
                path=str(path),
            )
        )
    return strategies
