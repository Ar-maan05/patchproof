from __future__ import annotations

import difflib
import shlex
import sys
from pathlib import Path

import pytest

PYTEST_CMD = shlex.join([sys.executable, "-m", "pytest", "-x", "-q", "-p", "no:cacheprovider"])


def make_patch(old: dict[str, str], new: dict[str, str]) -> str:
    """Build a git-format diff turning ``old`` file contents into ``new``."""
    out = []
    for path in sorted(set(old) | set(new)):
        a, b = old.get(path), new.get(path)
        if a == b:
            continue
        out.append(f"diff --git a/{path} b/{path}\n")
        if a is None:
            out.append("new file mode 100644\n")
        if b is None:
            out.append("deleted file mode 100644\n")
        diff = list(
            difflib.unified_diff(
                (a or "").splitlines(keepends=True),
                (b or "").splitlines(keepends=True),
                fromfile="/dev/null" if a is None else f"a/{path}",
                tofile="/dev/null" if b is None else f"b/{path}",
            )
        )
        out.extend(diff)
    return "".join(out)


def write_tree(root: Path, files: dict[str, str]) -> Path:
    for rel, text in files.items():
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text)
    return root


@pytest.fixture
def scenario(tmp_path):
    """Create (repo, patch_text) from base files and the post-patch files."""

    def build(base: dict[str, str], new: dict[str, str]) -> tuple[Path, str]:
        repo = write_tree(tmp_path / "repo", base)
        full_new = {**base, **new}
        return repo, make_patch(base, full_new)

    return build
