"""Optional LLM-backed reproducer generation (OpenAI-compatible chat endpoint)."""

from __future__ import annotations

import hashlib
import json
import os
import re
import shlex
import sys
import tempfile
import urllib.error
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from .classify import added_symbols, classify_failure
from .diff import FilePatch, parse_patch, split_patch
from .runner import run_command, tail
from .testfiles import is_test_path
from .workspace import ApplyError, Workspace

ChatFn = Callable[[list[dict]], str]


class LLMError(RuntimeError):
    pass


_THINK = re.compile(r"<think>.*?</think>", re.S | re.I)


def strip_reasoning(text: str) -> str:
    """Drop ``<think>...</think>`` blocks (and an unterminated leading one) from a reply."""
    text = _THINK.sub("", text)
    if "<think>" in text.lower() and "</think>" not in text.lower():
        text = re.split(r"<think>", text, maxsplit=1, flags=re.I)[0]
    if "</think>" in text.lower():  # reply that starts mid-thought
        text = re.split(r"</think>", text, maxsplit=1, flags=re.I)[1]
    return text.strip()


def request_key(messages: list[dict]) -> str:
    """Stable name for a conversation: identical prompts map to the same exchange files."""
    blob = json.dumps(messages, sort_keys=True, ensure_ascii=False).encode()
    return hashlib.sha256(blob).hexdigest()[:20]


def make_exchange_chat(directory: Path) -> ChatFn:
    """File-exchange backend: any agent or person can act as the model.

    A prompt without an answer is written to ``<key>.request.json`` and the call raises
    LLMError; whoever answers writes the reply text to ``<key>.reply.md`` and the run is
    repeated. File names are content hashes, so the answerer sees nothing but the prompt
    itself (no case names, no patch). Identical prompts share one reply, so repeated judge
    samples collapse into a single vote with this backend.
    """

    def chat(messages: list[dict]) -> str:
        key = request_key(messages)
        reply = directory / f"{key}.reply.md"
        if reply.exists():
            return strip_reasoning(reply.read_text(errors="replace"))
        directory.mkdir(parents=True, exist_ok=True)
        req = directory / f"{key}.request.json"
        req.write_text(json.dumps({"messages": messages}, indent=2, ensure_ascii=False))
        raise LLMError(f"awaiting a reply in {reply}")

    return chat


def make_chat(timeout: float = 300.0) -> ChatFn:
    """Chat function backed by PATCHPROOF_LLM_BASE_URL / _MODEL / _API_KEY.

    With PATCHPROOF_LLM_EXCHANGE_DIR set, prompts go through files instead (make_exchange_chat).
    """
    exchange = os.environ.get("PATCHPROOF_LLM_EXCHANGE_DIR")
    if exchange:
        return make_exchange_chat(Path(exchange))
    base = os.environ.get("PATCHPROOF_LLM_BASE_URL", "http://localhost:8080/v1").rstrip("/")
    model = os.environ.get("PATCHPROOF_LLM_MODEL", "local")
    key = os.environ.get("PATCHPROOF_LLM_API_KEY", "")

    def chat(messages: list[dict]) -> str:
        body = json.dumps({"model": model, "messages": messages, "temperature": 0.2}).encode()
        req = urllib.request.Request(
            f"{base}/chat/completions",
            data=body,
            headers={"Content-Type": "application/json",
                     **({"Authorization": f"Bearer {key}"} if key else {})},
        )  # fmt: skip
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                data = json.loads(resp.read())
            return strip_reasoning(data["choices"][0]["message"].get("content") or "")
        except (urllib.error.URLError, OSError, KeyError, IndexError, ValueError) as e:
            raise LLMError(f"LLM request to {base} failed: {e}") from e

    return chat


@dataclass
class GeneratedTest:
    path: str
    content: str
    test_cmd: str
    validated: bool
    attempts: int
    notes: list[str] = field(default_factory=list)

    def as_patch(self) -> str:
        return new_file_diff(self.path, self.content)


def new_file_diff(path: str, content: str) -> str:
    lines = content.splitlines()
    n = len(lines)
    body = "".join(f"+{ln}\n" for ln in lines)
    tail_marker = "" if content.endswith("\n") or not content else "\\ No newline at end of file\n"
    return (
        f"diff --git a/{path} b/{path}\nnew file mode 100644\n--- /dev/null\n+++ b/{path}\n"
        f"@@ -0,0 +1,{n} @@\n{body}{tail_marker}"
    )


_FENCE = re.compile(r"```(?:python|py)?\s*\n(.*?)```", re.S)


def extract_code(reply: str) -> str | None:
    blocks = _FENCE.findall(reply)
    if blocks:
        return max(blocks, key=len).strip("\n") + "\n"
    if re.search(r"^\s*(def test_|import |from )", reply, re.M):
        return reply.strip("\n") + "\n"
    return None


SYSTEM = (
    "You write minimal pytest reproducers for bug-fix patches. The test must FAIL on the "
    "unpatched code because of the bug's behaviour (an assertion about the wrong result), and "
    "PASS once the patch is applied. Do not test things that only exist after the patch (new "
    "function names), do not rely on import errors, and assert specific expected values. Reply "
    "with exactly one ```python code block containing the whole test file."
)


def _read_trunc(p: Path, limit: int = 6000) -> str:
    try:
        t = p.read_text(errors="replace")
    except OSError:
        return ""
    return t if len(t) <= limit else t[:limit] + "\n...[truncated]"


def generate_reproducer(
    repo: Path,
    patch_text: str,
    issue_text: str = "",
    *,
    chat: ChatFn | None = None,
    max_attempts: int = 3,
    timeout: float = 120.0,
    log: Callable[[str], None] = lambda m: None,
) -> GeneratedTest:
    """Ask the LLM for a failing reproducer; validate on base/head and feed output back."""
    chat = chat or make_chat()
    ps = parse_patch(patch_text)
    tests, code = split_patch(ps, is_test_path)
    if not code:
        raise LLMError("patch has no non-test changes")
    rel = (
        "tests/test_patchproof_repro.py"
        if (repo / "tests").is_dir()
        else "test_patchproof_repro.py"
    )
    cmd = shlex.join([sys.executable, "-m", "pytest", "-x", "-q", rel])
    code_patch = "".join(f.render() for f in code)
    sources = "\n\n".join(
        f"### {f.old_path} (before the patch)\n```\n{_read_trunc(repo / f.old_path)}\n```"
        for f in code
        if f.old_path
    )
    user = (
        f"Patch under test:\n```diff\n{code_patch[:20000]}\n```\n\n{sources}\n\n"
        + (f"Issue description:\n{issue_text[:8000]}\n\n" if issue_text else "")
        + f"The test file will be saved as `{rel}`. Write it now."
    )
    messages = [{"role": "system", "content": SYSTEM}, {"role": "user", "content": user}]
    symbols = added_symbols([t for f in code for h in f.hunks for _n, t in h.added])
    notes: list[str] = []
    last: tuple[str, bool] = ("", False)
    for attempt in range(1, max_attempts + 1):
        log(f"gen-test attempt {attempt}/{max_attempts}")
        reply = chat(messages)
        messages.append({"role": "assistant", "content": reply})
        content = extract_code(reply)
        if content is None:
            msg = "No code block found. Reply with one fenced python block."
            messages.append({"role": "user", "content": msg})
            notes.append(f"attempt {attempt}: no code block in reply")
            continue
        feedback = _validate(repo, tests, code, rel, content, cmd, symbols, timeout)
        last = (content, feedback is None)
        if feedback is None:
            return GeneratedTest(rel, content, cmd, True, attempt, notes)
        notes.append(f"attempt {attempt}: {feedback.splitlines()[0]}")
        messages.append({"role": "user", "content": feedback})
    if not last[0]:
        raise LLMError("the model never produced a usable test file")
    return GeneratedTest(rel, last[0], cmd, False, max_attempts, notes)


def _validate(
    repo: Path,
    tests: list[FilePatch],
    code: list[FilePatch],
    rel: str,
    content: str,
    cmd: str,
    symbols: set[str],
    timeout: float,
) -> str | None:
    """None when the reproducer fails on base and passes on head; else feedback for the model."""
    with tempfile.TemporaryDirectory(prefix="patchproof-gen-") as tmp:
        try:
            ws = Workspace(repo, Path(tmp), tests, code)
            base, head = ws.materialize(set()), ws.materialize(None)
        except ApplyError as e:
            raise LLMError(f"patch does not apply: {e}") from e
        for d in (base, head):
            f = d / rel
            f.parent.mkdir(parents=True, exist_ok=True)
            f.write_text(content)
        rb = run_command(cmd, base, timeout)
        if rb.passed:
            return ("The test PASSES on the unpatched code. It must fail there. Assert the "
                    "buggy behaviour's correct expected result.")  # fmt: skip
        c = classify_failure(rb.output, rb.returncode, rb.timed_out, symbols)
        if not c.behavioural:
            return (f"The test fails on the unpatched code for the wrong reason ({c.reason}). "
                    f"Output tail:\n{tail(rb.output, 1500)}")  # fmt: skip
        rh = run_command(cmd, head, timeout)
        if not rh.passed:
            return (
                "The test still FAILS with the patch applied. "
                f"Output tail:\n{tail(rh.output, 1500)}"
            )
    return None


__all__ = [
    "GeneratedTest",
    "LLMError",
    "strip_reasoning",
    "generate_reproducer",
    "make_chat",
    "new_file_diff",
]
