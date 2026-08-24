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
    "pathlib": "file I/O",
    "io": "file I/O",
    "shutil": "file I/O",
    "glob": "file I/O",
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
    "getattr": "attribute indirection bypasses linting",
    "__import__": "dynamic import bypasses linting",
    "compile": "dynamic compile",
    "globals": "global state access",
    "vars": "dynamic attribute access",
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
    "getenv",
    "listdir",
    "makedirs",
    "walk",
}

FORBIDDEN_NUMPY_ATTRS = {"random"}  # np.random.* — unseeded randomness


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
            if alias.name == "*":
                self._add(node, "star imports are not allowed in strategies")
                continue
            local = alias.asname or alias.name
            self._aliases[local] = root
            self._from_imports[local] = (root, alias.name)

    def visit_Attribute(self, node: ast.Attribute) -> None:
        root_name = _root_name(node.value)
        if root_name is not None:
            root = self._aliases.get(root_name, root_name)
            if root == "os" and node.attr in FORBIDDEN_OS_ATTRS:
                self._add(node, f"forbidden os.{node.attr}")
            if root == "numpy" and node.attr in FORBIDDEN_NUMPY_ATTRS:
                self._add(node, f"forbidden numpy.{node.attr}: unseeded randomness")
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
                    self._add(node, f"forbidden os.{func.attr}()")
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
