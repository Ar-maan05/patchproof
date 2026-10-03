"""``patchproof review``: is a review comment's suggestion actually necessary?

base = the PR head as it is, patch = the reviewer's suggestion, test = a reproducer of the
reviewer's CLAIM, written without seeing the suggestion.
"""

from __future__ import annotations

import shlex
import tempfile
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from .classify import classify_failure
from .diff import DiffError, FilePatch, parse_patch
from .llm import ChatFn, LLMError, make_chat
from .normalise import equivalent
from .reachability import check_reproducer
from .reproducer import (
    build_messages,
    default_cmd_template,
    default_path,
    detect_language,
    extract_block,
    read_trunc,
)
from .review_verdict import ReviewVerdict, combine
from .runner import RunResult, run_command, tail
from .suggestion import Comment, Suggestion, SuggestionError, parse_body, suggestion_from_comment
from .testfiles import find_existing_tests
from .workspace import ApplyError, PersistentWorkspace, Workspace

V = ReviewVerdict


@dataclass
class ReviewConfig:
    repo: Path
    comment: Comment
    test_cmd: str
    suggestion_patch: str | None = None  # --suggestion-patch: overrides suggestion blocks
    reproducer: str | None = None  # --reproducer: hand-written, skips the LLM
    reproducer_path: str | None = None
    reproducer_cmd: str | None = None  # template with {path}
    runs: int = 3
    timeout: float = 300.0
    persistent_workdir: Path | None = None
    max_attempts: int = 3
    reachability: bool = True
    chat: ChatFn | None = None
    log: Callable[[str], None] = lambda msg: None


@dataclass
class ReviewReport:
    verdict: ReviewVerdict
    summary: str
    evidence: dict = field(default_factory=dict)
    hunks: list[dict] = field(default_factory=list)
    timings: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "verdict": str(self.verdict),
            "summary": self.summary,
            "evidence": self.evidence,
            "hunks": self.hunks,
            "timings": self.timings,
        }


def error_report(message: str, **evidence) -> ReviewReport:
    return ReviewReport(V.ERROR, message, {"error": message, **evidence})


def _read(path: Path) -> str | None:
    try:
        return path.read_bytes().decode("utf-8")
    except OSError, UnicodeDecodeError:
        return None


class _Stop(Exception):
    """Abort with a finished report."""

    def __init__(self, report: ReviewReport):
        self.report = report


class Review:
    def __init__(self, cfg: ReviewConfig):
        self.cfg = cfg
        self.t_start = time.monotonic()
        self.timings: dict[str, float] = {}
        self.evidence: dict = {}
        self.findings: list[tuple[ReviewVerdict, str]] = []
        self.suggestion: Suggestion | None = None
        self.code_files: list[FilePatch] = []
        self.claim = ""
        self.language: str | None = None
        self.rel: str | None = None
        self.cmd_tpl: str | None = None
        self.reviewed_src: str | None = None

    # -- helpers -----------------------------------------------------------------------------
    def _timed(self, key: str, t0: float) -> None:
        self.timings[key] = round(self.timings.get(key, 0.0) + time.monotonic() - t0, 3)

    def _finish(self, verdict: ReviewVerdict, summary: str) -> ReviewReport:
        self.timings["total"] = round(time.monotonic() - self.t_start, 3)
        self.evidence["findings"] = [{"verdict": str(v), "message": m} for v, m in self.findings]
        return ReviewReport(verdict, summary, self.evidence, [], self.timings)

    def _repro_cmd(self, rel: str) -> str:
        assert self.cmd_tpl is not None
        return self.cmd_tpl.replace("{path}", shlex.quote(rel))

    # -- input -------------------------------------------------------------------------------
    def _prepare(self) -> None:
        cfg = self.cfg
        if not cfg.repo.is_dir():
            raise _Stop(error_report(f"--repo is not a directory: {cfg.repo}"))
        if cfg.runs < 1:
            raise _Stop(error_report("--runs must be at least 1"))
        c = cfg.comment
        self.evidence["comment"] = c.to_evidence()
        self.evidence["test_cmd"] = cfg.test_cmd
        try:
            if cfg.suggestion_patch is not None:
                self.claim, _blocks = parse_body(c.body)
                ps = parse_patch(cfg.suggestion_patch)
                if not ps.files:
                    raise SuggestionError("--suggestion-patch has no file changes")
                self.code_files = ps.files
                self.evidence["suggestion"] = {"source": "--suggestion-patch",
                                               "patch": cfg.suggestion_patch}  # fmt: skip
            else:
                self.claim, self.suggestion = suggestion_from_comment(c, cfg.repo)
                if self.suggestion is not None:
                    self.code_files = parse_patch(self.suggestion.patch).files
                    self.evidence["suggestion"] = self.suggestion.to_evidence()
        except (SuggestionError, DiffError) as e:
            raise _Stop(error_report(f"cannot use the review comment: {e}")) from e
        if not self.code_files:
            self.evidence["suggestion"] = None
        self.evidence["claim"] = self.claim
        if c.path:
            self.reviewed_src = _read(cfg.repo / c.path)

        self.language = detect_language(cfg.reproducer_path, c.path, cfg.test_cmd)
        self.rel = cfg.reproducer_path or default_path(self.language, cfg.repo)
        self.cmd_tpl = cfg.reproducer_cmd or default_cmd_template(cfg.test_cmd)
        self.evidence["language"] = self.language

    def _need_reproducer_setup(self) -> str | None:
        """Why a reproducer cannot be run at all (None when it can)."""
        if self.language is None:
            return "cannot tell the language of the reproducer: pass --reproducer-path"
        if self.rel is None:
            return f"--reproducer-path is required for {self.language} reproducers"
        if self.cmd_tpl is None:
            return "--reproducer-cmd is required unless --test-cmd is a plain pytest command"
        return None

    # -- main --------------------------------------------------------------------------------
    def run(self) -> ReviewReport:
        try:
            self._prepare()
            return self._run()
        except _Stop as s:
            return s.report

    def _run(self) -> ReviewReport:
        cfg = self.cfg
        problem = self._need_reproducer_setup()
        if problem and (cfg.reproducer is not None or self.claim.strip()):
            return error_report(problem, **self.evidence)
        self.evidence["persistent_workdir"] = cfg.persistent_workdir is not None
        with tempfile.TemporaryDirectory(prefix="patchproof-review-") as tmp:
            ws: Workspace | PersistentWorkspace
            try:
                if cfg.persistent_workdir is not None:
                    ws = PersistentWorkspace(cfg.repo, cfg.persistent_workdir, [], self.code_files)
                else:
                    ws = Workspace(cfg.repo, Path(tmp), [], self.code_files)
            except (ApplyError, OSError) as e:
                return error_report(str(e), **self.evidence)
            try:
                return self._with_workspace(ws)
            except ApplyError as e:
                return error_report(f"the suggestion does not apply to the PR head: {e}")
            finally:
                ws.close()

    def _with_workspace(self, ws: Workspace | PersistentWorkspace) -> ReviewReport:
        # ---- normalisation: is the suggestion a no-op? (skips everything else) ---------------
        if self.code_files:
            head = ws.checkout(set())
            olds = {
                fp.path: _read(head / fp.old_path) if fp.old_path else None
                for fp in self.code_files
            }
            sugg = ws.checkout(None)
            news = {
                fp.path: _read(sugg / fp.new_path) if fp.new_path else None
                for fp in self.code_files
            }
            checks = []
            for fp in self.code_files:
                same, how = equivalent(fp.path, olds[fp.path], news[fp.path])
                checks.append({"file": fp.path, "equivalent": same, "method": how})
            self.evidence["normalisation"] = {"files": checks}
            if all(x["equivalent"] for x in checks):
                methods = sorted({x["method"] for x in checks})
                if methods == ["ast"]:
                    how = "identical AST ignoring docstrings and comments"
                elif "ast" not in methods:
                    how = "identical token stream ignoring comments and whitespace"
                else:
                    how = "identical after normalisation"
                return self._finish(
                    V.NO_BEHAVIOUR_CHANGE,
                    f"the suggestion changes no behaviour: {how}",
                )

        # ---- phase A: the PR head ---------------------------------------------------------------
        head = ws.checkout(set())
        self._suite(head, "head")
        repro = self._reproducer_stage(ws, head)

        # ---- phase B: head + suggestion ---------------------------------------------------------
        if self.code_files:
            sugg = ws.checkout(None)
            self._suite(sugg, "suggestion")
            if repro is not None and repro.get("failed_on_head"):
                self._repro_on_suggestion(ws, sugg, repro)
        self._judge(repro)
        verdict, summary = combine(self.findings)
        return self._finish(verdict, summary)

    # -- the project's own test suite ---------------------------------------------------------
    def _suite(self, d: Path, side: str) -> None:
        cfg = self.cfg
        t0 = time.monotonic()
        cfg.log(f"suite on {side} {cfg.runs}x")
        runs = [run_command(cfg.test_cmd, d, cfg.timeout) for _ in range(cfg.runs)]
        self._timed(f"suite_{side}", t0)
        suite = self.evidence.setdefault("suite", {})
        suite[f"{side}_runs"] = [r.summary() for r in runs]
        bad = next((r for r in runs if not r.passed), None)
        if bad is not None:
            suite[f"{side}_failure_output_tail"] = tail(bad.output)
        passed = sum(r.passed for r in runs)
        if 0 < passed < len(runs):
            self.findings.append(
                (V.FLAKY,
                 f"the project's test command is not stable on the {side} "
                 f"({passed}/{len(runs)} runs passed)")
            )  # fmt: skip
        suite[f"{side}_passed"] = passed
        if side == "suggestion":
            head_passed = suite.get("head_passed", 0)
            if head_passed == len(runs) and passed == 0:
                c = classify_failure(bad.output, bad.returncode, bad.timed_out) if bad else None
                suite["suggestion_failure_class"] = c.to_dict() if c else None
                self.findings.append(
                    (V.HARMFUL,
                     "the project's test command passes on the PR head and fails with the "
                     "suggestion applied")
                )  # fmt: skip
            elif head_passed != len(runs):
                suite["harmful_check"] = "not assessable: the test command does not pass on head"

    # -- the reproducer ----------------------------------------------------------------------
    def _place(self, ws: Workspace | PersistentWorkspace, d: Path, content: str) -> Path:
        assert self.rel is not None
        ws.track(self.rel)
        target = d / self.rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content)
        return target

    def _run_repro(
        self, ws: Workspace | PersistentWorkspace, d: Path, content: str, n: int
    ) -> list[RunResult]:
        assert self.rel is not None
        target = self._place(ws, d, content)
        try:
            return [run_command(self._repro_cmd(self.rel), d, self.cfg.timeout) for _ in range(n)]
        finally:
            target.unlink(missing_ok=True)

    def _initial_messages(self, evid: dict) -> list[dict]:
        cfg = self.cfg
        assert self.language is not None and self.rel is not None
        c = cfg.comment
        existing = None
        if c.path:
            found = find_existing_tests(cfg.repo, c.path)
            if found:
                existing = (found, read_trunc(cfg.repo / found))
        evid["existing_tests"] = existing[0] if existing else None
        return build_messages(
            comment=c,
            claim=self.claim,
            language=self.language,
            rel=self.rel,
            cmd=self._repro_cmd(self.rel),
            source=self.reviewed_src,
            existing_tests=existing,
        )

    def _reproducer_stage(self, ws, head: Path) -> dict | None:
        cfg = self.cfg
        evid: dict = {"source": "provided" if cfg.reproducer is not None else "generated",
                      "attempts": []}  # fmt: skip
        self.evidence["reproducer"] = evid
        if cfg.reproducer is None and not self.claim.strip():
            evid["skipped"] = "the comment states no claim to reproduce"
            return None
        assert self.rel is not None and self.cmd_tpl is not None
        if (head / self.rel).exists():
            raise _Stop(error_report(f"--reproducer-path {self.rel} already exists in --repo"))
        evid["path"] = self.rel
        evid["cmd"] = self._repro_cmd(self.rel)
        provided = cfg.reproducer is not None
        t0 = time.monotonic()
        try:
            messages = [] if provided else self._initial_messages(evid)
            chat = None if provided else (cfg.chat or make_chat())
            for n in range(1, 2 if provided else cfg.max_attempts + 1):
                att: dict = {"attempt": n}
                evid["attempts"].append(att)
                content: str | None
                if provided:
                    content = cfg.reproducer
                else:
                    assert chat is not None and self.language is not None
                    cfg.log(f"reproducer attempt {n}/{cfg.max_attempts}")
                    reply = chat(messages)
                    messages.append({"role": "assistant", "content": reply})
                    content = extract_block(reply, self.language)
                if content is None:
                    att["outcome"] = "no_code_block"
                    att["reason"] = "the reply had no fenced code block"
                    feedback = "No fenced code block found. Reply with exactly one fenced block."
                else:
                    att["content"] = content
                    feedback = self._screen(ws, head, content, att)
                    if feedback is None:
                        return self._accept(ws, head, content, att, evid)
                messages.append({"role": "user", "content": feedback})
        except LLMError as e:
            evid["error"] = str(e)
            self.findings.append((V.ERROR, f"reproducer generation failed: {e}"))
            return evid
        finally:
            self._timed("reproducer_head", t0)
        evid["failed_on_head"] = False
        return evid

    def _screen(self, ws, head: Path, content: str, att: dict) -> str | None:
        """Run one candidate on the head once; None when it is a valid failing reproducer."""
        cfg = self.cfg
        if cfg.reachability:
            violations = check_reproducer(
                self.language, content, cfg.comment.path, self.reviewed_src
            )
            if violations:
                att["outcome"] = "rejected_reachability"
                att["reason"] = "; ".join(violations)
                return (
                    "Rejected by the reachability rule: " + "; ".join(violations)
                    + ". Drive the code through its public entry points only."
                )  # fmt: skip
        r = self._run_repro(ws, head, content, 1)[0]
        att["first_run"] = r.summary()
        if r.passed:
            att["outcome"] = "passes_on_head"
            att["reason"] = "the reproducer passes on the PR head"
            return (
                "The test PASSES on the current code. It must fail there because of the "
                "behaviour the claim describes. Keep to public entry points."
            )
        c = classify_failure(r.output, r.returncode, r.timed_out)
        att["classification"] = c.to_dict()
        if not c.behavioural:
            att["outcome"] = "wrong_reason_failure"
            att["reason"] = c.reason
            return (
                f"The test fails for the wrong reason ({c.reason}). Output tail:\n"
                f"{tail(r.output, 1500)}"
            )
        att["outcome"] = "accepted"
        att["_output"] = r.output
        return None

    def _accept(self, ws, head: Path, content: str, att: dict, evid: dict) -> dict:
        cfg = self.cfg
        first_output = att.pop("_output")
        evid["content"] = content
        evid["failure_class"] = att["classification"]
        evid["failure_output_tail"] = tail(first_output)
        extra = self._run_repro(ws, head, content, cfg.runs - 1) if cfg.runs > 1 else []
        runs = [att["first_run"], *[r.summary() for r in extra]]
        evid["head_runs"] = runs
        passed = sum(r["passed"] for r in runs)
        evid["failed_on_head"] = passed == 0
        evid["head_flaky"] = 0 < passed < len(runs)
        return evid

    def _repro_on_suggestion(self, ws, sugg: Path, evid: dict) -> None:
        if evid.get("head_flaky"):
            return
        t0 = time.monotonic()
        runs = self._run_repro(ws, sugg, evid["content"], self.cfg.runs)
        self._timed("reproducer_suggestion", t0)
        evid["suggestion_runs"] = [r.summary() for r in runs]
        bad = next((r for r in runs if not r.passed), None)
        if bad is not None:
            evid["suggestion_failure_output_tail"] = tail(bad.output)

    # -- verdict ------------------------------------------------------------------------------
    def _judge(self, repro: dict | None) -> None:
        f = self.findings
        n_att = len((repro or {}).get("attempts", []))
        if repro is None or repro.get("error"):
            if repro is None:
                f.append((V.NOT_SHOWN, _not_shown("the comment states no claim to reproduce")))
            return
        if not repro.get("failed_on_head"):
            if repro.get("head_flaky"):
                f.append((V.FLAKY, "the reproducer's result on the PR head differs between runs"))
                return
            reasons = [
                f"#{a['attempt']}: {a.get('reason') or a['outcome']}" for a in repro["attempts"]
            ]
            f.append(
                (V.NOT_SHOWN,
                 _not_shown(f"{n_att} attempt(s): " + "; ".join(reasons) if reasons else ""))
            )  # fmt: skip
            return
        if not self.code_files:
            f.append(
                (V.CLAIM_CONFIRMED,
                 "the reproducer fails on the PR head; the comment gives no suggestion to test")
            )  # fmt: skip
            return
        runs = repro.get("suggestion_runs", [])
        passed = sum(r["passed"] for r in runs)
        if runs and passed == len(runs):
            f.append(
                (V.NECESSARY,
                 "the reproducer fails on the PR head and passes with the suggestion applied")
            )  # fmt: skip
        elif passed == 0:
            f.append(
                (V.CLAIM_CONFIRMED,
                 "the reproducer fails on the PR head, but the suggestion does not fix it "
                 "(it still fails with the suggestion applied)")
            )  # fmt: skip
        else:
            f.append((V.FLAKY, "the reproducer's result with the suggestion differs between runs"))


def _not_shown(detail: str) -> str:
    base = (
        "the claim was not reproduced through public entry points; that is evidence, not "
        "proof, that the suggestion is unnecessary"
    )
    return f"{base} ({detail})" if detail else base


def review(cfg: ReviewConfig) -> ReviewReport:
    try:
        return Review(cfg).run()
    except Exception as e:  # noqa: BLE001 - internal failures are ERROR by contract
        return error_report(f"internal error: {type(e).__name__}: {e}")
