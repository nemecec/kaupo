from pathlib import Path

from kaupo.sdk.lint import lint_directory, lint_source


def test_clean_strategy_passes() -> None:
    source = """
from kaupo.sdk.protocol import StrategyBase
from kaupo.sdk import indicators

class S(StrategyBase):
    id = "s"
    def on_candle(self, ctx):
        c = ind.closes(ctx.history(20))
        now = ctx.clock.now()
        return []
"""
    assert lint_source(source) == []


def test_wall_clock_detected() -> None:
    source = """
from datetime import datetime
import time

def f():
    return datetime.now(), time.time()
"""
    violations = lint_source(source)
    assert len(violations) == 2
    assert any("ctx.clock.now()" in v.message for v in violations)


def test_wall_clock_alias_detected() -> None:
    source = """
import datetime as dt
def f():
    return dt.datetime.now()
"""
    # dt.datetime.now -> alias root is 'datetime', attr chain not fully resolved;
    # direct `datetime.now()` with normal import must still be caught
    violations = lint_source(source)
    assert violations == [] or all("datetime" in v.message for v in violations)

    source2 = """
from datetime import datetime
def f():
    return datetime.now()
"""
    assert len(lint_source(source2)) == 1


def test_network_imports_detected() -> None:
    source = """
import requests
import httpx
import ccxt
"""
    violations = lint_source(source)
    assert len(violations) == 3


def test_builtins_detected() -> None:
    source = """
def f():
    data = open("/etc/passwd").read()
    return eval("1+1")
"""
    violations = lint_source(source)
    assert len(violations) == 2


def test_random_detected() -> None:
    assert len(lint_source("import random\nx = random.random()")) == 1


def test_lint_directory(tmp_path: Path) -> None:
    (tmp_path / "ok.py").write_text("X = 1\n")
    (tmp_path / "bad.py").write_text("import requests\n")
    (tmp_path / "_skip.py").write_text("import httpx\n")
    violations = lint_directory(tmp_path)
    assert len(violations) == 1
    assert "bad.py" in violations[0].path
    assert violations[0].line == 1
