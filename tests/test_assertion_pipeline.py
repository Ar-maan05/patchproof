"""Assertion-strength verdicts and the mutation floor, end to end on tiny repos."""

from __future__ import annotations

import sys

import pytest
from conftest import PYTEST_CMD

from patchproof.pipeline import CheckConfig, check
from patchproof.report import render_text
from patchproof.verdict import Verdict

BUGGY = "def last(xs):\n    return xs[len(xs)]\n"
FIXED = "def last(xs):\n    return xs[-1] if xs else None\n"
STRONG_TEST = "from calc import last\n\n\ndef test_last():\n    assert last([1, 2, 3]) == 3\n"
NO_CRASH = (
    "from calc import last\n\n\ndef test_last():\n    r = last([1, 2, 3])\n"
    "    assert r is not None\n"
)


def run(scenario, test, **kw):
    repo, patch = scenario({"calc.py": BUGGY}, {"calc.py": FIXED, "tests/test_calc.py": test})
    return check(CheckConfig(repo=repo, patch_text=patch, test_cmd=PYTEST_CMD, timeout=60,
                             runs=1, mutation=False, **kw))  # fmt: skip


def test_weak_assertions_give_weak_test(scenario):
    r = run(scenario, NO_CRASH)
    assert r.verdict is Verdict.WEAK_TEST, r.to_dict()
    ev = r.evidence["assertion_strength"]
    ((name, entry),) = ev.items()
    assert name == "tests/test_calc.py::test_last"
    assert entry["class"] == "WEAK" and entry["reasons"]
    assert "weak assertions" in r.summary


def test_strong_assertions_stay_proven(scenario):
    r = run(scenario, STRONG_TEST)
    assert r.verdict is Verdict.PROVEN
    assert r.evidence["assertion_strength"]["tests/test_calc.py::test_last"]["class"] == "STRONG"


def test_only_discriminating_tests_are_graded(scenario):
    # a weak bystander that passes on both sides must not demote a strong flipping test
    test = STRONG_TEST + "\n\ndef test_bystander():\n    assert isinstance(last([1]), int)\n"
    # test_bystander fails on base too (IndexError) and passes on head => it flips and is weak
    r = run(scenario, test)
    ev = r.evidence["assertion_strength"]
    assert ev["tests/test_calc.py::test_last"]["class"] == "STRONG"
    assert ev["tests/test_calc.py::test_bystander"]["class"] == "WEAK"
    assert r.verdict is Verdict.PROVEN  # one strong discriminating test is enough


def test_can_be_disabled(scenario):
    r = run(scenario, NO_CRASH, assertion_analysis=False)
    assert r.verdict is Verdict.PROVEN and "assertion_strength" not in r.evidence


def test_non_pytest_command_is_unavailable_not_weak(scenario):
    runner = "import sys\nimport pytest\n\nsys.exit(pytest.main(['-q', '-p', 'no:cacheprovider', 'tests']))\n"
    repo, patch = scenario(
        {"calc.py": BUGGY, "runtests.py": runner},
        {"calc.py": FIXED, "tests/test_calc.py": NO_CRASH},
    )
    cmd = f"{sys.executable} runtests.py"  # not `pytest ...`: junit cannot be injected
    r = check(CheckConfig(repo=repo, patch_text=patch, test_cmd=cmd, timeout=60, runs=1,
                          mutation=False))  # fmt: skip
    assert "assertion_strength" not in r.evidence
    assert "plain pytest" in r.evidence["assertion_strength_unavailable"]
    assert r.verdict is Verdict.PROVEN  # unavailable analysis never demotes


# ---- mutation floor ----------------------------------------------------------------------
SURVIVES = "from calc import last\n\n\ndef test_last():\n    last([1, 2, 3])\n"


def run_mut(scenario, test, **kw):
    repo, patch = scenario({"calc.py": BUGGY}, {"calc.py": FIXED, "tests/test_calc.py": test})
    return check(CheckConfig(repo=repo, patch_text=patch, test_cmd=PYTEST_CMD, timeout=60,
                             runs=1, assertion_analysis=False, **kw))  # fmt: skip


def test_few_mutants_are_inconclusive_and_do_not_demote(scenario):
    # same weak test: with a low floor survivors demote it ...
    low = run_mut(scenario, SURVIVES, min_mutants=1)
    assert low.verdict is Verdict.WEAK_TEST and low.evidence["mutation"]["status"] == "conclusive"
    # ... with the default floor the tiny patch is inconclusive: no demotion, no support
    r = run_mut(scenario, SURVIVES, min_mutants=50)
    mu = r.evidence["mutation"]
    assert mu["status"] == "inconclusive" and mu["valid_mutants"] < 50
    assert r.verdict is Verdict.PROVEN
    assert f"mutation inconclusive: {mu['valid_mutants']} valid mutants" in r.summary
    assert "inconclusive" in render_text(r)


def test_inconclusive_mutation_does_not_hide_weak_assertions(scenario):
    repo, patch = scenario({"calc.py": BUGGY}, {"calc.py": FIXED, "tests/test_calc.py": NO_CRASH})
    r = check(
        CheckConfig(
            repo=repo, patch_text=patch, test_cmd=PYTEST_CMD, timeout=60, runs=1, min_mutants=50
        )
    )
    assert r.evidence["mutation"]["status"] == "inconclusive"
    assert r.verdict is Verdict.WEAK_TEST  # decided by the assertion analysis
    assert "inconclusive" not in r.summary


def test_floor_is_configurable(scenario):
    r = run_mut(scenario, STRONG_TEST, min_mutants=1)
    assert r.evidence["mutation"]["status"] == "conclusive" and r.verdict is Verdict.PROVEN


@pytest.mark.parametrize("floor", [0])
def test_floor_zero_disables(scenario, floor):
    r = run_mut(scenario, STRONG_TEST, min_mutants=floor)
    assert r.evidence["mutation"]["status"] == "conclusive"
