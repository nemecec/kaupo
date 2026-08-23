"""Discover and load strategy plugins from a directory.

Each ``*.py`` file (not starting with ``_``) in the directory is imported;
every :class:`StrategyBase` subclass defined *in that file* is registered.
Duplicate strategy ids are an error.
"""

import hashlib
import importlib.util
import inspect
import sys
from pathlib import Path

from kaupo.sdk.protocol import LoadedStrategy, StrategyBase


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
        module_name = f"kaupo_plugin_{path.stem}_{_hash_file(path)[:8]}"
        spec = importlib.util.spec_from_file_location(module_name, path)
        if spec is None or spec.loader is None:
            raise ImportError(f"Cannot load strategy module {path}")
        module = importlib.util.module_from_spec(spec)
        sys.modules[module_name] = module
        spec.loader.exec_module(module)

        for _, obj in inspect.getmembers(module, inspect.isclass):
            if obj is StrategyBase or not issubclass(obj, StrategyBase):
                continue
            if inspect.getmodule(obj) is not module:
                continue  # only classes defined in this file
            strategy_id = getattr(obj, "id", None)
            if not strategy_id:
                raise ValueError(f"Strategy class {obj.__name__} in {path} has no 'id'")
            if strategy_id in loaded:
                raise ValueError(
                    f"Duplicate strategy id {strategy_id!r}: {path} and {loaded[strategy_id].path}"
                )
            loaded[strategy_id] = LoadedStrategy(
                id=strategy_id,
                cls=obj,
                source_hash=_hash_file(path),
                path=str(path),
            )
    return loaded
