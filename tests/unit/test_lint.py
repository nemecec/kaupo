from pathlib import Path

from kaupo.sdk.lint import lint_directory, lint_source


def test_clean_strategy_passes() -> None:
    source = """
from kaupo.sdk.protocol import StrategyBase
from kaupo.sdk import indicators

class S(StrategyBase):
    id = "s"
    def on_candle(self, ctx):
        c = indicators.closes(ctx.history(20))
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


def test_wall_clock_from_import_detected() -> None:
    assert len(lint_source("from time import time\nx = time()")) == 1
    assert len(lint_source("from time import sleep\nsleep(1)")) == 1
    assert len(lint_source("from datetime import datetime\nx = datetime.now()")) == 1


def test_wall_clock_chained_attr_detected() -> None:
    source = "import datetime\nx = datetime.datetime.now()"
    violations = lint_source(source)
    assert len(violations) == 1
    assert "datetime.now" in violations[0].message


def test_network_imports_detected() -> None:
    source = """
import requests
import httpx
import ccxt
"""
    violations = lint_source(source)
    assert len(violations) == 3


def test_file_io_imports_detected() -> None:
    for module in ("pathlib", "io", "shutil", "glob"):
        assert len(lint_source(f"import {module}")) == 1, module


def test_builtins_detected() -> None:
    source = """
def f():
    data = open("/etc/passwd").read()
    return eval("1+1")
"""
    violations = lint_source(source)
    assert len(violations) == 2


def test_indirection_detected() -> None:
    assert len(lint_source('getattr(os, "system")("id")')) >= 1
    assert len(lint_source('__import__("os")')) >= 1
    assert len(lint_source("globals()")) >= 1


def test_random_detected() -> None:
    assert len(lint_source("import random\nx = random.random()")) == 1


def test_numpy_random_detected() -> None:
    assert len(lint_source("import numpy as np\nx = np.random.random()")) >= 1


def test_os_environ_detected() -> None:
    assert len(lint_source('import os\nx = os.environ["HOME"]')) >= 1
    assert len(lint_source('import os\nx = os.getenv("HOME")')) >= 1
    assert len(lint_source('import os\nos.system("id")')) >= 1


def test_star_import_detected() -> None:
    assert len(lint_source("from time import *")) >= 1


def test_syntax_error_reported_not_raised() -> None:
    violations = lint_source("def broken(:\n", path="bad.py")
    assert len(violations) == 1
    assert "syntax error" in violations[0].message
    assert violations[0].path == "bad.py"


def test_lint_directory(tmp_path: Path) -> None:
    (tmp_path / "ok.py").write_text("X = 1\n")
    (tmp_path / "bad.py").write_text("import requests\n")
    (tmp_path / "_skip.py").write_text("import httpx\n")
    violations = lint_directory(tmp_path)
    assert len(violations) == 1
    assert "bad.py" in violations[0].path
    assert violations[0].line == 1


def test_lint_directory_unreadable_file(tmp_path: Path) -> None:
    (tmp_path / "binary.py").write_bytes(b"\xff\xfe\x00\x01 not utf8")
    violations = lint_directory(tmp_path)
    assert len(violations) == 1
    assert "unreadable" in violations[0].message


def test_os_from_imports_detected() -> None:
    assert len(lint_source("from os import environ\nx = environ")) >= 1
    assert len(lint_source("from os import getenv\nx = getenv('HOME')")) >= 1
    assert len(lint_source("from os import system as run\nrun('id')")) >= 1


def test_numpy_random_paths_detected() -> None:
    assert len(lint_source("from numpy import random\nx = random.rand()")) >= 1
    assert len(lint_source("from numpy.random import default_rng\nx = default_rng().random()")) >= 1
    assert len(lint_source("import numpy.random as npr\nx = npr.rand()")) >= 1


def test_numpy_file_io_detected() -> None:
    assert len(lint_source("import numpy as np\nx = np.load('/tmp/x.npy')")) >= 1


def test_pandas_detected() -> None:
    assert len(lint_source("import pandas as pd")) >= 1


def test_extended_time_functions() -> None:
    assert len(lint_source("import time\nx = time.time_ns()")) == 1
    assert len(lint_source("import time\nx = time.perf_counter()")) == 1
    assert len(lint_source("from time import time_ns\nx = time_ns()")) == 1


def test_process_termination_detected() -> None:
    assert len(lint_source("import sys\nsys.exit(1)")) >= 1
    assert len(lint_source("exit(1)")) >= 1
    assert len(lint_source("import os\nos._exit(0)")) >= 1


def test_concurrency_detected() -> None:
    for module in ("threading", "multiprocessing", "concurrent.futures", "asyncio"):
        assert len(lint_source(f"import {module}")) >= 1, module


def test_dynamic_import_detected() -> None:
    assert len(lint_source("import importlib")) >= 1
    assert len(lint_source("import builtins")) >= 1
    assert len(lint_source("import sys")) >= 1
    assert len(lint_source("locals()")) >= 1
    assert len(lint_source("setattr(x, 'y', 1)")) >= 1


def test_no_duplicate_violations() -> None:
    violations = lint_source("import time\ntime.sleep(1)")
    assert len(violations) == 1


def test_missing_directory_raises(tmp_path: Path) -> None:
    import pytest

    with pytest.raises(FileNotFoundError):
        lint_directory(tmp_path / "nope")


def test_os_import_banned_outright() -> None:
    assert len(lint_source("import os")) >= 1
    assert len(lint_source("import os.path")) >= 1
    assert len(lint_source("from os import path")) >= 1


def test_dunder_access_detected() -> None:
    assert len(lint_source("x = f.__globals__")) >= 1
    assert len(lint_source("x = obj.__dict__")) >= 1
    assert len(lint_source("x = obj.__class__.__subclasses__()")) >= 1


def test_dunder_definitions_allowed() -> None:
    source = """
class S:
    def __init__(self):
        self.x = 1
    def __repr__(self):
        return "S"
"""
    assert lint_source(source) == []


def test_time_and_numpy_gaps_closed() -> None:
    assert len(lint_source("import time\ntime.gmtime()")) == 1
    assert len(lint_source("import numpy as np\nnp.datetime64('now')")) >= 1
    assert len(lint_source("import numpy as np\nnp.loadtxt('/etc/passwd')")) >= 1
