"""Temporary working copies of the repo, with patches (or hunk subsets) applied."""

from __future__ import annotations

import itertools
import json
import os
import shutil
import subprocess
from collections.abc import Collection
from pathlib import Path

from .diff import FilePatch, Unit, render_files

DEFAULT_IGNORE = (
    ".git",
    "__pycache__",
    "*.pyc",
    ".pytest_cache",
    ".mypy_cache",
    ".ruff_cache",
    ".venv",
    "venv",
    "node_modules",
    ".tox",
)
IGNORE = shutil.ignore_patterns(*DEFAULT_IGNORE)


def make_ignore(extra: Collection[str] = ()):
    """copytree ``ignore=`` callable: the defaults plus user-supplied fnmatch patterns."""
    return shutil.ignore_patterns(*DEFAULT_IGNORE, *extra)


class ApplyError(Exception):
    """A patch (or hunk subset) did not apply."""


def apply_patch(workdir: Path, patch_text: str) -> str:
    """Apply ``patch_text`` in ``workdir``; returns the tool used. Raises ApplyError."""
    if not patch_text.strip():
        return "none"
    env = dict(os.environ)
    env["GIT_CEILING_DIRECTORIES"] = str(workdir.parent)
    errors = []
    try:
        r = subprocess.run(
            ["git", "apply", "--whitespace=nowarn", "-"],
            input=patch_text,
            cwd=workdir,
            env=env,
            capture_output=True,
            text=True,
        )
        if r.returncode == 0:
            return "git apply"
        errors.append(f"git apply: {r.stderr.strip()}")
    except FileNotFoundError:
        errors.append("git not installed")
    try:
        r = subprocess.run(
            ["patch", "-p1", "-s", "-f", "--no-backup-if-mismatch"],
            input=patch_text,
            cwd=workdir,
            capture_output=True,
            text=True,
        )
        if r.returncode == 0:
            return "patch -p1"
        errors.append(f"patch -p1: {(r.stdout + r.stderr).strip()}")
    except FileNotFoundError:
        errors.append("patch not installed")
    raise ApplyError("; ".join(errors))


class Workspace:
    """Holds base+test-changes, and hands out disposable copies with code hunks applied."""

    def __init__(
        self,
        repo: Path,
        root: Path,
        test_files: list[FilePatch],
        code_files: list[FilePatch],
        exclude: Collection[str] = (),
    ):
        self.root = root
        self.code_files = code_files
        self._counter = itertools.count()
        self._views: dict[bool, Path] = {}
        self.base = root / "pristine"
        shutil.copytree(repo, self.base, ignore=make_ignore(exclude), symlinks=True)
        try:
            apply_patch(self.base, render_files(test_files))
        except ApplyError as e:
            raise ApplyError(f"test-file changes do not apply to the base: {e}") from e

    def materialize(self, units: Collection[Unit] | None = None) -> Path:
        """Fresh copy of base+tests with the given code hunks (None = every hunk) applied."""
        d = self.root / f"w{next(self._counter)}"
        shutil.copytree(self.base, d, symlinks=True)
        try:
            apply_patch(d, render_files(self.code_files, units))
        except ApplyError:
            shutil.rmtree(d, ignore_errors=True)
            raise
        return d

    def checkout(self, units: Collection[Unit] | None = None) -> Path:
        """Cached long-lived copy: ``set()`` is the base, ``None`` is the full patch.

        Callers may run commands in it and must put back anything they change.
        """
        key = units is None
        if not key and units:
            return self.materialize(units)
        if key not in self._views:
            self._views[key] = self.materialize(None if key else set())
        return self._views[key]

    def track(self, rel: str) -> None:
        """Nothing to remember: every copy lives under ``root``, which the caller removes."""

    def close(self) -> None:
        """Nothing to undo: every copy lives under ``root``, which the caller removes."""

    @staticmethod
    def discard(path: Path) -> None:
        shutil.rmtree(path, ignore_errors=True)


MARKER = ".patchproof-persistent"


class PersistentWorkspace:
    """One long-lived working copy, reused for every run (``--persistent-workdir``).

    Layout under ``workdir``: ``tree/`` is the working copy (build directories created by the
    test command live in it and survive between runs), ``backup/`` holds the original bytes of
    every file this workspace may touch, and a marker file. Instead of copying the repo per
    run, ``materialize`` rewrites only the files the patch touches: it puts them back to the
    base content, then applies the requested hunks. Every other file keeps its mtime, so an
    incremental build (ninja, make) only recompiles what changed.

    Trade-offs: runs must be sequential, and every run shares the tree (and whatever the test
    command leaves in it). ``close`` restores the touched files, so the same workdir can be
    reused for another patch against the same base.
    """

    def __init__(
        self,
        repo: Path,
        workdir: Path,
        test_files: list[FilePatch],
        code_files: list[FilePatch],
        exclude: Collection[str] = (),
    ):
        self.code_files = code_files
        self.workdir = workdir
        self.tree = workdir / "tree"
        self._backup = workdir / "backup"
        self._manifest_path = workdir / "manifest.json"
        self._manifest: dict[str, bool] = {}  # path -> existed originally
        self._state: object = "unset"
        self._code_paths = _paths(code_files)
        self._base_bytes: dict[str, bytes | None] = {}
        marker = workdir / MARKER
        if workdir.exists() and any(workdir.iterdir()):
            if not marker.exists():
                raise ApplyError(
                    f"--persistent-workdir {workdir} is not empty and was not created by patchproof"
                )
            self._recover()
        else:
            workdir.mkdir(parents=True, exist_ok=True)
        if not self.tree.exists():
            shutil.copytree(repo, self.tree, ignore=make_ignore(exclude), symlinks=True)
            marker.write_text(f"seeded from {repo}\n")
        for p in _paths(test_files) + self._code_paths:
            self._save(p)
        try:
            apply_patch(self.tree, render_files(test_files))
        except ApplyError as e:
            self.close()
            raise ApplyError(f"test-file changes do not apply to the base: {e}") from e
        for p in self._code_paths:
            self._base_bytes[p] = self._read(p)

    # -- bookkeeping ------------------------------------------------------------------------
    def _read(self, rel: str) -> bytes | None:
        try:
            return (self.tree / rel).read_bytes()
        except FileNotFoundError:
            return None

    def _write(self, rel: str, data: bytes | None) -> None:
        target = self.tree / rel
        if data is None:
            target.unlink(missing_ok=True)
            return
        if target.exists() and target.read_bytes() == data:
            return  # identical: leave the mtime alone so the build stays incremental
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)

    def _save(self, rel: str) -> None:
        if rel in self._manifest:
            return
        data = self._read(rel)
        if data is not None:
            dest = self._backup / rel
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_bytes(data)
        self._manifest[rel] = data is not None
        self._manifest_path.write_text(json.dumps(self._manifest))

    def _recover(self) -> None:
        """A previous run died before ``close``: put its files back first."""
        if not self._manifest_path.exists() or not self.tree.exists():
            return
        self._manifest = json.loads(self._manifest_path.read_text())
        self._restore_originals()

    def _restore_originals(self) -> None:
        for rel, existed in self._manifest.items():
            orig = (self._backup / rel).read_bytes() if existed else None
            self._write(rel, orig)
        shutil.rmtree(self._backup, ignore_errors=True)
        self._manifest_path.unlink(missing_ok=True)
        self._manifest = {}

    # -- Workspace interface ------------------------------------------------------------------
    def materialize(self, units: Collection[Unit] | None = None) -> Path:
        """Put the shared tree in the base+tests state with ``units`` applied (None = all)."""
        key = None if units is None else frozenset(units)
        if self._state == key:
            return self.tree
        for p in self._code_paths:
            self._write(p, self._base_bytes[p])
        self._state = "unset"
        apply_patch(self.tree, render_files(self.code_files, units))
        self._state = key
        return self.tree

    def checkout(self, units: Collection[Unit] | None = None) -> Path:
        return self.materialize(units)

    def track(self, rel: str) -> None:
        """Remember ``rel`` (a file the caller is about to create or overwrite in the tree) so
        that ``close``, or recovery after a killed run, puts it back (removes it if new)."""
        self._save(rel)

    def close(self) -> None:
        """Restore every touched file to its original content (idempotent)."""
        self._state = "unset"
        self._restore_originals()

    @staticmethod
    def discard(path: Path) -> None:
        """The tree is shared and long-lived: nothing to delete."""


def _paths(files: list[FilePatch]) -> list[str]:
    out: list[str] = []
    for fp in files:
        for p in (fp.old_path, fp.new_path):
            if p and p not in out:
                out.append(p)
    return out
