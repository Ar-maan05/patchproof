"""Shared helpers for the patchproof eval scripts (case loading, patch parsing)."""
from __future__ import annotations

import fnmatch
import re
import shlex
import sys
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

EVAL_DIR = Path(__file__).resolve().parent
CASES_DIR = EVAL_DIR / "cases"
PATCHPROOF_ROOT = EVAL_DIR.parent

VERDICTS = ["PROVEN", "WEAK_TEST", "NO_OP", "PARTIAL", "CANNOT_REPRODUCE", "FLAKY", "ERROR"]


@dataclass
class Case:
    id: str
    path: Path
    test_cmd: str
    expected_verdict: str
    category: str
    source: str
    notes: str
    repro_mode: str = ""  # CANNOT_REPRODUCE only: base_import_error | head_fails
    extra: dict = field(default_factory=dict)

    @property
    def base(self) -> Path:
        return self.path / "base"

    @property
    def patch(self) -> Path:
        return self.path / "fix.patch"


def load_cases(only: str | None = None, cases_dir: Path = CASES_DIR) -> list[Case]:
    cases = []
    for d in sorted(p for p in cases_dir.iterdir() if p.is_dir()):
        if only and not fnmatch.fnmatch(d.name, only):
            continue
        meta = tomllib.loads((d / "case.toml").read_text())
        cases.append(
            Case(
                id=d.name,
                path=d,
                test_cmd=meta["test_cmd"],
                expected_verdict=meta["expected_verdict"],
                category=meta["category"],
                source=meta["source"],
                notes=meta.get("notes", ""),
                repro_mode=meta.get("repro_mode", ""),
                extra=meta,
            )
        )
    return cases


def is_test_path(path: str) -> bool:
    p = Path(path)
    return (
        p.name.startswith("test_")
        or p.name.endswith("_test.py")
        or p.name == "conftest.py"
        or "tests" in p.parts
        or "test" in p.parts
    )


def patch_files(patch_text: str) -> list[str]:
    """Paths (b/ side, prefix stripped) touched by a git-format patch."""
    return re.findall(r"^diff --git a/\S+ b/(\S+)$", patch_text, flags=re.M)


def with_python(cmd: str, python: str = sys.executable) -> str:
    """Replace a leading bare `python` in a test command with a concrete interpreter."""
    parts = shlex.split(cmd)
    if parts and parts[0] in ("python", "python3"):
        parts[0] = python
    return shlex.join(parts)
