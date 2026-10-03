"""Is a suggested change equivalent to the code it replaces? (NO_BEHAVIOUR_CHANGE)

Python: identical ``ast.dump`` once docstrings are ignored (comments, whitespace, redundant
parentheses and formatting never reach the AST). Other languages: identical token stream after
stripping comments and whitespace. This is purely syntactic: it never claims two different
programs are equal, but it misses equivalences that need semantics (reordered independent
statements, ``x != 0`` versus ``0 != x``).
"""

from __future__ import annotations

import ast
import re
from pathlib import PurePosixPath

C_LIKE = {
    ".c", ".h", ".cc", ".cpp", ".cxx", ".hpp", ".hh", ".java", ".js", ".mjs", ".ts", ".tsx",
    ".go", ".rs", ".cs", ".swift", ".kt", ".scala", ".m", ".mm", ".php", ".dart",
}  # fmt: skip
HASH_COMMENT = {
    ".py", ".sh", ".bash", ".rb", ".pl", ".toml", ".yaml", ".yml", ".cfg", ".ini", ".mk",
    ".conf", ".r",
}  # fmt: skip


class _StripDocstrings(ast.NodeTransformer):
    def _strip(self, node):
        self.generic_visit(node)
        body = node.body
        if (
            body
            and isinstance(body[0], ast.Expr)
            and isinstance(body[0].value, ast.Constant)
            and isinstance(body[0].value.value, str)
        ):
            node.body = body[1:] or [ast.Pass()]
        return node

    visit_Module = visit_ClassDef = visit_FunctionDef = visit_AsyncFunctionDef = _strip


def python_fingerprint(src: str) -> str | None:
    """``ast.dump`` of ``src`` without docstrings, or None when it does not parse."""
    try:
        tree = ast.parse(src)
    except SyntaxError, ValueError:
        return None
    return ast.dump(_StripDocstrings().visit(tree))


_TOKEN = re.compile(
    r"""
    "(?:\\.|[^"\\\n])*" | '(?:\\.|[^'\\\n])*' | `[^`]*`      # strings / char literals
    | [A-Za-z_]\w* | \d[\w.]*                                  # identifiers, numbers
    | <<=|>>=|\.\.\.|->|\+\+|--|<<|>>|<=|>=|==|!=|&&|\|\||[+\-*/%&|^]=|::
    | \S
    """,
    re.X,
)
_C_COMMENT = re.compile(r"""//[^\n]*|/\*.*?\*/|("(?:\\.|[^"\\\n])*"|'(?:\\.|[^'\\\n])*')""", re.S)
_HASH_COMMENT = re.compile(r"""#[^\n]*|("(?:\\.|[^"\\\n])*"|'(?:\\.|[^'\\\n])*')""")


def token_stream(src: str, ext: str) -> list[str]:
    """Tokens of ``src`` with comments removed (comment syntax chosen by file extension)."""
    pattern = _C_COMMENT if ext in C_LIKE else _HASH_COMMENT if ext in HASH_COMMENT else None
    if pattern is not None:
        src = pattern.sub(lambda m: m.group(1) or " ", src)
    return _TOKEN.findall(src)


def method_for(path: str) -> str:
    return "ast" if PurePosixPath(path).suffix.lower() == ".py" else "tokens"


def equivalent(path: str, old: str | None, new: str | None) -> tuple[bool, str]:
    """(equal after normalisation, method used) for one file's before/after contents."""
    if old is None or new is None:
        return old == new, "existence"
    if old == new:
        return True, "identical"
    ext = PurePosixPath(path).suffix.lower()
    if ext == ".py":
        a, b = python_fingerprint(old), python_fingerprint(new)
        if a is not None and b is not None:
            return a == b, "ast"
    return token_stream(old, ext) == token_stream(new, ext), "tokens"
