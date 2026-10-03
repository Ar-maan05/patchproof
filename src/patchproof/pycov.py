"""Python coverage plumbing: wrap the test command in ``coverage run`` and read the results."""

from __future__ import annotations

import ast
import json
import os
import re
import shlex
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path

from .runner import RunResult, run_command

_SHELL_META = re.compile(r"[|&;<>()`$\n]")
_PYTHON = re.compile(r"^python[\d.]*$")


def wrap_command(cmd: str, data_file: Path, source: Path, rcfile: Path) -> str | None:
    """Rewrite a pytest/python command to run under coverage, or None if we cannot."""
    if _SHELL_META.search(cmd):
        return None
    try:
        toks = shlex.split(cmd)
    except ValueError:
        return None
    if not toks:
        return None
    py = sys.executable
    base = [
        py, "-m", "coverage", "run", f"--rcfile={rcfile}", f"--data-file={data_file}",
        f"--source={source}",
    ]  # fmt: skip
    first = os.path.basename(toks[0])
    rest = toks[1:]
    if first in ("pytest", "py.test"):
        return shlex.join([*base, "-m", "pytest", *rest])
    if _PYTHON.match(first):
        if rest and rest[0] == "-m" and len(rest) >= 2:
            return shlex.join([*base, "-m", *rest[1:]])
        if rest and not rest[0].startswith("-"):
            return shlex.join([*base, *rest])
    return None


@dataclass
class FileCoverage:
    executed: set[int] = field(default_factory=set)
    missing: set[int] = field(default_factory=set)

    @property
    def executable(self) -> set[int]:
        return self.executed | self.missing


@dataclass
class CoverageResult:
    run: RunResult | None = None
    files: dict[str, FileCoverage] = field(default_factory=dict)
    error: str | None = None

    @property
    def available(self) -> bool:
        return self.error is None


def run_with_coverage(cmd: str, workdir: Path, timeout: float, scratch: Path) -> CoverageResult:
    """Run ``cmd`` under coverage inside ``workdir``; data files live in ``scratch``."""
    scratch.mkdir(parents=True, exist_ok=True)
    data = scratch / ".coverage"
    rc = scratch / "coveragerc"
    rc.write_text("[run]\nbranch = False\n")
    wrapped = wrap_command(cmd, data, workdir, rc)
    if wrapped is None:
        return CoverageResult(error="test command is not a plain pytest/python invocation")
    res = run_command(wrapped, workdir, timeout)
    out = CoverageResult(run=res)
    if not data.exists():
        out.error = "coverage produced no data (is the `coverage` package importable?)"
        return out
    js = scratch / "cov.json"
    r = subprocess.run(
        [sys.executable, "-m", "coverage", "json", f"--rcfile={rc}", f"--data-file={data}",
         "-o", str(js)],
        cwd=workdir, capture_output=True, text=True,
    )  # fmt: skip
    if r.returncode != 0 or not js.exists():
        out.error = f"coverage json failed: {r.stderr.strip()[:200]}"
        return out
    for path, info in json.loads(js.read_text()).get("files", {}).items():
        rel = os.path.relpath(os.path.join(workdir, path), workdir)
        out.files[rel] = FileCoverage(set(info["executed_lines"]), set(info["missing_lines"]))
    return out


def statement_line_map(tree: ast.AST) -> dict[int, int]:
    """Map each source line to the first line of its statement (what coverage.py reports)."""
    mapping: dict[int, int] = {}
    stmts = [n for n in ast.walk(tree) if isinstance(n, ast.stmt)]
    for st in stmts:
        end = st.end_lineno or st.lineno
        # compound statements: only their header belongs to them, bodies are separate statements
        children = [
            c
            for field_name in ("body", "orelse", "finalbody", "handlers", "cases")
            for c in (getattr(st, field_name, None) or [])
            if hasattr(c, "lineno")
        ]
        if children:
            end = min(c.lineno for c in children) - 1
            end = max(end, st.lineno)
        for ln in range(st.lineno, end + 1):
            # innermost (latest-starting) statement wins
            if ln not in mapping or mapping[ln] <= st.lineno:
                mapping[ln] = st.lineno
    return mapping


def line_to_statement(source: str, lines: set[int]) -> set[int]:
    """Translate raw line numbers to coverage statement lines (identity if unparsable)."""
    try:
        m = statement_line_map(ast.parse(source))
    except SyntaxError:
        return set(lines)
    return {m[ln] for ln in lines if ln in m}


_FRAME_PY = re.compile(r'File "([^"]+)", line (\d+), in (\S+)')
_FRAME_PYTEST = re.compile(r"^([\w./\\-]+\.\w+):(\d+): ", re.M)


def traceback_frames(output: str, patched: dict[str, list[tuple[int, int]]]) -> list[dict]:
    """Frames in a failure output that land in patched files.

    ``patched`` maps old-side file path -> list of (first, last) changed old lines.
    """
    frames: list[dict] = []
    seen = set()

    def add(path: str, line: int, func: str | None):
        if path.startswith("<repo>/"):
            path = path[len("<repo>/") :]
        while path.startswith("./"):
            path = path[2:]
        if path not in patched or (path, line) in seen:
            return
        seen.add((path, line))
        in_region = any(lo - 2 <= line <= hi + 2 for lo, hi in patched[path])
        frames.append(
            {"file": path, "line": line, "function": func, "in_changed_region": in_region}
        )

    for m in _FRAME_PY.finditer(output):
        add(m.group(1), int(m.group(2)), m.group(3))
    for m in _FRAME_PYTEST.finditer(output):
        add(m.group(1), int(m.group(2)), None)
    return frames
