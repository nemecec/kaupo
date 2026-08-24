"""Static determinism checks for strategy source files.

Strategies must be pure functions of their inputs: no wall-clock, no I/O,
no unseeded randomness. This linter walks the AST and reports violations
with line numbers. It complements (not replaces) the SDK design.

Known limits of AST linting (documented, accepted): value-level aliasing
(``f = open; f(...)``), PYTHONHASHSEED-dependent hash/set ordering, and
side effects inside imported-but-allowed third-party modules. The linter is
a tripwire for plausible mistakes, not a security boundary.
"""

import ast
from dataclasses import dataclass
from pathlib import Path

# module -> why forbidden
FORBIDDEN_IMPORTS = {
    "requests": "network I/O",
    "httpx": "network I/O",
    "aiohttp": "network I/O",
    "urllib": "network I/O",
    "socket": "network I/O",
    "ccxt": "strategies must not talk to exchanges directly",
    "subprocess": "process execution",
    "random": "unseeded randomness breaks determinism",
    "pathlib": "file I/O",
    "io": "file I/O",
    "shutil": "file I/O",
    "glob": "file I/O",
    "sqlite3": "file-backed state breaks determinism",
    "linecache": "file I/O",
    "fileinput": "file I/O",
    "pandas": "pandas gives wall-clock/file/network access; use kaupo.sdk.indicators",
    "threading": "concurrency breaks determinism",
    "multiprocessing": "concurrency breaks determinism",
    "concurrent": "concurrency breaks determinism",
    "asyncio": "concurrency breaks determinism",
    "importlib": "dynamic import bypasses linting",
    "builtins": "dynamic builtins access bypasses linting",
    "sys": "system access (exit, argv, ...) is forbidden in strategies",
    "os": "OS access (files, env, processes) is forbidden in strategies",
}

# (module, attribute) -> why forbidden
FORBIDDEN_CALLS = {
    ("datetime", "now"): "wall-clock; use ctx.clock.now()",
    ("datetime", "utcnow"): "wall-clock; use ctx.clock.now()",
    ("datetime", "today"): "wall-clock; use ctx.clock.now()",
    ("time", "time"): "wall-clock; use ctx.clock.now()",
    ("time", "time_ns"): "wall-clock",
    ("time", "monotonic"): "wall-clock",
    ("time", "monotonic_ns"): "wall-clock",
    ("time", "perf_counter"): "wall-clock",
    ("time", "perf_counter_ns"): "wall-clock",
    ("time", "process_time"): "process clock",
    ("time", "thread_time"): "process clock",
    ("time", "localtime"): "wall-clock",
    ("time", "gmtime"): "wall-clock",
    ("time", "ctime"): "wall-clock",
    ("time", "strftime"): "wall-clock",
    ("time", "sleep"): "blocking sleep",
}

FORBIDDEN_BUILTINS = {
    "open": "file I/O",
    "eval": "dynamic eval",
    "exec": "dynamic exec",
    "input": "stdin I/O",
    "getattr": "attribute indirection bypasses linting",
    "__import__": "dynamic import bypasses linting",
    "compile": "dynamic compile",
    "globals": "global state access",
    "locals": "dynamic scope access",
    "vars": "dynamic attribute access",
    "setattr": "dynamic attribute access",
    "delattr": "dynamic attribute access",
    "exit": "process termination",
    "quit": "process termination",
    "breakpoint": "debugger",
}

# os attributes forbidden as attribute access AND calls
FORBIDDEN_OS_ATTRS = {
    "system",
    "popen",
    "remove",
    "unlink",
    "rmdir",
    "environ",
    "environb",
    "getenv",
    "listdir",
    "makedirs",
    "walk",
    "_exit",
    "kill",
    "fork",
    "execv",
    "execl",
    "spawnl",
    "spawnv",
}

# np.random.* (unseeded randomness), numpy file I/O, wall-clock datetime64
FORBIDDEN_NUMPY_ATTRS = {
    "random",
    "load",
    "loadtxt",
    "genfromtxt",
    "fromregex",
    "fromfile",
    "savetxt",
    "save",
    "memmap",
    "datetime64",
}

# introspection dunders: traversal paths that bypass every name-based rule
# (normal dunders like __init__/__repr__ are fine and allowed)
FORBIDDEN_DUNDERS = {
    "__globals__",
    "__dict__",
    "__class__",
    "__subclasses__",
    "__bases__",
    "__mro__",
    "__builtins__",
    "__import__",
    "__code__",
    "__closure__",
    "__getattribute__",
    "__func__",
    "__self__",
    "__module__",
    "__init_subclass__",
    "__subclasshook__",
    "__getattr__",
    "__get__",
    "__set__",
    "__reduce__",
    "__reduce_ex__",
    "__getstate__",
    "__setstate__",
}


@dataclass(frozen=True)
class Violation:
    path: str
    line: int
    message: str

    def __str__(self) -> str:
        return f"{self.path}:{self.line}: {self.message}"


def _root_name(node: ast.AST) -> str | None:
    """Walk an attribute chain (a.b.c) down to the root Name."""
    while isinstance(node, ast.Attribute):
        node = node.value
    if isinstance(node, ast.Name):
        return node.id
    return None


class _Visitor(ast.NodeVisitor):
    def __init__(self, path: str) -> None:
        self.path = path
        self.violations: list[Violation] = []
        # aliases: local name -> module root (import x as y / from x import y)
        self._aliases: dict[str, str] = {}
        # from-imports: local name -> (module root, imported name)
        self._from_imports: dict[str, tuple[str, str]] = {}
        self._seen: set[tuple[int, str]] = set()

    def _add(self, node: ast.AST, message: str) -> None:
        lineno = getattr(node, "lineno", 0)
        key = (lineno, message)
        if key in self._seen:
            return
        self._seen.add(key)
        self.violations.append(Violation(self.path, lineno, message))

    def visit_Import(self, node: ast.Import) -> None:
        for alias in node.names:
            root = alias.name.split(".")[0]
            local = alias.asname or root
            self._aliases[local] = root
            if root in FORBIDDEN_IMPORTS:
                self._add(node, f"forbidden import {alias.name!r}: {FORBIDDEN_IMPORTS[root]}")
            if alias.name.split(".")[:2] == ["numpy", "random"]:
                self._add(node, f"forbidden import {alias.name!r}: unseeded randomness")

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
        root = (node.module or "").split(".")[0]
        if root in FORBIDDEN_IMPORTS:
            self._add(node, f"forbidden import {node.module!r}: {FORBIDDEN_IMPORTS[root]}")
        for alias in node.names:
            if alias.name == "*":
                self._add(node, "star imports are not allowed in strategies")
                continue
            local = alias.asname or alias.name
            self._aliases[local] = root
            self._from_imports[local] = (root, alias.name)
            if root == "os" and alias.name in FORBIDDEN_OS_ATTRS:
                self._add(node, f"forbidden from-import os.{alias.name}")
            if root == "numpy" and (alias.name in FORBIDDEN_NUMPY_ATTRS or node.module == "numpy.random"):
                self._add(node, f"forbidden from-import {node.module}.{alias.name}")

    def visit_Attribute(self, node: ast.Attribute) -> None:
        # dunder traversal (__globals__, __subclasses__, ...) bypasses every
        # name-based rule — reject introspection dunders (allow __init__ etc.)
        if node.attr in FORBIDDEN_DUNDERS:
            self._add(node, f"forbidden dunder attribute access {node.attr!r}")
        root_name = _root_name(node.value)
        if root_name is not None:
            root = self._aliases.get(root_name, root_name)
            if root == "os" and node.attr in FORBIDDEN_OS_ATTRS:
                self._add(node, f"forbidden os.{node.attr}")
            if root == "numpy" and node.attr in FORBIDDEN_NUMPY_ATTRS:
                self._add(node, f"forbidden numpy.{node.attr}")
        self.generic_visit(node)

    def visit_Call(self, node: ast.Call) -> None:
        func = node.func
        if isinstance(func, ast.Name):
            if func.id in FORBIDDEN_BUILTINS:
                self._add(node, f"forbidden builtin {func.id}(): {FORBIDDEN_BUILTINS[func.id]}")
            elif func.id in self._from_imports:
                root, original = self._from_imports[func.id]
                if (root, original) in FORBIDDEN_CALLS:
                    self._add(
                        node,
                        f"forbidden call {original}() (from {root}): {FORBIDDEN_CALLS[(root, original)]}",
                    )
        elif isinstance(func, ast.Attribute):
            root_name = _root_name(func.value)
            if root_name is not None:
                root = self._aliases.get(root_name, root_name)
                if (root, func.attr) in FORBIDDEN_CALLS:
                    self._add(
                        node,
                        f"forbidden call {root}.{func.attr}(): {FORBIDDEN_CALLS[(root, func.attr)]}",
                    )
                if root == "os" and func.attr in FORBIDDEN_OS_ATTRS:
                    self._add(node, f"forbidden os.{func.attr}")
        self.generic_visit(node)


def lint_source(source: str, path: str = "<string>") -> list[Violation]:
    try:
        tree = ast.parse(source, filename=path)
    except SyntaxError as exc:
        return [Violation(path, exc.lineno or 0, f"syntax error: {exc.msg}")]
    visitor = _Visitor(path)
    visitor.visit(tree)
    return visitor.violations


def lint_directory(directory: Path) -> list[Violation]:
    directory = Path(directory)
    if not directory.is_dir():
        raise FileNotFoundError(f"Strategies directory not found: {directory}")
    violations: list[Violation] = []
    for path in sorted(Path(directory).glob("*.py")):
        if path.name.startswith("_"):
            continue
        try:
            source = path.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError) as exc:
            violations.append(Violation(str(path), 0, f"unreadable file: {exc}"))
            continue
        violations.extend(lint_source(source, str(path)))
    return violations
