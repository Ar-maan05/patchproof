"""The reachability rule: a reproducer may only use the project's real entry points.

A reproducer that calls an internal helper with an argument no caller can produce proves
nothing about the project, so it is rejected before it is run. The check is static and
heuristic (see the README for its limits):

- Python: no import or use of a leading-underscore name from the module under review, no
  attribute access on an object to a private name the reviewed module defines, and no
  monkeypatching, mocking or assignment into the module under review.
- C: no call to a function declared ``static`` in the reviewed file, and no ``#include`` of
  that file.

Public functions can still be called with inputs real callers never pass. That is not detected.
"""

from __future__ import annotations

import ast
import contextlib
import re
from pathlib import PurePosixPath

PATCH_FUNCS = {"setattr", "delattr", "setitem", "delitem"}
PATCH_SUFFIXES = ("patch", "patch.object", "patch.dict", "patch.multiple")


def is_private(name: str) -> bool:
    return name.startswith("_") and not (name.startswith("__") and name.endswith("__"))


def module_names(path: str) -> set[str]:
    """Dotted names a Python file can be imported as (``src/pkg/mod.py`` -> pkg.mod, mod, ...)."""
    parts = list(PurePosixPath(path).with_suffix("").parts)
    if parts and parts[-1] == "__init__":
        parts = parts[:-1]
    return {".".join(parts[i:]) for i in range(len(parts))}


def _private_defs(tree: ast.AST) -> set[str]:
    out: set[str] = set()
    for n in ast.walk(tree):
        if isinstance(n, ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef):
            out.add(n.name)
        elif isinstance(n, ast.Name) and isinstance(n.ctx, ast.Store):
            out.add(n.id)
        elif isinstance(n, ast.Attribute) and isinstance(n.ctx, ast.Store):
            out.add(n.attr)
    return {x for x in out if is_private(x)}


class _PyChecker(ast.NodeVisitor):
    def __init__(self, mods: set[str], private_defs: set[str], own_defs: set[str]):
        self.mods = mods
        self.private_defs = private_defs - own_defs
        self.alias: dict[str, str] = {}
        self.out: list[str] = []

    # -- name resolution ---------------------------------------------------------------------
    def dotted(self, node: ast.AST) -> str | None:
        parts: list[str] = []
        while isinstance(node, ast.Attribute):
            parts.append(node.attr)
            node = node.value
        if not isinstance(node, ast.Name):
            return None
        root = self.alias.get(node.id, node.id)
        return ".".join([root, *reversed(parts)])

    def in_module(self, dotted: str | None) -> bool:
        return dotted is not None and any(
            dotted == m or dotted.startswith(m + ".") for m in self.mods
        )

    def is_module(self, dotted: str | None) -> bool:
        return dotted is not None and dotted in self.mods

    def add(self, node: ast.AST, msg: str) -> None:
        self.out.append(f"line {getattr(node, 'lineno', '?')}: {msg}")

    # -- imports -----------------------------------------------------------------------------
    def visit_Import(self, node: ast.Import) -> None:
        for a in node.names:
            if a.asname:
                self.alias[a.asname] = a.name
            else:
                self.alias[a.name.split(".")[0]] = a.name.split(".")[0]

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
        mod = node.module or ""
        for a in node.names:
            full = f"{mod}.{a.name}" if mod else a.name
            self.alias[a.asname or a.name] = full
            if self.is_module(mod) and is_private(a.name):
                self.add(node, f"imports the private name {a.name} from the module under review")

    # -- uses --------------------------------------------------------------------------------
    def visit_Attribute(self, node: ast.Attribute) -> None:
        base = self.dotted(node.value)
        if is_private(node.attr):
            if self.is_module(base):
                self.add(node, f"uses the private name {node.attr} of the module under review")
            elif node.attr in self.private_defs and not (
                isinstance(node.value, ast.Name) and node.value.id in ("self", "cls")
            ):
                self.add(node, f"uses {node.attr}, a private name the reviewed module defines")
        if isinstance(node.ctx, ast.Store) and self.in_module(base):
            self.add(node, "assigns into the module under review (monkeypatching)")
        self.generic_visit(node)

    def visit_Call(self, node: ast.Call) -> None:
        fn = self.dotted(node.func) or ""
        last = fn.rsplit(".", 1)[-1]
        if fn == "getattr" and len(node.args) >= 2:
            name = node.args[1]
            if (
                isinstance(name, ast.Constant)
                and isinstance(name.value, str)
                and is_private(name.value)
                and self.in_module(self.dotted(node.args[0]))
            ):
                self.add(node, f"getattr reaches the private name {name.value}")
        patching = last in PATCH_FUNCS or fn.endswith(PATCH_SUFFIXES) or fn == "patch"
        if patching and node.args:
            first = node.args[0]
            if isinstance(first, ast.Constant) and isinstance(first.value, str):
                target = first.value
                if any(target == m or target.startswith(m + ".") for m in self.mods):
                    self.add(node, f"patches {target!r} in the module under review")
            elif self.in_module(self.dotted(first)):
                self.add(node, "monkeypatches or mocks the module under review")
        self.generic_visit(node)


def check_python(reproducer: str, reviewed_path: str, reviewed_src: str | None) -> list[str]:
    try:
        tree = ast.parse(reproducer)
    except SyntaxError as e:
        return [f"the reproducer does not parse: {e.msg} (line {e.lineno})"]
    private_defs: set[str] = set()
    if reviewed_src:
        with contextlib.suppress(SyntaxError):
            private_defs = _private_defs(ast.parse(reviewed_src))
    own = _private_defs(tree)
    chk = _PyChecker(module_names(reviewed_path), private_defs, own)
    chk.visit(tree)
    return chk.out


# -- C ---------------------------------------------------------------------------------------
_C_NOISE = re.compile(r"""//[^\n]*|/\*.*?\*/|"(?:\\.|[^"\\\n])*"|'(?:\\.|[^'\\\n])*'""", re.S)
_STATIC_FN = re.compile(r"^[ \t]*static\b[^;{}()=]*?\b([A-Za-z_]\w*)[ \t\n]*\(", re.M)
_ANY_FN_DEF = re.compile(r"^[A-Za-z_][\w \t\*]*?\b([A-Za-z_]\w*)[ \t]*\([^;{}]*\)[ \t\n]*\{", re.M)
_KEYWORDS = {"if", "for", "while", "switch", "return", "sizeof", "defined", "__attribute__"}


def _blank(m: re.Match) -> str:
    return re.sub(r"[^\n]", " ", m.group(0))


def static_functions(c_src: str) -> set[str]:
    """Names of functions declared or defined ``static`` (a simple regex, no preprocessing)."""
    clean = _C_NOISE.sub(_blank, c_src)
    return {n for n in _STATIC_FN.findall(clean) if n not in _KEYWORDS}


def check_c(reproducer: str, reviewed_path: str, reviewed_src: str | None) -> list[str]:
    clean = _C_NOISE.sub(_blank, reproducer)
    out: list[str] = []
    base = PurePosixPath(reviewed_path).name
    for m in re.finditer(r'^[ \t]*#[ \t]*include[ \t]*"([^"]*)"', reproducer, re.M):
        if PurePosixPath(m.group(1)).name == base and base.endswith((".c", ".cc", ".cpp")):
            line = reproducer.count("\n", 0, m.start()) + 1
            out.append(f"line {line}: #includes {base}, which exposes its static functions")
    if not reviewed_src:
        return out
    own = set(_ANY_FN_DEF.findall(clean))
    for name in sorted(static_functions(reviewed_src) - own):
        for m in re.finditer(rf"\b{re.escape(name)}[ \t]*\(", clean):
            line = clean.count("\n", 0, m.start()) + 1
            out.append(f"line {line}: calls {name}(), which is static in {base}")
            break
    return out


def check_reproducer(
    language: str | None, reproducer: str, reviewed_path: str | None, reviewed_src: str | None
) -> list[str]:
    """Violations of the reachability rule (empty list = acceptable)."""
    if reviewed_path is None:
        return []
    if language == "python":
        return check_python(reproducer, reviewed_path, reviewed_src)
    if language == "c":
        return check_c(reproducer, reviewed_path, reviewed_src)
    return []
