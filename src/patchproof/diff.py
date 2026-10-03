"""Unified-diff parsing and hunk-level re-rendering."""

from __future__ import annotations

import re
from collections.abc import Callable, Collection, Iterable
from dataclasses import dataclass, field

HUNK_RE = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@ ?(.*)$")

# A unit of ablation: (index into the file list, index into that file's hunks).
Unit = tuple[int, int]


class DiffError(ValueError):
    """Raised when a patch cannot be parsed."""


@dataclass
class Hunk:
    old_start: int
    old_len: int
    new_start: int
    new_len: int
    section: str
    lines: list[str]  # each line keeps its leading ' ', '+', '-' or '\\' marker

    def header(self, new_start: int | None = None) -> str:
        ns = self.new_start if new_start is None else new_start
        tail = f" {self.section}" if self.section else ""
        return f"@@ -{self.old_start},{self.old_len} +{ns},{self.new_len} @@{tail}"

    def _walk(self):
        old_no = self.old_start if self.old_len > 0 else self.old_start + 1
        new_no = self.new_start if self.new_len > 0 else self.new_start + 1
        for line in self.lines:
            tag = line[:1]
            if tag == "+":
                yield "+", None, new_no, line[1:]
                new_no += 1
            elif tag == "-":
                yield "-", old_no, None, line[1:]
                old_no += 1
            elif tag == "\\":
                continue
            else:
                yield " ", old_no, new_no, line[1:]
                old_no += 1
                new_no += 1

    @property
    def added(self) -> list[tuple[int, str]]:
        """(new line number, text) for every added line."""
        return [(n, t) for tag, _o, n, t in self._walk() if tag == "+"]

    @property
    def removed(self) -> list[tuple[int, str]]:
        """(old line number, text) for every removed line."""
        return [(o, t) for tag, o, _n, t in self._walk() if tag == "-"]

    def old_anchor_lines(self) -> list[int]:
        """Old-side lines that represent this hunk: removed lines, else the insertion neighbours."""
        removed = [n for n, _ in self.removed]
        if removed:
            return removed
        return [n for n in (self.old_start, self.old_start + 1) if n >= 1]


@dataclass
class FilePatch:
    old_path: str | None
    new_path: str | None
    header: list[str] = field(default_factory=list)
    hunks: list[Hunk] = field(default_factory=list)
    extra: list[str] = field(default_factory=list)  # raw body when there are no hunks

    @property
    def path(self) -> str:
        return self.new_path or self.old_path or ""

    @property
    def is_new(self) -> bool:
        return self.old_path is None

    @property
    def is_delete(self) -> bool:
        return self.new_path is None

    def render(self, hunk_idx: Collection[int] | None = None) -> str:
        """Render this file's patch using only the hunks in ``hunk_idx`` (all when None)."""
        if not self.hunks:
            return "".join(f"{ln}\n" for ln in [*self.header, *self.extra])
        selected = [i for i in range(len(self.hunks)) if hunk_idx is None or i in hunk_idx]
        if not selected:
            return ""
        out = list(self.header)
        out.append(f"--- {'/dev/null' if self.old_path is None else 'a/' + self.old_path}")
        out.append(f"+++ {'/dev/null' if self.new_path is None else 'b/' + self.new_path}")
        selected_set = set(selected)
        for i in selected:
            h = self.hunks[i]
            skipped = sum(
                self.hunks[j].new_len - self.hunks[j].old_len
                for j in range(i)
                if j not in selected_set
            )
            out.append(h.header(h.new_start - skipped))
            out.extend(h.lines)
        return "".join(f"{ln}\n" for ln in out)


@dataclass
class PatchSet:
    files: list[FilePatch]

    def render(self, units: Collection[Unit] | None = None) -> str:
        """Render all files; ``units`` restricts hunks (files without hunks are always kept)."""
        parts = []
        for fi, fp in enumerate(self.files):
            if units is None:
                parts.append(fp.render())
            else:
                parts.append(fp.render({hi for (f, hi) in units if f == fi}))
        return "".join(parts)

    def all_units(self) -> list[Unit]:
        return [(fi, hi) for fi, fp in enumerate(self.files) for hi in range(len(fp.hunks))]


def _strip_prefix(path: str) -> str | None:
    path = path.split("\t")[0].strip()
    if path == "/dev/null":
        return None
    if path.startswith('"') and path.endswith('"'):
        path = path[1:-1]
    if path.startswith(("a/", "b/")):
        return path[2:]
    return path


def _git_header_paths(line: str) -> tuple[str | None, str | None]:
    rest = line[len("diff --git ") :]
    m = re.fullmatch(r"a/(.+) b/(.+)", rest)
    if m and m.group(1) == m.group(2):
        return m.group(1), m.group(2)
    # ambiguous (spaces / renames): try every split point
    for i, ch in enumerate(rest):
        if ch == " " and rest[:i].startswith("a/") and rest[i + 1 :].startswith("b/"):
            a, b = rest[2:i], rest[i + 3 :]
            if a == b:
                return a, b
    if m:
        return m.group(1), m.group(2)
    return None, None


def parse_patch(text: str) -> PatchSet:
    """Parse a unified/git diff into per-file patches with individually addressable hunks."""
    lines = text.split("\n")
    if lines and lines[-1] == "":
        lines.pop()
    files: list[FilePatch] = []
    cur: FilePatch | None = None
    i = 0
    n = len(lines)
    while i < n:
        line = lines[i]
        if line.startswith("diff --git "):
            a, b = _git_header_paths(line)
            cur = FilePatch(old_path=a, new_path=b, header=[line])
            files.append(cur)
            i += 1
            continue
        if cur is not None and not cur.hunks and not line.startswith(("--- ", "@@")):
            # extended header (or opaque body such as a binary patch)
            if line.startswith("rename from "):
                cur.old_path = line[len("rename from ") :]
            elif line.startswith("rename to "):
                cur.new_path = line[len("rename to ") :]
            elif line.startswith("new file mode"):
                cur.old_path = None
            elif line.startswith("deleted file mode"):
                cur.new_path = None
            cur.header.append(line)
            i += 1
            continue
        if line.startswith("--- ") and i + 1 < n and lines[i + 1].startswith("+++ "):
            old_p, new_p = _strip_prefix(line[4:]), _strip_prefix(lines[i + 1][4:])
            if cur is None or cur.hunks:
                cur = FilePatch(old_path=old_p, new_path=new_p)
                files.append(cur)
            else:
                cur.old_path, cur.new_path = old_p, new_p
            # header lines recorded so far must not include rename/mode/etc. losses: keep them
            i += 2
            continue
        m = HUNK_RE.match(line)
        if m:
            if cur is None:
                raise DiffError("hunk without file header")
            old_start, old_len, new_start, new_len = (
                int(m.group(1)),
                int(m.group(2)) if m.group(2) is not None else 1,
                int(m.group(3)),
                int(m.group(4)) if m.group(4) is not None else 1,
            )
            body: list[str] = []
            i += 1
            ro, rn = old_len, new_len
            while i < n and (ro > 0 or rn > 0 or lines[i].startswith("\\")):
                ln = lines[i]
                tag = ln[:1]
                if tag == "\\":
                    body.append(ln)
                elif tag == "+":
                    rn -= 1
                    body.append(ln)
                elif tag == "-":
                    ro -= 1
                    body.append(ln)
                elif tag == " " or ln == "":
                    ro -= 1
                    rn -= 1
                    body.append(ln if ln else " ")
                else:
                    raise DiffError(f"unexpected line in hunk: {ln!r}")
                i += 1
            if ro != 0 or rn != 0:
                raise DiffError("hunk line counts do not match its header")
            cur.hunks.append(Hunk(old_start, old_len, new_start, new_len, m.group(5), body))
            continue
        if cur is not None and not cur.hunks:
            cur.header.append(line)
        # otherwise: stray text between files (e.g. email preamble), ignore
        i += 1
    return PatchSet(files=[f for f in files if f.path])


def split_patch(
    ps: PatchSet, is_test: Callable[[str], bool]
) -> tuple[list[FilePatch], list[FilePatch]]:
    """Split files into (test_files, code_files) using ``is_test`` on the file path."""
    tests, code = [], []
    for fp in ps.files:
        paths = [p for p in (fp.old_path, fp.new_path) if p]
        (tests if any(is_test(p) for p in paths) else code).append(fp)
    return tests, code


def render_files(files: Iterable[FilePatch], units: Collection[Unit] | None = None) -> str:
    return PatchSet(list(files)).render(units)
