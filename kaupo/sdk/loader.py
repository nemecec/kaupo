"""Discover and load strategy plugins from a directory.

Each ``*.py`` file (not starting with ``_``) in the directory is imported;
every :class:`StrategyBase` or :class:`PortfolioStrategyBase` subclass
defined *in that file* is registered. Strategy ids are unique across both
kinds; duplicates are an error.

Loaded modules are cached by content hash: unchanged files are not
re-executed on repeated loads (the API calls this per request).
"""

import ast
import hashlib
import importlib.util
import inspect
import logging
import sys
from pathlib import Path

from kaupo.sdk.protocol import LoadedStrategy, PortfolioStrategyBase, StrategyBase

log = logging.getLogger(__name__)

# (path, content_hash) -> strategies defined in that file version
_cache: dict[tuple[str, str], list[LoadedStrategy]] = {}


def _hash_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _strip_docstrings(tree: ast.AST) -> ast.AST:
    """Drop every module, class and function docstring from the tree."""
    for node in ast.walk(tree):
        if not isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        body = node.body
        if not body:
            continue
        first = body[0]
        if (
            isinstance(first, ast.Expr)
            and isinstance(first.value, ast.Constant)
            and isinstance(first.value.value, str)
        ):
            node.body = body[1:] or [ast.Pass()]
    return tree


def _hash_behaviour(source: bytes) -> str:
    """sha256 of the parsed source without docstrings: the resume identity.

    Comments never reach the AST, and docstrings are stripped here, so an
    edit that cannot change a decision leaves this hash alone and the run
    chain resumes (kaupo#46). A file that does not parse falls back to the
    file hash, which keeps the old behaviour for a broken plugin.
    """
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return hashlib.sha256(source).hexdigest()
    dumped = ast.dump(_strip_docstrings(tree), annotate_fields=True, include_attributes=False)
    return hashlib.sha256(dumped.encode()).hexdigest()


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
            try:
                strategies = _load_file(path, content_hash)
            except Exception:
                log.exception("Skipping broken strategy file %s", path)
                continue
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

    behaviour_hash = _hash_behaviour(path.read_bytes())
    strategies = []
    for _, obj in inspect.getmembers(module, inspect.isclass):
        if obj in (StrategyBase, PortfolioStrategyBase):
            continue
        if not issubclass(obj, (StrategyBase, PortfolioStrategyBase)):
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
                behaviour_hash=behaviour_hash,
            )
        )
    return strategies
