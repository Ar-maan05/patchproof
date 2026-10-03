"""Writing a reproducer for a review comment's CLAIM (the writer never sees the suggestion)."""

from __future__ import annotations

import re
import shlex
from pathlib import Path, PurePosixPath

from .suggestion import Comment, split_lines
from .testresults import pytest_prefix

C_EXTS = {".c", ".h", ".cc", ".cpp", ".cxx", ".hpp"}

REACHABILITY_RULE = (
    "REACHABILITY RULE (checked by a script after you reply): the reproducer may only drive the "
    "code through the project's real entry points: public functions, classes, commands and "
    "files that real callers use, with inputs that real callers can produce. It must not "
    "import or call names starting with an underscore from the module under review, must not "
    "call functions that are `static` in the reviewed C file (or #include that file), must not "
    "monkeypatch or mock anything inside the module under review, and must not build internal "
    "state that the public API cannot produce. If the claim can only be triggered by a value "
    "no caller can pass, do not fake it: write the most faithful test through the public API "
    "even if it ends up passing. A reproducer that breaks this rule is thrown away."
)

SYSTEM_PY = (
    "You write a minimal pytest reproducer for a claim made in a code review comment. You do "
    "not know what change the reviewer proposed and you must not guess one. The test must FAIL "
    "on the code as it is now, because of the behaviour the claim describes: assert the "
    "correct result with specific expected values (a crash is also a failure, but prefer an "
    "explicit assertion). Do not rely on import errors. Import the project the way its "
    "existing tests do. " + REACHABILITY_RULE + " Reply with exactly one ```python code block "
    "containing the whole test file."
)

SYSTEM_C = (
    "You write a minimal C reproducer for a claim made in a code review comment. You do not "
    "know what change the reviewer proposed and you must not guess one. The program must fail "
    "on the code as it is now (a failed assert(), a non-zero exit, or a sanitizer report) "
    "because of the behaviour the claim describes, and exit 0 when the behaviour is correct. "
    "Follow the conventions of the project's existing test file when one is shown. "
    + REACHABILITY_RULE
    + " Reply with exactly one ```c code block containing the whole file."
)


def detect_language(
    reproducer_path: str | None, comment_path: str | None, test_cmd: str
) -> str | None:
    for p in (reproducer_path, comment_path):
        if p:
            ext = PurePosixPath(p).suffix.lower()
            if ext == ".py":
                return "python"
            if ext in C_EXTS:
                return "c"
    if pytest_prefix(test_cmd) is not None:
        return "python"
    return None


def default_path(language: str | None, repo: Path) -> str | None:
    if language == "python":
        name = "test_patchproof_review.py"
        return f"tests/{name}" if (repo / "tests").is_dir() else name
    return None  # C and the rest: the project's build decides where a test must live


def default_cmd_template(test_cmd: str) -> str | None:
    """``<test-cmd prefix> -x -q {path}`` when the test command is plain pytest, else None."""
    prefix = pytest_prefix(test_cmd)
    if prefix is None:
        return None
    toks = shlex.split(test_cmd)[len(prefix) :]
    keep: list[str] = []
    i = 0
    while i < len(toks):
        if toks[i] == "-p" and i + 1 < len(toks):
            keep += toks[i : i + 2]
            i += 2
        else:
            i += 1
    return shlex.join([*prefix, *keep, "-x", "-q"]) + " {path}"


def numbered_window(src: str, start: int, end: int, radius: int = 40) -> tuple[str, int, int]:
    """Lines around ``start..end`` with line numbers; the commented lines are marked with >."""
    lines = [ln.rstrip("\r\n") for ln in split_lines(src)]
    lo, hi = max(1, start - radius), min(len(lines), end + radius)
    width = len(str(hi))
    out = []
    for n in range(lo, hi + 1):
        mark = ">" if start <= n <= end else " "
        out.append(f"{mark}{n:>{width}} | {lines[n - 1]}")
    return "\n".join(out), lo, hi


def _trunc(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[:limit] + "\n...[truncated]"


def build_messages(
    *,
    comment: Comment,
    claim: str,
    language: str,
    rel: str,
    cmd: str,
    source: str | None,
    existing_tests: tuple[str, str] | None,
) -> list[dict]:
    where = comment.path or "the pull request (not anchored to a line)"
    rng = comment.line_range()
    head = f"Review comment on {where}"
    if rng:
        head += f", lines {rng[0]}-{rng[1]}" if rng[0] != rng[1] else f", line {rng[0]}"
    parts = [f"{head}:\n\n{_trunc(claim, 6000)}"]
    if comment.diff_hunk:
        parts.append(
            f"Diff context of the comment:\n```diff\n{_trunc(comment.diff_hunk, 4000)}\n```"
        )
    if source is not None and rng:
        window, lo, hi = numbered_window(source, *rng)
        parts.append(
            f"Code under review ({comment.path}, lines {lo}-{hi}; lines marked > are the "
            f"commented ones):\n```\n{_trunc(window, 12000)}\n```"
        )
    if existing_tests:
        tpath, ttext = existing_tests
        parts.append(f"Existing tests for this module ({tpath}):\n```\n{_trunc(ttext, 6000)}\n```")
    parts.append(f"The reproducer will be saved as `{rel}` and run with: `{cmd}`\nWrite it now.")
    system = SYSTEM_C if language == "c" else SYSTEM_PY
    return [
        {"role": "system", "content": system},
        {"role": "user", "content": "\n\n".join(parts)},
    ]


def extract_block(reply: str, language: str) -> str | None:
    langs = r"c|cpp|c\+\+|h" if language == "c" else r"python|py"
    blocks = re.findall(rf"```(?:{langs})?[ \t]*\n(.*?)```", reply, re.S | re.I)
    if blocks:
        return max(blocks, key=len).strip("\n") + "\n"
    return None


def read_trunc(path: Path, limit: int = 6000) -> str:
    try:
        return _trunc(path.read_text(errors="replace"), limit)
    except OSError:
        return ""
