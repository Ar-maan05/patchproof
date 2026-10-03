"""The patchproof pipeline: from (repo, patch, test command) to an evidence-backed verdict."""

from __future__ import annotations

import ast
import tempfile
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from .classify import FailureClass, added_symbols, classify_failure
from .ddmin import ddmin
from .diff import DiffError, FilePatch, PatchSet, Unit, parse_patch, split_patch
from .mutation import C_LIKE_EXTS, Mutant, c_like_mutants, python_mutants, sample
from .pycov import (
    CoverageResult,
    line_to_statement,
    run_with_coverage,
    traceback_frames,
)
from .runner import RunResult, run_command, tail
from .testfiles import is_test_path
from .testresults import all_weak, discriminating, grade_tests, parse_junit, with_junit
from .verdict import BLOCKING, Verdict, combine
from .workspace import ApplyError, PersistentWorkspace, Workspace

DOC_EXTS = {".md", ".rst", ".txt"}


@dataclass
class CheckConfig:
    repo: Path
    patch_text: str
    test_cmd: str
    runs: int = 3
    mutation: bool = True
    timeout: float = 300.0
    mutation_threshold: float = 0.5
    max_mutants: int = 30
    min_mutants: int = 5
    assertion_analysis: bool = True
    second_opinion: bool = False
    issue_text: str = ""
    chat: Callable[[list[dict]], str] | None = None  # injectable LLM (tests); default: env config
    min_confirmations: int = 1
    persistent_workdir: Path | None = None  # one reused working copy (see PersistentWorkspace)
    exclude: tuple[str, ...] = ()  # extra fnmatch patterns not to copy from --repo
    log: Callable[[str], None] = lambda msg: None


@dataclass
class Report:
    verdict: Verdict
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


def error_report(message: str, **evidence) -> Report:
    return Report(Verdict.ERROR, message, {"error": message, **evidence})


def _ext(path: str) -> str:
    return Path(path).suffix.lower()


def _enclosing_function(tree: ast.AST, line: int) -> ast.AST | None:
    best = None
    for n in ast.walk(tree):
        is_func = isinstance(n, ast.FunctionDef | ast.AsyncFunctionDef)
        inside = is_func and n.lineno <= line <= (n.end_lineno or n.lineno)
        if inside and (best is None or n.lineno >= best.lineno):
            best = n
    return best


def _def_lines(src: str | None) -> set[int]:
    """Lines of def/class statements: running them only defines a name, it does not run the fix."""
    if not src:
        return set()
    try:
        tree = ast.parse(src)
    except SyntaxError:
        return set()
    return {
        n.lineno
        for n in ast.walk(tree)
        if isinstance(n, ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef)
    }


def _read(path: Path) -> str | None:
    try:
        return path.read_bytes().decode("utf-8")
    except OSError, UnicodeDecodeError:
        return None


def _hunk_infos(
    code_files: list[FilePatch], test_files: list[FilePatch]
) -> tuple[list[dict], dict[Unit, int]]:
    infos: list[dict] = []
    index: dict[Unit, int] = {}
    for fi, fp in enumerate(code_files):
        if not fp.hunks:
            infos.append({"id": f"{fp.path}#file", "file": fp.path, "kind": "code",
                          "status": "always_applied", "note": "no textual hunks"})  # fmt: skip
        for hi, h in enumerate(fp.hunks):
            index[(fi, hi)] = len(infos)
            infos.append(
                {
                    "id": f"{fp.path}#{hi + 1}",
                    "file": fp.path,
                    "kind": "code",
                    "old_range": [h.old_start, h.old_len],
                    "new_range": [h.new_start, h.new_len],
                    "section": h.section,
                    "added": len(h.added),
                    "removed": len(h.removed),
                    "status": "not_evaluated",
                    "executed": None,
                }
            )
    for fp in test_files:
        infos.append({"id": f"{fp.path}", "file": fp.path, "kind": "test", "status": "test",
                      "hunks": len(fp.hunks)})  # fmt: skip
    return infos, index


class Pipeline:
    def __init__(self, cfg: CheckConfig):
        self.cfg = cfg
        self.t_start = time.monotonic()
        self.timings: dict[str, float] = {}
        self.findings: list[tuple[Verdict, str]] = []
        self.evidence: dict = {}
        self.hunks: list[dict] = []
        self._persistent = False

    # -- helpers -----------------------------------------------------------------------------
    def _timed(self, key: str, t0: float) -> None:
        self.timings[key] = round(self.timings.get(key, 0.0) + time.monotonic() - t0, 3)

    def _finish(self) -> Report:
        verdict, summary = combine(self.findings)
        mu = self.evidence.get("mutation") or {}
        if verdict is Verdict.PROVEN and mu.get("status") == "inconclusive":
            summary += f" (mutation inconclusive: {mu['valid_mutants']} valid mutants)"
        self.timings["total"] = round(time.monotonic() - self.t_start, 3)
        self.evidence["findings"] = [{"verdict": str(v), "message": m} for v, m in self.findings]
        return Report(verdict, summary, self.evidence, self.hunks, self.timings)

    def _blocked(self) -> bool:
        return any(v in BLOCKING for v, _ in self.findings)

    # -- main --------------------------------------------------------------------------------
    def run(self) -> Report:
        cfg = self.cfg
        if not cfg.repo.is_dir():
            return error_report(f"--repo is not a directory: {cfg.repo}")
        if cfg.runs < 1:
            return error_report("--runs must be at least 1")
        try:
            ps = parse_patch(cfg.patch_text)
        except DiffError as e:
            return error_report(f"could not parse patch: {e}")
        if not ps.files:
            return error_report("patch is empty or contains no file changes")
        test_files, code_files = split_patch(ps, is_test_path)
        if not code_files:
            return error_report("patch only touches test files: there is no fix to prove")
        self.hunks, unit_index = _hunk_infos(code_files, test_files)
        units = PatchSet(code_files).all_units()
        self.evidence["test_files"] = [f.path for f in test_files]
        self.evidence["code_files"] = [f.path for f in code_files]
        self.evidence["test_cmd"] = cfg.test_cmd

        with tempfile.TemporaryDirectory(prefix="patchproof-") as tmp:
            persistent = cfg.persistent_workdir is not None
            ws: Workspace | PersistentWorkspace
            try:
                if cfg.persistent_workdir is not None:
                    ws = PersistentWorkspace(
                        cfg.repo, cfg.persistent_workdir, test_files, code_files, cfg.exclude
                    )
                else:
                    ws = Workspace(cfg.repo, Path(tmp), test_files, code_files, cfg.exclude)
                ws.checkout(set())
            except (ApplyError, OSError) as e:
                return error_report(str(e))
            try:
                try:
                    ws.checkout(None)
                except ApplyError as e:
                    return error_report(f"patch does not apply to the base: {e}")
                self.evidence["persistent_workdir"] = persistent
                self._persistent = persistent

                added_lines = [t for fp in code_files for h in fp.hunks for _n, t in h.added]
                symbols = added_symbols(added_lines)

                self._stage_runs(ws, symbols)
                if not self._blocked():
                    self._stage_coverage(ws, code_files, unit_index, Path(tmp))
                if cfg.assertion_analysis and not self._blocked():
                    self._stage_assertions(ws, Path(tmp))
                if not self._blocked():
                    minimal = self._stage_ablation(ws, units, unit_index)
                    if cfg.mutation:
                        self._stage_mutation(ws, code_files, minimal, unit_index)
                    else:
                        self.evidence["mutation"] = {"skipped": "--no-mutation"}
                if cfg.second_opinion and not self._blocked():
                    self._stage_second_opinion(ws, code_files)
            finally:
                ws.close()
        return self._finish()

    # -- stage 2-4: base/head runs ---------------------------------------------------------------
    def _stage_runs(self, ws: Workspace | PersistentWorkspace, symbols: set[str]) -> None:
        cfg = self.cfg
        t0 = time.monotonic()
        cfg.log(f"running base {cfg.runs}x")
        base_dir = ws.checkout(set())
        base = [run_command(cfg.test_cmd, base_dir, cfg.timeout) for _ in range(cfg.runs)]
        self._timed("base_runs", t0)
        t0 = time.monotonic()
        cfg.log(f"running head {cfg.runs}x")
        head_dir = ws.checkout(None)
        head = [run_command(cfg.test_cmd, head_dir, cfg.timeout) for _ in range(cfg.runs)]
        self._timed("head_runs", t0)

        base_fail = [r for r in base if not r.passed]
        classes: list[FailureClass] = [
            classify_failure(r.output, r.returncode, r.timed_out, symbols) for r in base_fail
        ]
        self.evidence["base_runs"] = [r.summary() for r in base]
        self.evidence["head_runs"] = [r.summary() for r in head]
        self.evidence["base_failure"] = {
            "classification": [c.to_dict() for c in classes[:1]] if classes else [],
            "output_tail": tail(base_fail[0].output) if base_fail else "",
        }
        if len(head) and not all(r.passed for r in head):
            first_bad = next(r for r in head if not r.passed)
            self.evidence["head_failure_output_tail"] = tail(first_bad.output)
        self._base_fail_output = base_fail[0].output if base_fail else ""

        n_base_pass = sum(r.passed for r in base)
        n_head_pass = sum(r.passed for r in head)
        n = cfg.runs
        if n_head_pass == 0:
            self.findings.append(
                (Verdict.CANNOT_REPRODUCE,
                 "test still fails with the patch applied: the fix does not fix it")
            )  # fmt: skip
        if base_fail and not any(c.behavioural for c in classes) and n_base_pass == 0:
            c = classes[0]
            self.findings.append(
                (Verdict.CANNOT_REPRODUCE,
                 f"test fails on the base for the wrong reason: {c.reason}"
                 + (f" [{c.detail}]" if c.detail else ""))
            )  # fmt: skip
        if 0 < n_base_pass < n or 0 < n_head_pass < n:
            self.findings.append(
                (Verdict.FLAKY,
                 f"results differ between runs (base passed {n_base_pass}/{n}, "
                 f"head passed {n_head_pass}/{n})")
            )  # fmt: skip
        if n_base_pass == n and n_head_pass == n:
            self.findings.append(
                (Verdict.NO_OP,
                 f"test passes without the fix (base + test changes passed {n}/{n} runs)")
            )  # fmt: skip
        elif n_base_pass == n and n_head_pass == 0:
            pass  # already CANNOT_REPRODUCE

    # -- stage 3/5: coverage -------------------------------------------------------------------
    def _stage_coverage(
        self,
        ws: Workspace | PersistentWorkspace,
        code_files: list[FilePatch],
        unit_index: dict[Unit, int],
        tmp: Path,
    ) -> None:
        cfg = self.cfg
        py_files = [fp for fp in code_files if _ext(fp.path) == ".py" and fp.hunks]
        other = [fp for fp in code_files if _ext(fp.path) != ".py"]
        cov_ev: dict = {"available": False}
        self.evidence["coverage"] = cov_ev
        if not py_files:
            cov_ev["reason"] = "no Python files in the patch"
            return
        if self._persistent:
            cov_ev["reason"] = "Python coverage needs both trees at once (not in persistent mode)"
            return
        base_dir, head_dir = ws.checkout(set()), ws.checkout(None)
        t0 = time.monotonic()
        cfg.log("coverage on base")
        base_cov = run_with_coverage(cfg.test_cmd, base_dir, cfg.timeout, tmp / "cov-base")
        cfg.log("coverage on head")
        head_cov = run_with_coverage(cfg.test_cmd, head_dir, cfg.timeout, tmp / "cov-head")
        self._timed("coverage", t0)
        for label, cov, want_pass in (("base", base_cov, False), ("head", head_cov, True)):
            if not cov.available:
                cov_ev["reason"] = f"{label}: {cov.error}"
                return
            if cov.run is not None and cov.run.passed != want_pass:
                cov_ev["reason"] = (
                    f"{label} run under coverage behaved differently from the plain runs"
                )
                return
        cov_ev["available"] = True
        self._base_cov, self._head_cov = base_cov, head_cov

        # ---- base: does the failing test even touch what the patch touches? -----------------
        patched_old = {
            fp.old_path: [(min(h.old_anchor_lines()), max(h.old_anchor_lines())) for h in fp.hunks]
            for fp in py_files
            if fp.old_path
        }
        frames = traceback_frames(self._base_fail_output, patched_old)
        base_ev: dict = {"frames_in_patched_files": frames, "hunks": []}
        head_ev: dict = {"hunks": []}
        any_executed = False
        any_executable = False
        self._exec_stmt_lines: dict[str, set[int]] = {}
        for fi, fp in enumerate(code_files):
            if fp not in py_files:
                continue
            head_src = _read(head_dir / fp.new_path) if fp.new_path else None
            base_src = _read(base_dir / fp.old_path) if fp.old_path else None
            bcov = base_cov.files.get(fp.old_path) if fp.old_path else None
            hcov = head_cov.files.get(fp.new_path) if fp.new_path else None
            if hcov:
                self._exec_stmt_lines[fp.new_path] = set(hcov.executed)
            base_tree = ast.parse(base_src) if base_src and _parses(base_src) else None
            for hi, h in enumerate(fp.hunks):
                info = self.hunks[unit_index[(fi, hi)]]
                # base side
                anchors = set(h.old_anchor_lines())
                b_all = line_to_statement(base_src, anchors) if base_src else set()
                b_stmts = b_all - _def_lines(base_src)
                region_exec = bool(bcov and b_stmts & bcov.executed)
                func = None
                func_exec = None
                if base_tree is not None and anchors:
                    fn = _enclosing_function(base_tree, min(anchors))
                    if fn is not None:
                        func = fn.name
                        span = range(fn.lineno, (fn.end_lineno or fn.lineno) + 1)
                        func_exec = bool(bcov and bcov.executed & set(span))
                base_ev["hunks"].append(
                    {
                        "id": info["id"],
                        "file_executed": bool(bcov and bcov.executed),
                        "region_executed": region_exec,
                        "function": func,
                        "function_executed": func_exec,
                    }
                )
                # head side: added lines that are executable / executed
                a_stmts = (
                    line_to_statement(head_src, {n for n, _ in h.added}) - _def_lines(head_src)
                    if head_src
                    else set()
                )
                a_exe = a_stmts & hcov.executable if hcov else set()
                a_run = a_stmts & hcov.executed if hcov else set()
                r_exe = b_stmts & bcov.executable if bcov and h.removed else set()
                r_run = b_stmts & bcov.executed if bcov and h.removed else set()
                executable = bool(a_exe or r_exe)
                executed = bool(a_run or r_run)
                any_executable |= executable
                any_executed |= executed
                info["executed"] = executed if executable else None
                info["executable_lines_changed"] = executable
                head_ev["hunks"].append(
                    {
                        "id": info["id"],
                        "added_executable": sorted(a_exe),
                        "added_executed": sorted(a_run),
                        "added_unexecuted": sorted(a_exe - a_run),
                        "removed_executed_on_base": sorted(r_run),
                    }
                )
        cov_ev["base"] = base_ev
        cov_ev["head"] = head_ev
        # Only conclude NO_OP when every non-Python file is documentation/config-like.
        if all(_ext(fp.path) in DOC_EXTS for fp in other):
            if not any_executable:
                self.findings.append(
                    (Verdict.NO_OP,
                     "the patch changes no executable Python lines (comments/docstrings only)")
                )  # fmt: skip
            elif not any_executed:
                self.findings.append(
                    (Verdict.NO_OP, "the fix's lines are never run by the test")
                )  # fmt: skip
        else:
            cov_ev["note"] = "non-Python code in patch: executed-lines check not decisive"

    # -- stage 5b: which tests discriminate, and do they assert anything specific? ---------------
    def _stage_assertions(self, ws: Workspace | PersistentWorkspace, tmp: Path) -> None:
        cfg = self.cfg
        outcomes = []
        for label, units in (("base", set()), ("head", None)):
            d = ws.checkout(units)
            junit = tmp / f"junit-{label}.xml"
            cmd = with_junit(cfg.test_cmd, junit)
            if cmd is None:
                self._as_unavailable("test command is not a plain pytest invocation")
                return
            cfg.log(f"per-test outcomes on {label}")
            run_command(cmd, d, cfg.timeout)
            xml = junit.read_text(errors="replace") if junit.exists() else ""
            parsed = parse_junit(xml)
            if not parsed:
                self._as_unavailable(f"no junit report from the {label} run")
                return
            outcomes.append(parsed)
        disc = discriminating(outcomes[0], outcomes[1])
        if not disc:
            self._as_unavailable("no test failed on base and passed on head in the per-test run")
            return
        graded = grade_tests(ws.checkout(None), disc)
        self.evidence.pop("assertion_strength_unavailable", None)
        self.evidence["assertion_strength"] = {test: s.to_dict() for test, s in graded.items()}
        if all_weak(graded):
            reasons = sorted({r for s in graded.values() for r in s.reasons})
            names = ", ".join(graded)
            self.findings.append(
                (Verdict.WEAK_TEST,
                 f"the tests that flip ({names}) only make weak assertions: " + "; ".join(reasons))
            )  # fmt: skip

    def _as_unavailable(self, reason: str) -> None:
        self.evidence["assertion_strength_unavailable"] = reason

    # -- stage 8: independent second-opinion tests (LLM) ---------------------------------------
    def _stage_second_opinion(
        self, ws: Workspace | PersistentWorkspace, code_files: list[FilePatch]
    ) -> None:
        from .llm import LLMError, make_chat
        from .second_opinion import changed_head_lines, second_opinion, touched_interface

        cfg = self.cfg
        t0 = time.monotonic()
        if self._persistent:
            self.evidence["second_opinion"] = {"skipped": "not available in persistent mode"}
            return
        if not cfg.issue_text.strip():
            self.evidence["second_opinion"] = {"skipped": "no issue text (--issue FILE)"}
            return
        d = ws.materialize(None)
        try:
            files = []
            for path, lines in changed_head_lines(code_files):
                src = _read(d / path)
                if src is not None:
                    files.append((path, src, lines))
            interface = touched_interface(files)
            try:
                ev = second_opinion(
                    head_dir=d,
                    test_cmd=cfg.test_cmd,
                    issue_text=cfg.issue_text,
                    interface=interface,
                    chat=cfg.chat or make_chat(),
                    timeout=cfg.timeout,
                    min_confirmations=cfg.min_confirmations,
                    log=cfg.log,
                )
            except LLMError as e:
                self.evidence["second_opinion"] = {"skipped": f"LLM unavailable: {e}"}
                return
        finally:
            ws.discard(d)
        self._timed("second_opinion", t0)
        self.evidence["second_opinion"] = ev
        if ev.get("flagged"):
            first = ev["confirmed"][0]
            self.findings.append(
                (Verdict.WEAK_TEST,
                 f"fix appears wrong: independent test {first['test']} fails "
                 "(LLM opinion, written without seeing the patch)")
            )  # fmt: skip

    # -- stage 6: ablation ---------------------------------------------------------------------
    def _stage_ablation(
        self, ws: Workspace | PersistentWorkspace, units: list[Unit], unit_index: dict[Unit, int]
    ) -> list[Unit]:
        cfg = self.cfg
        evid: dict = {"hunks_total": len(units)}
        self.evidence["ablation"] = evid
        if len(units) <= 1:
            evid["skipped"] = "single code hunk"
            for u in units:
                self.hunks[unit_index[u]]["status"] = "required"
            return units
        t0 = time.monotonic()
        cache: dict[frozenset[Unit], bool] = {}
        calls = {"n": 0}

        def passes(subset: list[Unit]) -> bool:
            key = frozenset(subset)
            if key in cache:
                return cache[key]
            calls["n"] += 1
            cfg.log(f"ablation: trying {len(subset)}/{len(units)} hunks")
            try:
                d = ws.materialize(set(subset))
            except ApplyError:
                cache[key] = False
                return False
            try:
                ok = run_command(cfg.test_cmd, d, cfg.timeout).passed
            finally:
                ws.discard(d)
            cache[key] = ok
            return ok

        minimal = ddmin(units, passes)
        self._timed("ablation", t0)
        evid["test_runs"] = calls["n"]
        evid["minimal_hunks"] = [self.hunks[unit_index[u]]["id"] for u in minimal]
        for u in units:
            self.hunks[unit_index[u]]["status"] = "required" if u in minimal else "not_exercised"
        if len(minimal) < len(units):
            skipped = [self.hunks[unit_index[u]]["id"] for u in units if u not in minimal]
            evid["not_exercised"] = skipped
            self.findings.append(
                (Verdict.PARTIAL,
                 f"only {len(minimal)} of {len(units)} code hunks are needed to make the test "
                 f"pass; not exercised: {', '.join(skipped)}")
            )  # fmt: skip
        return minimal

    # -- stage 7: mutation ---------------------------------------------------------------------
    def _stage_mutation(
        self,
        ws: Workspace | PersistentWorkspace,
        code_files: list[FilePatch],
        minimal: list[Unit],
        unit_index: dict[Unit, int],
    ) -> None:
        cfg = self.cfg
        t0 = time.monotonic()
        head_dir = ws.checkout(None)
        mutants: list[Mutant] = []
        minimal_set = set(minimal)
        for fi, fp in enumerate(code_files):
            if not fp.hunks or not fp.new_path:
                continue
            changed = {n for hi, h in enumerate(fp.hunks) if (fi, hi) in minimal_set
                       for n, _ in h.added}  # fmt: skip
            if not changed:
                continue
            src = _read(head_dir / fp.new_path)
            if src is None:
                continue
            ext = _ext(fp.new_path)
            if ext == ".py":
                executed = getattr(self, "_exec_stmt_lines", {}).get(fp.new_path)
                mutants += python_mutants(src, fp.new_path, changed, executed)
            elif ext in C_LIKE_EXTS:
                mutants += c_like_mutants(src, fp.new_path, changed)
        total_found = len(mutants)
        mutants = sample(mutants, cfg.max_mutants)
        evid: dict = {
            "threshold": cfg.mutation_threshold,
            "mutants_generated": total_found,
            "mutants_run": len(mutants),
            "capped": total_found > len(mutants),
        }
        self.evidence["mutation"] = evid
        if not mutants:
            evid.update(killed=0, survived=0, invalid=0, survival_ratio=None, survivors=[])
            evid["note"] = "no mutants could be generated for the changed lines"
            evid.update(valid_mutants=0, min_mutants=cfg.min_mutants, status="inconclusive")
            return

        head_times = [r["duration"] for r in self.evidence.get("head_runs", [])]
        avg = sum(head_times) / len(head_times) if head_times else 10.0
        mut_timeout = min(cfg.timeout, max(10.0, 5 * avg))
        killed, invalid, survivors = 0, 0, []
        originals: dict[str, bytes] = {}
        for i, m in enumerate(mutants, 1):
            cfg.log(f"mutation {i}/{len(mutants)}: {m.file}:{m.line} {m.description}")
            target = head_dir / m.file
            originals.setdefault(m.file, target.read_bytes())
            target.write_bytes(m.source.encode("utf-8"))
            try:
                r = run_command(cfg.test_cmd, head_dir, mut_timeout)
            finally:
                target.write_bytes(originals[m.file])
            if r.passed:
                survivors.append(m)
            else:
                c = classify_failure(r.output, r.returncode, r.timed_out)
                if c.kind in ("syntax", "compile", "command"):
                    invalid += 1
                else:
                    killed += 1
        self._timed("mutation", t0)
        valid = killed + len(survivors)
        ratio = len(survivors) / valid if valid else None
        evid.update(
            killed=killed,
            survived=len(survivors),
            invalid=invalid,
            survival_ratio=None if ratio is None else round(ratio, 3),
            survivors=[m.to_dict() for m in survivors],
        )
        evid["valid_mutants"] = valid
        evid["min_mutants"] = cfg.min_mutants
        if valid < cfg.min_mutants:
            # Too few valid mutants to say anything: not evidence for PROVEN, not grounds for
            # WEAK_TEST either. The verdict is decided by the other stages.
            evid["status"] = "inconclusive"
            return
        evid["status"] = "conclusive"
        if ratio is not None and ratio > cfg.mutation_threshold:
            self.findings.append(
                (Verdict.WEAK_TEST,
                 f"{len(survivors)}/{valid} mutants of the changed lines survived: the test "
                 "does not pin down the fix")
            )  # fmt: skip


def _parses(src: str) -> bool:
    try:
        ast.parse(src)
    except SyntaxError:
        return False
    return True


def check(cfg: CheckConfig) -> Report:
    try:
        return Pipeline(cfg).run()
    except Exception as e:  # noqa: BLE001 - the CLI contract says internal failures are ERROR
        return error_report(f"internal error: {type(e).__name__}: {e}")


__all__ = ["CheckConfig", "Report", "check", "RunResult", "CoverageResult"]
