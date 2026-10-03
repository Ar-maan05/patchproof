"""End-to-end verdicts on tiny fixture repos (these really run pytest in temp workdirs)."""

from __future__ import annotations

import pytest
from conftest import PYTEST_CMD, make_patch

from patchproof.pipeline import CheckConfig, check
from patchproof.verdict import Verdict

BUGGY = "def last(xs):\n    return xs[len(xs)]\n"
FIXED = "def last(xs):\n    return xs[-1] if xs else None\n"
GOOD_TEST = (
    "from calc import last\n\n\n"
    "def test_last():\n    assert last([1, 2, 3]) == 3\n    assert last([]) is None\n"
)


def run(repo, patch, **kw):
    cfg = CheckConfig(repo=repo, patch_text=patch, test_cmd=PYTEST_CMD, timeout=60, **kw)
    return check(cfg)


def test_real_fix_is_proven(scenario):
    repo, patch = scenario({"calc.py": BUGGY}, {"calc.py": FIXED, "tests/test_calc.py": GOOD_TEST})
    r = run(repo, patch, runs=2)
    assert r.verdict is Verdict.PROVEN, r.to_dict()
    ev = r.evidence
    assert [x["passed"] for x in ev["base_runs"]] == [False, False]
    assert [x["passed"] for x in ev["head_runs"]] == [True, True]
    assert ev["coverage"]["available"]
    assert ev["coverage"]["base"]["frames_in_patched_files"][0]["file"] == "calc.py"
    assert ev["coverage"]["base"]["hunks"][0]["function"] == "last"
    assert ev["coverage"]["base"]["hunks"][0]["function_executed"] is True
    assert ev["mutation"]["mutants_run"] > 0 and ev["mutation"]["survived"] == 0
    assert [h["status"] for h in r.hunks if h["kind"] == "code"] == ["required"]
    assert r.timings["total"] > 0
    # the repo given with --repo must never be modified
    assert (repo / "calc.py").read_text() == BUGGY
    assert not (repo / "tests").exists()


def test_test_already_in_base(scenario):
    repo, patch = scenario({"calc.py": BUGGY, "tests/test_calc.py": GOOD_TEST}, {"calc.py": FIXED})
    r = run(repo, patch, runs=1)
    assert r.verdict is Verdict.PROVEN, r.to_dict()


def test_no_op_patch_test_passes_without_fix(scenario):
    base = {"calc.py": "def add(a, b):\n    return a + b\n"}
    test = "from calc import add\n\n\ndef test_add():\n    assert add(1, 2) == 3\n"
    repo, patch = scenario(
        base, {"calc.py": "def add(a, b):\n    return b + a\n", "tests/test_calc.py": test}
    )
    r = run(repo, patch, runs=2)
    assert r.verdict is Verdict.NO_OP, r.to_dict()
    assert "without the fix" in r.summary


def test_partial_patch_extra_unrelated_hunk(scenario):
    pad = "".join(f"# filler {i}\n" for i in range(12))
    base = {"calc.py": BUGGY + "\n" + pad + "\ndef double(x):\n    return x * 2\n"}
    fixed = FIXED + "\n" + pad + "\ndef double(x):\n    return x + x\n"
    repo, patch = scenario(base, {"calc.py": fixed, "tests/test_calc.py": GOOD_TEST})
    r = run(repo, patch, runs=1)
    assert r.verdict is Verdict.PARTIAL, r.to_dict()
    statuses = {h["id"]: h["status"] for h in r.hunks if h["kind"] == "code"}
    assert sorted(statuses.values()) == ["not_exercised", "required"]
    assert r.evidence["ablation"]["minimal_hunks"] == ["calc.py#1"]
    assert r.evidence["ablation"]["not_exercised"] == ["calc.py#2"]


def test_weak_test_asserts_nothing(scenario):
    weak = "from calc import last\n\n\ndef test_last():\n    last([1, 2, 3])\n"
    repo, patch = scenario({"calc.py": BUGGY}, {"calc.py": FIXED, "tests/test_calc.py": weak})
    r = run(repo, patch, runs=1)
    assert r.verdict is Verdict.WEAK_TEST, r.to_dict()
    mu = r.evidence["mutation"]
    assert mu["survived"] > 0 and mu["survival_ratio"] > 0.5
    assert mu["survivors"][0]["file"] == "calc.py"
    assert "line" in mu["survivors"][0] and mu["survivors"][0]["description"]


def test_no_mutation_skips_weak_test_detection(scenario):
    weak = "from calc import last\n\n\ndef test_last():\n    last([1, 2, 3])\n"
    repo, patch = scenario({"calc.py": BUGGY}, {"calc.py": FIXED, "tests/test_calc.py": weak})
    r = run(repo, patch, runs=1, mutation=False, assertion_analysis=False)
    assert r.verdict is Verdict.PROVEN
    assert r.evidence["mutation"] == {"skipped": "--no-mutation"}


def test_import_error_on_base_is_cannot_reproduce(scenario):
    test = "from calc import last, helper_new\n\n\ndef test_last():\n    assert last([1]) == 1\n"
    fixed = FIXED + "\n\ndef helper_new():\n    return 1\n"
    repo, patch = scenario({"calc.py": BUGGY}, {"calc.py": fixed, "tests/test_calc.py": test})
    r = run(repo, patch, runs=1)
    assert r.verdict is Verdict.CANNOT_REPRODUCE, r.to_dict()
    assert r.evidence["base_failure"]["classification"][0]["kind"] in ("import", "collection")
    assert "wrong reason" in r.summary


def test_fix_that_does_not_fix_is_cannot_reproduce(scenario):
    wrong = "def last(xs):\n    return xs[len(xs) - 2] if xs else None\n"
    repo, patch = scenario({"calc.py": BUGGY}, {"calc.py": wrong, "tests/test_calc.py": GOOD_TEST})
    r = run(repo, patch, runs=1)
    assert r.verdict is Verdict.CANNOT_REPRODUCE, r.to_dict()
    assert "does not fix" in r.summary


def test_flaky_test(scenario, tmp_path):
    counter = tmp_path / "counter.txt"
    flaky = (
        "from pathlib import Path\n\n\ndef test_flaky():\n"
        f"    p = Path({str(counter)!r})\n"
        "    n = int(p.read_text()) if p.exists() else 0\n"
        "    p.write_text(str(n + 1))\n"
        "    assert n % 2 == 1\n"
    )
    repo, patch = scenario({"calc.py": BUGGY}, {"calc.py": FIXED, "tests/test_flaky.py": flaky})
    r = run(repo, patch, runs=3)
    assert r.verdict is Verdict.FLAKY, r.to_dict()


def test_patch_that_does_not_apply_is_error(scenario):
    repo, patch = scenario({"calc.py": BUGGY}, {"calc.py": FIXED, "tests/test_calc.py": GOOD_TEST})
    (repo / "calc.py").write_text("something = 'else entirely'\n")
    r = run(repo, patch, runs=1)
    assert r.verdict is Verdict.ERROR
    assert "apply" in r.summary


def test_test_only_patch_is_error(scenario):
    repo, patch = scenario({"calc.py": BUGGY}, {"tests/test_calc.py": GOOD_TEST})
    r = run(repo, patch, runs=1)
    assert r.verdict is Verdict.ERROR


def test_missing_repo_is_error(tmp_path):
    r = run(tmp_path / "nope", make_patch({}, {"a.py": "x\n"}))
    assert r.verdict is Verdict.ERROR


def test_deleted_lines_fix_is_recognised_as_executed(scenario):
    base = {"calc.py": "def f(x):\n    x = x + 1\n    return x\n"}
    new = {"calc.py": "def f(x):\n    return x\n", "tests/test_f.py": (
        "from calc import f\n\n\ndef test_f():\n    assert f(1) == 1\n")}  # fmt: skip
    repo, patch = scenario(base, new)
    r = run(repo, patch, runs=1, mutation=False)
    assert r.verdict is Verdict.PROVEN, r.to_dict()
    assert r.evidence["coverage"]["head"]["hunks"][0]["removed_executed_on_base"]


def test_fix_lines_never_run_is_no_op_via_coverage(scenario, tmp_path):
    # A run-order dependent "flaky" test that happens to fail on base run #1 and pass on head
    # run #1: it looks like the patch fixed it, but the patch only adds a function nobody calls.
    counter = tmp_path / "counter.txt"
    test = (
        "from pathlib import Path\nfrom calc import f\n\n\ndef test_f():\n"
        f"    p = Path({str(counter)!r})\n"
        "    n = int(p.read_text()) if p.exists() else 0\n"
        "    p.write_text(str(n + 1))\n"
        "    assert f() == 1 and n % 2 == 1\n"
    )
    base = {"calc.py": "def f():\n    return 1\n"}
    new = {"calc.py": base["calc.py"] + "\n\ndef unused():\n    return 2\n",
           "tests/test_f.py": test}  # fmt: skip
    repo, patch = scenario(base, new)
    r = run(repo, patch, runs=1)
    assert r.verdict is Verdict.NO_OP, r.to_dict()
    assert "never run" in r.summary


def test_comment_only_patch_changes_no_executable_lines(scenario, tmp_path):
    counter = tmp_path / "counter.txt"
    test = (
        "from pathlib import Path\nfrom calc import f\n\n\ndef test_f():\n"
        f"    p = Path({str(counter)!r})\n"
        "    n = int(p.read_text()) if p.exists() else 0\n"
        "    p.write_text(str(n + 1))\n"
        "    assert f() == 1 and n % 2 == 1\n"
    )
    base = {"calc.py": "def f():\n    return 1\n"}
    repo, patch = scenario(
        base, {"calc.py": "# explained\n" + base["calc.py"], "tests/test_t.py": test}
    )
    r = run(repo, patch, runs=1)
    assert r.verdict is Verdict.NO_OP, r.to_dict()
    assert "no executable" in r.summary


@pytest.mark.parametrize("runs", [0])
def test_invalid_runs(scenario, runs):
    repo, patch = scenario({"calc.py": BUGGY}, {"calc.py": FIXED})
    assert run(repo, patch, runs=runs).verdict is Verdict.ERROR
