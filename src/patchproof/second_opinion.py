"""Second-opinion tests: an LLM that has never seen the patch writes extra tests for the bug.

Why independence matters: a fix and its test are usually written by the same author (or the
same model) from the same mental model of the bug. If that model is wrong, both are wrong in
the same way and the test passes. The second opinion is written from the *issue text* and the
*public contract* (signatures and docstrings of the touched functions, read from the HEAD
code) only. The LLM never receives the patch or any function body, so it cannot copy the
fix's assumptions.

Pipeline: generate -> run on HEAD -> drop broken tests -> judge each remaining failure against
the issue (majority vote) -> if enough confirmed failures remain, the fix is flagged.

The result is an LLM's opinion, not a proof. It is used to demote PROVEN to WEAK_TEST, never to
promote anything.
"""

from __future__ import annotations

import ast
import json
import re
import shlex
import sys
import tempfile
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from .classify import classify_failure
from .diff import FilePatch
from .llm import ChatFn, LLMError, extract_code
from .runner import run_command, tail
from .testresults import TestOutcome, parse_junit, pytest_prefix

TEST_FILE = "test_patchproof_second_opinion.py"

GEN_SYSTEM = (
    "You are a meticulous QA engineer. You are given a bug report and the public interface "
    "(signatures and docstrings) of the functions that were changed to fix it. You have NOT "
    "seen the fix. Write pytest tests that check the behaviour the bug report asks for. "
    "Concentrate on edge cases and boundary inputs a fix could plausibly get wrong: the exact "
    "case in the report, neighbouring inputs, empty/zero/None values, unusual characters, "
    "ordering and off-by-one boundaries. Rules: each test must assert a specific expected "
    "value that the bug report (or the docstring) clearly implies; do not assert anything the "
    "report does not determine; import the code under test exactly as described; no network, "
    "no files, no randomness. Reply with exactly one ```python code block containing the whole "
    "test file, with 4 to 8 small, independent test functions."
)

JUDGE_SYSTEM = (
    "You review a test written by someone else for a bug report. Decide whether the test's "
    "expectation is CONSISTENT with the bug report: the report (together with the documented "
    "interface) states or directly entails the exact value or behaviour the test asserts. "
    "If the expectation relies on an assumption the report does not make, or contradicts the "
    "report, or the report is silent on that input, it is NOT consistent. Be strict. Answer "
    'with one JSON object on a single line: {"consistent": true|false, "reason": "<short>"}.'
)


# --------------------------------------------------------------------------------------------
# public interface extraction (signatures + docstrings, never bodies)
# --------------------------------------------------------------------------------------------
def module_name(path: str) -> str:
    p = Path(path)
    parts = list(p.with_suffix("").parts)
    if parts and parts[0] == "src":
        parts = parts[1:]
    if parts and parts[-1] == "__init__":
        parts = parts[:-1]
    return ".".join(parts)


def _signature_stub(node: ast.FunctionDef | ast.AsyncFunctionDef, indent: str = "") -> str:
    """``def name(args) -> ret:`` plus the docstring, with the body dropped."""
    stub = copy_without_body(node)
    text = ast.unparse(ast.fix_missing_locations(ast.Module(body=[stub], type_ignores=[])))
    return "\n".join(indent + ln for ln in text.splitlines())


def copy_without_body(node: ast.FunctionDef | ast.AsyncFunctionDef):
    doc = ast.get_docstring(node, clean=True)
    body: list[ast.stmt] = [ast.Expr(ast.Constant(doc))] if doc else []
    body.append(ast.Expr(ast.Constant(...)))
    cls = type(node)
    return cls(
        name=node.name, args=node.args, body=body, decorator_list=[], returns=node.returns,
        type_comment=None, type_params=getattr(node, "type_params", []),
    )  # fmt: skip


def _collect(body: list[ast.stmt], changed: set[int], nested: bool) -> list[str]:
    entries: list[str] = []
    for n in body:
        if isinstance(n, ast.ClassDef):
            inner = _collect(n.body, changed, nested=True)
            if inner:
                doc = ast.get_docstring(n, clean=True)
                head = f"class {n.name}:" + (f"\n    {doc!r}" if doc else "")
                entries.append(head + "\n" + "\n".join(inner))
        elif isinstance(n, ast.FunctionDef | ast.AsyncFunctionDef):
            span = set(range(n.lineno, (n.end_lineno or n.lineno) + 1))
            if not span & changed or (n.name.startswith("_") and span <= changed):
                continue
            entries.append(_signature_stub(n, "    " if nested else ""))
    return entries


def touched_interface(files: list[tuple[str, str, set[int]]]) -> str:
    """Signatures/docstrings of functions whose lines intersect ``changed`` (HEAD line numbers).

    ``files`` holds (path, head_source, changed_lines). Functions that the patch adds entirely
    and that are private (leading underscore) are left out: their names would only leak how the
    fix is structured.
    """
    blocks: list[str] = []
    for path, src, changed in files:
        try:
            tree = ast.parse(src)
        except SyntaxError:
            continue
        entries = _collect(tree.body, changed, nested=False)
        if entries:
            blocks.append(
                f"# file: {path}  (import as: {module_name(path)})\n" + "\n\n".join(entries)
            )
    return "\n\n".join(blocks)


def changed_head_lines(code_files: list[FilePatch]) -> list[tuple[str, set[int]]]:
    """(new_path, lines of the HEAD file touched by the patch) for Python files."""
    out = []
    for fp in code_files:
        if not fp.new_path or not fp.new_path.endswith(".py"):
            continue
        lines: set[int] = set()
        for h in fp.hunks:
            lines |= {n for n, _ in h.added}
            if not h.added:  # pure deletion: the function around the join point
                lines |= set(range(max(h.new_start - 1, 1), h.new_start + 2))
        out.append((fp.new_path, lines))
    return out


# --------------------------------------------------------------------------------------------
# running the generated tests
# --------------------------------------------------------------------------------------------
@dataclass
class Verdicted:
    outcome: TestOutcome
    source: str
    votes: list[bool] = field(default_factory=list)
    reasons: list[str] = field(default_factory=list)

    @property
    def consistent(self) -> bool:
        return sum(self.votes) * 2 > len(self.votes)


def _split_functions(code: str) -> dict[str, str]:
    try:
        tree = ast.parse(code)
    except SyntaxError:
        return {}
    segs = {}
    for n in tree.body:
        if isinstance(n, ast.FunctionDef | ast.AsyncFunctionDef) and n.name.startswith("test"):
            segs[n.name] = ast.get_source_segment(code, n) or ""
    return segs


def _run_generated(
    head_dir: Path, test_cmd: str, code: str, timeout: float
) -> tuple[dict[str, TestOutcome], str]:
    (head_dir / TEST_FILE).write_text(code)
    prefix = pytest_prefix(test_cmd) or [sys.executable, "-m", "pytest"]
    with tempfile.TemporaryDirectory(prefix="patchproof-so-") as tmp:
        junit = Path(tmp) / "so.xml"
        cmd = shlex.join(
            [*prefix, "-q", "-p", "no:cacheprovider", f"--junitxml={junit}", TEST_FILE]
        )
        res = run_command(cmd, head_dir, timeout)
        xml = junit.read_text(errors="replace") if junit.exists() else ""
    return parse_junit(xml), res.output


def _collection_broken(outcomes: dict[str, TestOutcome]) -> bool:
    return not outcomes or any(
        o.status == "error" and "collection" in o.message.lower() for o in outcomes.values()
    )


def judge_messages(issue_text: str, interface: str, source: str, failure: str) -> list[dict]:
    """The judge conversation for one failing test (also used by eval/judge_eval.py)."""
    prompt = (
        f"Bug report:\n{issue_text[:8000]}\n\nPublic interface:\n```python\n"
        f"{interface[:6000]}\n```\n\nTest under review:\n```python\n{source}\n```\n\n"
        f"It fails against the current code with:\n{failure}\n\n"
        "Is the test's expectation consistent with the bug report?"
    )
    return [{"role": "system", "content": JUDGE_SYSTEM}, {"role": "user", "content": prompt}]


def parse_judgement(reply: str) -> tuple[bool, str]:
    m = re.search(r"\{.*?\}", reply, re.S)
    if m:
        try:
            obj = json.loads(m.group(0))
            return bool(obj.get("consistent")) is True, str(obj.get("reason", ""))[:300]
        except ValueError, AttributeError:
            pass
    return False, "unparseable judgement (counted as not consistent)"


def second_opinion(
    *,
    head_dir: Path,
    test_cmd: str,
    issue_text: str,
    interface: str,
    chat: ChatFn,
    timeout: float = 120.0,
    judge_samples: int = 3,
    min_confirmations: int = 1,
    log: Callable[[str], None] = lambda m: None,
) -> dict:
    """Run the whole second-opinion flow in ``head_dir`` (a scratch copy of HEAD).

    Returns evidence: ``{"flagged": bool, "confirmed": [...], "generated": N, "dropped": [...],
    "judged": [...], ...}``. Raises LLMError when the model cannot be reached.
    """
    ev: dict = {
        "note": "An LLM's independent opinion, not proof. Written from the issue text and the "
        "public interface only; the model never saw the patch or any function body.",
        "interface": interface,
        "flagged": False,
        "generated": 0,
        "dropped": [],
        "judged": [],
        "confirmed": [],
    }
    if not interface:
        ev["skipped"] = "no Python function touched by the patch to describe"
        return ev
    user = (
        f"Bug report:\n{issue_text[:8000]}\n\nPublic interface of the changed functions "
        f"(no bodies):\n```python\n{interface[:8000]}\n```\n\nWrite the test file now."
    )
    messages = [{"role": "system", "content": GEN_SYSTEM}, {"role": "user", "content": user}]
    log("second opinion: generating tests")
    code = extract_code(chat(messages))
    if code is None:
        ev["skipped"] = "the model returned no code block"
        return ev
    outcomes, output = _run_generated(head_dir, test_cmd, code, timeout)
    if _collection_broken(outcomes):  # nothing usable ran: one repair round
        log("second opinion: collection error, asking for a repair")
        messages += [
            {"role": "assistant", "content": f"```python\n{code}```"},
            {"role": "user", "content": "The file failed to collect or import. Output tail:\n"
             f"{tail(output, 1500)}\nFix the imports/syntax only and reply with the whole file."},
        ]  # fmt: skip
        repaired = extract_code(chat(messages))
        if repaired is not None:
            code = repaired
            outcomes, output = _run_generated(head_dir, test_cmd, code, timeout)
        if _collection_broken(outcomes):
            ev["skipped"] = "generated tests errored on collection"
            ev["collection_output_tail"] = tail(output, 800)
            return ev
    ev["generated"] = len(outcomes)
    segments = _split_functions(code)
    candidates: list[Verdicted] = []
    for o in outcomes.values():
        base_name = o.name.split("[", 1)[0]
        if o.status == "passed":
            continue
        why = None
        if o.status != "failed":
            why = f"{o.status}: not a test failure"
        else:
            text = f"{o.err_type}: {o.message}\n{o.detail}"
            fc = classify_failure(text, 1, False)
            msg = o.message.lstrip()
            is_assert = (
                msg.startswith(("assert", "AssertionError"))
                or "AssertionError" in o.err_type
                or "DID NOT RAISE" in msg
            )
            if not fc.behavioural:
                why = f"non-behavioural failure ({fc.kind})"
            elif not is_assert:
                why = f"failed with {o.err_type or 'an error'}, not an assertion"
        if why:
            ev["dropped"].append({"test": o.name, "reason": why})
        else:
            candidates.append(Verdicted(o, segments.get(base_name, "")))
    pending: list[str] = []
    for v in candidates:
        o = v.outcome
        failure = tail(f"{o.message}\n{o.detail}".strip(), 1200)
        messages = judge_messages(issue_text, interface, v.source, failure)
        try:
            for _ in range(max(1, judge_samples)):
                ok, reason = parse_judgement(chat(messages))
                v.votes.append(ok)
                v.reasons.append(reason)
        except LLMError as e:  # keep going so every pending judgement is asked for at once
            pending.append(str(e))
            continue
        entry = {"test": o.name, "votes": v.votes, "reasons": v.reasons,
                 "consistent": v.consistent}  # fmt: skip
        ev["judged"].append(entry)
        if v.consistent:
            ev["confirmed"].append(
                {"test": o.name, "source": v.source, "failure": failure, "votes": v.votes}
            )
    if pending:
        raise LLMError(f"{len(pending)} judgement(s) unanswered: {pending[0]}")
    ev["min_confirmations"] = min_confirmations
    ev["flagged"] = len(ev["confirmed"]) >= min_confirmations
    return ev


__all__ = [
    "LLMError",
    "changed_head_lines",
    "judge_messages",
    "parse_judgement",
    "second_opinion",
    "touched_interface",
]
