"""Path heuristics that decide whether a changed file is a test file."""

from __future__ import annotations

import fnmatch
import os
import posixpath
from pathlib import Path, PurePosixPath

TEST_DIRS = {"test", "tests", "__tests__", "testing", "testdata", "fixtures", "spec", "specs"}
TEST_NAME_GLOBS = (
    "test_*",
    "test-*",
    "*_test.*",
    "*-test.*",
    "*_tests.*",
    "*.test.*",
    "*.spec.*",
    "*_spec.*",
    "conftest.py",
    "tests.py",
    "test.py",
    "*Test.java",
    "*Tests.java",
    "*Test.php",
    "*Test.kt",
    "*Tests.swift",
)


def is_test_path(path: str) -> bool:
    path = path.replace("\\", "/")
    while path.startswith("./"):
        path = path[2:]
    parts = path.split("/")
    if any(p.lower() in TEST_DIRS for p in parts[:-1]):
        return True
    name = posixpath.basename(path)
    return any(fnmatch.fnmatchcase(name, g) for g in TEST_NAME_GLOBS)


def likely_test_names(stem: str, ext: str) -> list[str]:
    """File names a project is likely to give the tests of the module ``stem``."""
    stem_dash = stem.replace("_", "-")
    if ext == ".py":
        return [f"test_{stem}.py", f"{stem}_test.py", f"test_{stem}s.py", f"tests_{stem}.py"]
    if ext in (".c", ".h", ".cc", ".cpp", ".cxx"):
        suffixes = (".c", ".cc", ".cpp", ".cxx")
        names = [f"test-{stem_dash}", f"test_{stem}", f"{stem}_test", f"{stem}-test"]
        return [n + s for n in names for s in suffixes]
    return [f"test_{stem}{ext}", f"{stem}_test{ext}", f"{stem}.test{ext}", f"{stem}.spec{ext}"]


def find_existing_tests(repo: Path, path: str, max_files: int = 50_000) -> str | None:
    """Repo-relative path of the existing test file for the module at ``path``, or None.

    Heuristic: a file with a conventional name (``test_<stem>.py``, ``test-<stem>.c``, ...)
    anywhere in the tree, preferring the one that shares the most leading directories.
    """
    p = PurePosixPath(path)
    wanted = set(likely_test_names(p.stem, p.suffix.lower()))
    skip = {".git", "__pycache__", "node_modules", ".venv", "venv", "build", ".tox"}
    found: list[str] = []
    seen = 0
    for root, dirs, files in os.walk(repo):
        dirs[:] = [d for d in dirs if d not in skip]
        for f in files:
            seen += 1
            if f in wanted:
                found.append(PurePosixPath(Path(root, f).relative_to(repo)).as_posix())
        if seen > max_files:
            break
    if not found:
        return None

    def score(cand: str) -> tuple[int, int, str]:
        shared = 0
        for a, b in zip(PurePosixPath(cand).parts, p.parts, strict=False):
            if a != b:
                break
            shared += 1
        return (-shared, len(cand), cand)

    return min(found, key=score)
