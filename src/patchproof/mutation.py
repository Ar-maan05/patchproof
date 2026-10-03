"""Mutation operators restricted to changed lines.

Python gets AST-guided mutants (applied as exact byte-range edits, so the rest of the file is
untouched). C-like languages get a deliberately simple text-level mutator.
"""

from __future__ import annotations

import ast
import re
from dataclasses import dataclass

from .pycov import statement_line_map

C_LIKE_EXTS = {
    ".c", ".h", ".cc", ".cpp", ".cxx", ".hpp", ".hh", ".go", ".rs", ".java", ".js", ".ts",
    ".cs", ".kt", ".php", ".swift",
}  # fmt: skip


@dataclass
class Mutant:
    file: str
    line: int
    description: str
    operator: str
    source: str  # the full mutated file content

    def to_dict(self) -> dict:
        return {
            "file": self.file,
            "line": self.line,
            "operator": self.operator,
            "description": self.description,
        }


def _offsets(data: bytes) -> list[int]:
    offs, pos = [], 0
    for ln in data.splitlines(keepends=True):
        offs.append(pos)
        pos += len(ln)
    offs.append(pos)
    return offs


_CMP_SWAP = {
    ast.Lt: ("<", "<="),
    ast.LtE: ("<=", "<"),
    ast.Gt: (">", ">="),
    ast.GtE: (">=", ">"),
    ast.Eq: ("==", "!="),
    ast.NotEq: ("!=", "=="),
}
_SIMPLE_STMTS = (ast.Assign, ast.AugAssign, ast.AnnAssign, ast.Return, ast.Raise, ast.Assert,
                 ast.Delete, ast.Expr)  # fmt: skip


def python_mutants(
    source: str,
    filename: str,
    changed_lines: set[int],
    executed_stmt_lines: set[int] | None = None,
) -> list[Mutant]:
    """Mutants for ``source`` that touch ``changed_lines`` (new-side numbers).

    When ``executed_stmt_lines`` is given, nodes on statements the test never ran are skipped
    (they would trivially survive and say nothing about test strength).
    """
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return []
    data = source.encode("utf-8")
    offs = _offsets(data)
    smap = statement_line_map(tree)

    def pos(line: int, col: int) -> int:
        return offs[line - 1] + col

    def span(node: ast.AST) -> tuple[int, int]:
        return pos(node.lineno, node.col_offset), pos(node.end_lineno, node.end_col_offset)

    def relevant(node: ast.AST) -> bool:
        lo, hi = node.lineno, node.end_lineno or node.lineno
        if not any(lo <= ln <= hi for ln in changed_lines):
            return False
        if executed_stmt_lines is None:
            return True
        return smap.get(lo, lo) in executed_stmt_lines

    found: dict[tuple[int, int, str], Mutant] = {}

    def emit(start: int, end: int, repl: str, node: ast.AST, operator: str, desc: str) -> None:
        key = (start, end, repl)
        if key in found:
            return
        new = (data[:start] + repl.encode() + data[end:]).decode("utf-8")
        try:
            ast.parse(new)
        except SyntaxError:
            return
        found[key] = Mutant(filename, node.lineno, desc, operator, new)

    for node in ast.walk(tree):
        if isinstance(node, ast.Compare) and relevant(node):
            left = node.left
            for op, right in zip(node.ops, node.comparators, strict=True):
                swap = _CMP_SWAP.get(type(op))
                if swap:
                    a, b = swap
                    s0, s1 = span(left)[1], span(right)[0]
                    idx = data[s0:s1].find(a.encode())
                    if idx >= 0:
                        emit(s0 + idx, s0 + idx + len(a), b, node, "compare", f"`{a}` -> `{b}`")
                left = right
        elif isinstance(node, ast.BoolOp) and relevant(node):
            repl = "or" if isinstance(node.op, ast.And) else "and"
            orig = "and" if repl == "or" else "or"
            edits = []
            for v1, v2 in zip(node.values, node.values[1:], strict=False):
                s0, s1 = span(v1)[1], span(v2)[0]
                m = re.search(rb"\b%s\b" % orig.encode(), data[s0:s1])
                if m:
                    edits.append((s0 + m.start(), s0 + m.end()))
            if edits:
                new = data
                for s, e in reversed(edits):
                    new = new[:s] + repl.encode() + new[e:]
                key = (edits[0][0], edits[-1][1], repl)
                try:
                    ast.parse(new.decode())
                    found.setdefault(
                        key,
                        Mutant(filename, node.lineno, f"`{orig}` -> `{repl}`", "boolop",
                               new.decode()),
                    )  # fmt: skip
                except SyntaxError:
                    pass
        elif isinstance(node, (ast.If, ast.While, ast.IfExp)):
            test = node.test
            if relevant(test):
                s, e = span(test)
                txt = data[s:e].decode()
                emit(s, e, f"not ({txt})", test, "negate", f"negate condition `{txt[:40]}`")
        elif isinstance(node, ast.Constant) and relevant(node):
            s, e = span(node)
            if isinstance(node.value, bool):
                new = "False" if node.value else "True"
                emit(s, e, new, node, "bool", f"`{node.value}` -> `{new}`")
            elif type(node.value) is int:
                for delta in (1, -1):
                    nv = node.value + delta
                    txt = f"({nv})" if nv < 0 else str(nv)
                    emit(s, e, txt, node, "const", f"constant {node.value} -> {nv}")
        if isinstance(node, ast.Return) and node.value is not None and relevant(node):
            is_none = isinstance(node.value, ast.Constant) and node.value.value is None
            if not is_none:
                s, e = span(node.value)
                emit(s, e, "None", node, "return_none", "return None")
        if isinstance(node, _SIMPLE_STMTS) and relevant(node):
            doc = (
                isinstance(node, ast.Expr)
                and isinstance(node.value, ast.Constant)
                and (isinstance(node.value.value, str) or node.value.value is Ellipsis)
            )
            if not doc and not (isinstance(node, ast.AnnAssign) and node.value is None):
                s, e = span(node)
                emit(s, e, "pass", node, "delete", "delete statement")
    return sorted(found.values(), key=lambda m: (m.line, m.operator, m.description))


# ---------------------------------------------------------------------------------------------
# Simple text-level mutator for C-like languages
# ---------------------------------------------------------------------------------------------

_C_RULES: list[tuple[str, re.Pattern[str], str]] = [
    ("compare", re.compile(r"(?<![<>=!])=="), "!="),
    ("compare", re.compile(r"!=(?!=)"), "=="),
    ("compare", re.compile(r"(?<![<>=!-])<=(?!=)"), "<"),
    ("compare", re.compile(r"(?<![<>=!-])>=(?!=)"), ">"),
    ("compare", re.compile(r"(?<![<>=!-])<(?![<=])"), "<="),
    ("compare", re.compile(r"(?<![<>=!-])>(?![>=])"), ">="),
    ("boolop", re.compile(r"&&"), "||"),
    ("boolop", re.compile(r"\|\|"), "&&"),
    ("const", re.compile(r"(?<=[\w)\]])\s*\+\s*1\b(?![.\w])(?![=+])"), "-"),
    ("const", re.compile(r"(?<=[\w)\]])\s*-\s*1\b(?![.\w])(?![=-])"), "+"),
]


def _mask_strings(line: str) -> str:
    out, quote, esc = [], "", False
    for ch in line:
        if quote:
            out.append(" ")
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == quote:
                quote = ""
        elif ch in "\"'":
            quote = ch
            out.append(" ")
        else:
            out.append(ch)
    return "".join(out)


def c_like_mutants(source: str, filename: str, changed_lines: set[int]) -> list[Mutant]:
    lines = source.split("\n")
    found: list[Mutant] = []
    for ln in sorted(changed_lines):
        if not 1 <= ln <= len(lines):
            continue
        orig = lines[ln - 1]
        stripped = orig.strip()
        if not stripped or stripped.startswith(("//", "/*", "*", "#", "import ", "use ")):
            continue
        masked = _mask_strings(orig)
        cut = masked.find("//")
        if cut >= 0:
            masked = masked[:cut] + " " * (len(masked) - cut)
        for operator, rx, repl in _C_RULES:
            for m in rx.finditer(masked):
                if operator == "const":
                    text = m.group(0)
                    newtext = text.replace("+" if repl == "-" else "-", repl, 1)
                    desc = f"`{text.strip()}` -> `{newtext.strip()}`"
                else:
                    newtext, desc = repl, f"`{m.group(0)}` -> `{repl}`"
                new_line = orig[: m.start()] + newtext + orig[m.end() :]
                new_src = "\n".join([*lines[: ln - 1], new_line, *lines[ln:]])
                found.append(Mutant(filename, ln, desc, operator, new_src))
    return found


def sample(mutants: list[Mutant], limit: int) -> list[Mutant]:
    """Deterministic, evenly spaced sample of at most ``limit`` mutants."""
    if limit <= 0 or len(mutants) <= limit:
        return mutants
    step = len(mutants) / limit
    return [mutants[int(i * step)] for i in range(limit)]
