"""Per-test outcomes (pytest junit XML) and discrimination analysis.

A test *discriminates* when it fails (or errors) on the base and passes on the head. Only those
tests are evidence for the fix, so only their assertions are worth grading.
"""

from __future__ import annotations

import os
import re
import shlex
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from pathlib import Path

from .assertions import UNKNOWN, WEAK, Strength, classify_source

_SHELL_META = re.compile(r"[|&;<>()`$\n]")
_PYTEST_TOK = ("pytest", "py.test")
_DROP_FLAGS = {"-x", "--exitfirst"}


def pytest_command_tokens(cmd: str) -> list[str] | None:
    """Tokens of a plain ``pytest ...`` / ``python -m pytest ...`` command, else None."""
    if _SHELL_META.search(cmd):
        return None
    try:
        toks = shlex.split(cmd)
    except ValueError:
        return None
    if not toks:
        return None
    if os.path.basename(toks[0]) in _PYTEST_TOK:
        return toks
    if "-m" in toks[:6]:
        i = toks.index("-m")
        if i + 1 < len(toks) and toks[i + 1] == "pytest":
            return toks
    return None


def pytest_prefix(cmd: str) -> list[str] | None:
    """The interpreter + ``-m pytest`` (or ``pytest``) part of a test command, no test args."""
    toks = pytest_command_tokens(cmd)
    if toks is None:
        return None
    if os.path.basename(toks[0]) in _PYTEST_TOK:
        return toks[:1]
    i = toks.index("-m")
    return toks[: i + 2]


def with_junit(cmd: str, junit_path: Path) -> str | None:
    """``cmd`` rewritten to write junit XML to ``junit_path`` and not stop at the first failure."""
    toks = pytest_command_tokens(cmd)
    if toks is None:
        return None
    out = []
    skip = False
    for t in toks:
        if skip:
            skip = False
            continue
        if t in _DROP_FLAGS or t.startswith("--maxfail="):
            continue
        if t == "--maxfail":
            skip = True
            continue
        if t.startswith("--junitxml") or t.startswith("--junit-xml"):
            if "=" not in t:
                skip = True
            continue
        out.append(t)
    out.append(f"--junitxml={junit_path}")
    return shlex.join(out)


@dataclass(frozen=True)
class TestOutcome:
    __test__ = False  # not a pytest class
    classname: str
    name: str
    status: str  # passed | failed | error | skipped
    err_type: str = ""
    message: str = ""
    detail: str = ""

    @property
    def key(self) -> str:
        return f"{self.classname}::{self.name}" if self.classname else self.name


def parse_junit(xml_text: str) -> dict[str, TestOutcome]:
    """Map ``classname::name`` to an outcome; empty dict if the XML is unusable."""
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError:
        return {}
    out: dict[str, TestOutcome] = {}
    for tc in root.iter("testcase"):
        status, err_type, message, detail = "passed", "", "", ""
        for child in tc:
            if child.tag in ("failure", "error"):
                status = "failed" if child.tag == "failure" else "error"
                err_type = child.get("type", "") or ""
                message = child.get("message", "") or ""
                detail = (child.text or "").strip()
                break
            if child.tag == "skipped":
                status = "skipped"
                break
        o = TestOutcome(tc.get("classname", "") or "", tc.get("name", "") or "", status,
                        err_type, message, detail)  # fmt: skip
        out[o.key] = o
    return out


def discriminating(base: dict[str, TestOutcome], head: dict[str, TestOutcome]) -> list[TestOutcome]:
    """Tests that fail/error on base and pass on head (head outcome returned)."""
    keys = [
        k for k, b in base.items()
        if b.status in ("failed", "error") and k in head and head[k].status == "passed"
    ]  # fmt: skip
    return [head[k] for k in sorted(keys)]


def locate_test(root: Path, outcome: TestOutcome) -> tuple[Path, str | None, str] | None:
    """(file, class_name, function_name) for a junit testcase, or None if not found."""
    func = outcome.name.split("[", 1)[0]
    parts = [p for p in outcome.classname.split(".") if p]
    for i in range(len(parts), 0, -1):
        f = root.joinpath(*parts[:i]).with_suffix(".py")
        if f.is_file():
            rest = parts[i:]
            return f, (rest[-1] if rest else None), func
    if parts:  # rootdir/importmode quirks: search by module basename
        for f in root.rglob(parts[-1] + ".py"):
            return f, None, func
        for i in range(len(parts) - 1, 0, -1):
            for f in root.rglob(parts[i - 1] + ".py"):
                return f, parts[-1], func
    return None


def grade_tests(root: Path, outcomes: list[TestOutcome]) -> dict[str, Strength]:
    """Assertion strength for each discriminating test, keyed by ``file::[Class::]test``."""
    graded: dict[str, Strength] = {}
    for o in outcomes:
        loc = locate_test(root, o)
        if loc is None:
            graded[o.key] = Strength(UNKNOWN, ["could not find the test's source file"])
            continue
        path, cls, func = loc
        try:
            src = path.read_text(encoding="utf-8")
        except OSError, UnicodeDecodeError:
            graded[o.key] = Strength(UNKNOWN, ["could not read the test's source file"])
            continue
        rel = path.relative_to(root).as_posix() if path.is_relative_to(root) else path.name
        label = f"{rel}::{cls + '::' if cls else ''}{func}"
        s = classify_source(src, func, cls)
        # parametrized cases of one function collapse to one entry (weakest wins)
        prev = graded.get(label)
        if prev is None or (s.cls == WEAK and prev.cls != WEAK):
            graded[label] = s
    return graded


def all_weak(graded: dict[str, Strength]) -> bool:
    """True when there is at least one graded test and every one is WEAK."""
    return bool(graded) and all(s.cls == WEAK for s in graded.values())
