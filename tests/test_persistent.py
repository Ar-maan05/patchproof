"""--persistent-workdir: one reused working copy with an incremental-build-friendly lifecycle."""

import os
import shutil

import pytest
from conftest import PYTEST_CMD

from patchproof.diff import parse_patch, split_patch
from patchproof.pipeline import CheckConfig, check
from patchproof.testfiles import is_test_path
from patchproof.verdict import Verdict
from patchproof.workspace import ApplyError, PersistentWorkspace

BUGGY = "def last(xs):\n    return xs[len(xs)]\n"
FIXED = "def last(xs):\n    return xs[-1] if xs else None\n"
OTHER = "VALUE = 1\n"
TEST = "from calc import last\n\n\ndef test_last():\n    assert last([4, 5]) == 5\n    assert last([]) is None\n"
# The test command leaves a file behind (like a build dir) that must survive between runs.
BUILD_CMD = "echo run >> .build-cache && " + PYTEST_CMD


@pytest.fixture
def case(scenario, tmp_path):
    repo, patch = scenario(
        {"calc.py": BUGGY, "other.py": OTHER}, {"calc.py": FIXED, "tests/test_calc.py": TEST}
    )
    return repo, patch, tmp_path / "work"


def _cfg(repo, patch, workdir, **kw):
    return CheckConfig(repo=repo, patch_text=patch, test_cmd=kw.pop("test_cmd", PYTEST_CMD),
                       runs=1, timeout=60, persistent_workdir=workdir, **kw)  # fmt: skip


def test_persistent_verdict_matches_copy_mode(case):
    repo, patch, work = case
    plain = check(CheckConfig(repo=repo, patch_text=patch, test_cmd=PYTEST_CMD, runs=1))
    pers = check(_cfg(repo, patch, work))
    assert plain.verdict is Verdict.PROVEN
    assert pers.verdict is Verdict.PROVEN, pers.to_dict()
    assert pers.evidence["persistent_workdir"] is True
    assert pers.evidence["mutation"]["killed"] >= 1


def test_untouched_files_and_build_products_survive_and_touched_files_are_restored(case):
    repo, patch, work = case
    check(_cfg(repo, patch, work, test_cmd=BUILD_CMD, mutation=False))
    tree = work / "tree"
    assert (tree / ".build-cache").read_text().count("run") >= 2  # shared by every run
    assert (tree / "calc.py").read_text() == BUGGY  # restored to base
    assert not (tree / "tests" / "test_calc.py").exists()  # test hunks removed again
    assert not (work / "backup").exists()
    assert (repo / "calc.py").read_text() == BUGGY  # --repo untouched
    assert not (repo / ".build-cache").exists()


def test_only_patched_files_are_rewritten(case):
    repo, patch, work = case
    check(_cfg(repo, patch, work, mutation=False))  # seed the tree
    other = work / "tree" / "other.py"
    os.utime(other, ns=(1_000_000_000, 1_000_000_000))
    check(_cfg(repo, patch, work, mutation=False))  # second patch check reuses the tree
    assert other.stat().st_mtime_ns == 1_000_000_000


def test_workdir_is_reused_across_checks(case):
    repo, patch, work = case
    a = check(_cfg(repo, patch, work, mutation=False))
    shutil.rmtree(repo)  # the tree already holds the base: --repo is not copied again
    repo.mkdir()
    b = check(_cfg(repo, patch, work, mutation=False))
    assert a.verdict is b.verdict is Verdict.PROVEN


def test_refuses_foreign_non_empty_directory(case):
    repo, patch, work = case
    work.mkdir()
    (work / "precious.txt").write_text("x")
    r = check(_cfg(repo, patch, work))
    assert r.verdict is Verdict.ERROR and "not empty" in r.summary
    assert (work / "precious.txt").exists()


def test_recovers_files_left_modified_by_a_crashed_run(case):
    repo, patch, work = case
    ps = parse_patch(patch)
    tests, code = split_patch(ps, is_test_path)
    ws = PersistentWorkspace(repo, work, tests, code)
    ws.materialize(None)
    assert (work / "tree" / "calc.py").read_text() == FIXED
    # simulated crash: no close(); a new workspace on the same dir puts everything back first
    ws2 = PersistentWorkspace(repo, work, tests, code)
    assert (work / "tree" / "calc.py").read_text() == BUGGY
    ws2.close()


def test_failing_patch_application_reports_error_and_restores(case):
    repo, patch, work = case
    bad = patch.replace("-    return xs[len(xs)]", "-    return something_else")
    r = check(_cfg(repo, bad, work))
    assert r.verdict is Verdict.ERROR
    assert (work / "tree" / "calc.py").read_text() == BUGGY
    with pytest.raises(ApplyError):
        ws = PersistentWorkspace(repo, work, [], parse_patch(bad).files)
        ws.materialize(None)


def test_exclude_patterns_skip_copying_in_both_modes(case):
    repo, patch, work = case
    (repo / "huge.bin").write_text("x")
    cmd = "test ! -e huge.bin && " + PYTEST_CMD
    plain = check(
        CheckConfig(repo=repo, patch_text=patch, test_cmd=cmd, runs=1, exclude=("*.bin",))
    )
    pers = check(_cfg(repo, patch, work, test_cmd=cmd, exclude=("*.bin",)))
    assert plain.verdict is pers.verdict is Verdict.PROVEN


def test_cli_flags(case, capsys):
    import json

    from patchproof import cli

    repo, patch, work = case
    pf = work.parent / "fix.patch"
    pf.write_text(patch)
    code = cli.main(["check", "--repo", str(repo), "--patch", str(pf), "--test-cmd", PYTEST_CMD,
                     "--runs", "1", "--json", "--no-mutation", "--persistent-workdir", str(work),
                     "--exclude", "*.bin"])  # fmt: skip
    data = json.loads(capsys.readouterr().out)
    assert code == 0 and data["evidence"]["persistent_workdir"] is True
