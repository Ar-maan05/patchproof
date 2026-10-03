#!/usr/bin/env python3
"""Self-check for the eval corpus. Does NOT use patchproof.

For every case it verifies that the patch applies and that the observed pytest
outcomes on base-with-test-changes and on head match the expected verdict:

  PROVEN            base fails behaviourally (not import/syntax error); head passes
  NO_OP             base-with-tests passes; head passes
  PARTIAL           base fails; head passes; partial.patch (essential hunk only) passes
                    and the patch has >= 2 non-test hunks
  WEAK_TEST         base fails behaviourally; head passes; mutant.patch (a different,
                    semantically weaker/wrong change) also passes the test;
                    wrong_fix cases additionally fail hidden_test.py on head
  CANNOT_REPRODUCE  repro_mode=base_import_error: base dies on ImportError and head passes
                    repro_mode=head_fails: head still fails
  FLAKY             head shows both a pass and a fail within --flaky-runs runs

Run:  uv run --python 3.14 --with pytest python eval/validate_corpus.py [--only GLOB] [--jobs N]
"""
from __future__ import annotations

import sys

sys.dont_write_bytecode = True

import argparse
import os
import re
import shutil
import subprocess
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from _common import CASES_DIR, Case, is_test_path, load_cases, patch_files, with_python

IMPORT_ERR = re.compile(r"ImportError|ModuleNotFoundError|SyntaxError|IndentationError")
MAX_RUN_SECONDS = 2.0


def run_test(cmd: str, cwd: Path, timeout: float = 60.0) -> tuple[int, str, float]:
    env = {**os.environ, "PYTHONDONTWRITEBYTECODE": "1", "PYTHONPATH": ""}
    t0 = time.perf_counter()
    try:
        p = subprocess.run(with_python(cmd), shell=True, cwd=cwd, env=env, capture_output=True,
                           text=True, timeout=timeout)
        return p.returncode, p.stdout + p.stderr, time.perf_counter() - t0
    except subprocess.TimeoutExpired:
        return 124, "timeout", time.perf_counter() - t0


def git_apply(patch: Path, cwd: Path) -> None:
    p = subprocess.run(["git", "apply", "--whitespace=nowarn", str(patch)], cwd=cwd,
                       capture_output=True, text=True)
    if p.returncode:
        raise RuntimeError(f"git apply {patch.name} failed: {p.stderr.strip()}")


def fresh(base: Path, work: Path, name: str) -> Path:
    d = work / name
    shutil.copytree(base, d)
    return d


def validate(case: Case, flaky_runs: int) -> tuple[list[str], list[str], float]:
    """Return (problems, info lines, max single-run seconds)."""
    problems: list[str] = []
    info: list[str] = []
    slowest = 0.0
    exp = case.expected_verdict
    patch_text = case.patch.read_text()
    with tempfile.TemporaryDirectory(prefix=f"pp-{case.id}-") as w:
        work = Path(w)

        def check_apply(patch: Path, name: str) -> Path | None:
            d = fresh(case.base, work, name)
            try:
                git_apply(patch, d)
            except RuntimeError as e:
                problems.append(str(e))
                return None
            return d

        head = check_apply(case.patch, "head")
        if head is None:
            return problems, info, slowest

        # base with only the test-file changes of the patch applied
        base_t = fresh(case.base, work, "base_tests")
        touched = patch_files(patch_text)
        tests = [f for f in touched if is_test_path(f)]
        code = [f for f in touched if not is_test_path(f)]
        if not tests:
            problems.append("patch contains no test-file changes")
        if not code:
            problems.append("patch contains no non-test changes")
        for f in tests:
            (base_t / f).parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(head / f, base_t / f)

        def run(d: Path) -> tuple[bool, str]:
            nonlocal slowest
            rc, out, dt = run_test(case.test_cmd, d)
            slowest = max(slowest, dt)
            return rc == 0, out

        h_ok, h_out = run(head)
        b_ok, b_out = run(base_t)
        b_import = bool(IMPORT_ERR.search(b_out))
        info.append(f"base={'pass' if b_ok else 'FAIL'} head={'pass' if h_ok else 'FAIL'}")

        if exp == "PROVEN":
            if b_ok: problems.append("test passes on base (expected failure)")
            if b_import: problems.append("base fails with import/syntax error, not behaviourally")
            if not h_ok: problems.append("test fails on head")
        elif exp == "NO_OP":
            if not b_ok: problems.append("test fails on base (NO_OP cases must pass without the fix)")
            if not h_ok: problems.append("test fails on head")
        elif exp == "PARTIAL":
            if b_ok: problems.append("test passes on base")
            if b_import: problems.append("base fails with import/syntax error")
            if not h_ok: problems.append("test fails on head")
            if len(code) and len(re.findall(r"^@@", "".join(
                    _file_section(patch_text, f) for f in code), flags=re.M)) < 2:
                problems.append("PARTIAL needs >= 2 non-test hunks")
            part = case.path / "partial.patch"
            if not part.exists():
                problems.append("missing partial.patch")
            else:
                pd = check_apply(part, "partial")
                if pd:
                    ok, _ = run(pd)
                    info.append(f"partial={'pass' if ok else 'FAIL'}")
                    if not ok: problems.append("essential-hunk-only patch does not pass the test")
        elif exp == "WEAK_TEST":
            if b_ok: problems.append("test passes on base (would be NO_OP)")
            if b_import: problems.append("base fails with import/syntax error")
            if not h_ok: problems.append("test fails on head")
            mut = case.path / "mutant.patch"
            hidden = case.path / "hidden_test.py"
            if not mut.exists() and not hidden.exists():
                problems.append("WEAK_TEST needs mutant.patch (weaker variant passes) or hidden_test.py (head is wrong)")
            if mut.exists():
                md = check_apply(mut, "mutant")
                if md:
                    ok, _ = run(md)
                    info.append(f"mutant={'pass' if ok else 'FAIL'}")
                    if not ok: problems.append("mutant.patch variant does not pass the test (test is not weak)")
            if hidden.exists():
                dest = head / "tests" / "test_hidden.py"
                shutil.copy2(hidden, dest)
                rc, out, dt = run_test("python -m pytest -q -p no:cacheprovider tests/test_hidden.py", head)
                info.append(f"hidden-on-head={'pass' if rc == 0 else 'FAIL'}")
                if rc == 0: problems.append("hidden_test.py passes on head (fix is not actually wrong)")
                elif IMPORT_ERR.search(out): problems.append("hidden_test.py fails with import error")
        elif exp == "CANNOT_REPRODUCE":
            mode = case.repro_mode
            if mode == "base_import_error":
                if b_ok: problems.append("test passes on base")
                elif not b_import: problems.append("base failure is not an import error")
                if not h_ok: problems.append("test fails on head")
            elif mode == "head_fails":
                if h_ok: problems.append("test passes on head (expected still failing)")
                if b_ok: problems.append("test passes on base")
            else:
                problems.append(f"CANNOT_REPRODUCE case needs repro_mode, got {mode!r}")
        elif exp == "FLAKY":
            outcomes = {h_ok}
            runs = 1
            while len(outcomes) < 2 and runs < flaky_runs:
                outcomes.add(run(head)[0]); runs += 1
            info.append(f"head outcomes after {runs} runs: {sorted(outcomes)}")
            if len(outcomes) < 2:
                problems.append(f"head never flipped over {runs} runs")
        else:
            problems.append(f"unknown expected_verdict {exp!r}")
    return problems, info, slowest


def _file_section(patch_text: str, path: str) -> str:
    parts = re.split(r"(?m)^(?=diff --git )", patch_text)
    return "".join(p for p in parts if p.startswith(f"diff --git a/{path} "))


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--cases-dir", default=str(CASES_DIR), help="corpus directory (default eval/cases)")
    ap.add_argument("--only", help="glob on case id")
    ap.add_argument("--jobs", type=int, default=4)
    ap.add_argument("--flaky-runs", type=int, default=40)
    ap.add_argument("-v", "--verbose", action="store_true")
    a = ap.parse_args()

    try:
        import pytest  # noqa: F401
    except ImportError:
        print("pytest is not importable by this interpreter; run with: uv run --with pytest python eval/validate_corpus.py")
        return 2

    cases = load_cases(a.only, Path(a.cases_dir).resolve())
    if not cases:
        print("no cases matched")
        return 2

    def one(c: Case):
        try:
            return c, *validate(c, a.flaky_runs)
        except Exception as e:  # noqa: BLE001
            return c, [f"validator crashed: {e!r}"], [], 0.0

    failed = 0
    by_exp: dict[str, int] = {}
    with ThreadPoolExecutor(a.jobs) as ex:
        for c, problems, info, slow in ex.map(one, cases):
            by_exp[c.expected_verdict] = by_exp.get(c.expected_verdict, 0) + 1
            slow_note = f"  [slow run {slow:.1f}s > {MAX_RUN_SECONDS}s]" if slow > MAX_RUN_SECONDS else ""
            if problems:
                failed += 1
                print(f"FAIL {c.id} ({c.expected_verdict}): " + "; ".join(problems))
            else:
                print(f"ok   {c.id} ({c.expected_verdict}) " + (" ".join(info) if a.verbose else "") + slow_note)
    print()
    print("cases per expected verdict:", dict(sorted(by_exp.items())))
    print(f"{len(cases) - failed}/{len(cases)} cases valid")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
