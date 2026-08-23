"""Static determinism checks for strategy source files.

Strategies must be pure functions of their inputs: no wall-clock, no I/O,
no unseeded randomness. This linter walks the AST and reports violations
with line numbers. It complements (not replaces) the SDK design.
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
}

# (module, attribute) -> why forbidden
FORBIDDEN_CALLS = {
    ("datetime", "now"): "wall-clock; use ctx.clock.now()",
    ("datetime", "utcnow"): "wall-clock; use ctx.clock.now()",
    ("datetime", "today"): "wall-clock; use ctx.clock.now()",
    ("time", "time"): "wall-clock; use ctx.clock.now()",
    ("time", "monotonic"): "wall-clock",
    ("time", "sleep"): "blocking sleep",
}

FORBIDDEN_BUILTINS = {
    "open": "file I/O",
    "eval": "dynamic eval",
    "exec": "dynamic exec",
    "input": "stdin I/O",
}
FORBIDDEN_OS_ATTRS = {"system", "popen", "remove", "unlink", "rmdir", "environ"}


@dataclass(frozen=True)
class Violation:
    path: str
    line: int
    message: str

    def __str__(self) -> str:
        return f"{self.path}:{self.line}: {self.message}"


class _Visitor(ast.NodeVisitor):
    def __init__(self, path: str) -> None:
        self.path = path
        self.violations: list[Violation] = []
        # aliases: local name -> fully qualified root (e.g. dt -> datetime)
        self._aliases: dict[str, str] = {}

    def _add(self, node: ast.AST, message: str) -> None:
        lineno = getattr(node, "lineno", 0)
        self.violations.append(Violation(self.path, lineno, message))

    def visit_Import(self, node: ast.Import) -> None:
        for alias in node.names:
            root = alias.name.split(".")[0]
            local = alias.asname or root
            self._aliases[local] = root
            if root in FORBIDDEN_IMPORTS:
                self._add(node, f"forbidden import {alias.name!r}: {FORBIDDEN_IMPORTS[root]}")

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
        root = (node.module or "").split(".")[0]
        if root in FORBIDDEN_IMPORTS:
            self._add(node, f"forbidden import {node.module!r}: {FORBIDDEN_IMPORTS[root]}")
        for alias in node.names:
            self._aliases[alias.asname or alias.name] = root

    def visit_Call(self, node: ast.Call) -> None:
        func = node.func
        if isinstance(func, ast.Name):
            if func.id in FORBIDDEN_BUILTINS:
                self._add(node, f"forbidden builtin {func.id}(): {FORBIDDEN_BUILTINS[func.id]}")
        elif isinstance(func, ast.Attribute) and isinstance(func.value, ast.Name):
            root = self._aliases.get(func.value.id, func.value.id)
            key = (root, func.attr)
            if key in FORBIDDEN_CALLS:
                self._add(node, f"forbidden call {root}.{func.attr}(): {FORBIDDEN_CALLS[key]}")
            if root == "os" and func.attr in FORBIDDEN_OS_ATTRS:
                self._add(node, f"forbidden os.{func.attr}()")
        self.generic_visit(node)


def lint_source(source: str, path: str = "<string>") -> list[Violation]:
    tree = ast.parse(source, filename=path)
    visitor = _Visitor(path)
    visitor.visit(tree)
    return visitor.violations


def lint_directory(directory: Path) -> list[Violation]:
    violations: list[Violation] = []
    for path in sorted(Path(directory).glob("*.py")):
        if path.name.startswith("_"):
            continue
        violations.extend(lint_source(path.read_text(), str(path)))
    return violations
