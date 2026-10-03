"""Second-opinion flow with a mocked LLM. Never touches the network."""

from __future__ import annotations

import json
import urllib.request

import pytest
from conftest import PYTEST_CMD

from patchproof import cli
from patchproof import second_opinion as so
from patchproof.llm import LLMError, strip_reasoning
from patchproof.pipeline import CheckConfig, check
from patchproof.verdict import Verdict

BUGGY = "def last(xs):\n    return xs[len(xs)]\n"
FIXED = (
    'def last(xs):\n    """Return the last element, or None when empty."""\n'
    "    return xs[-1] if xs else None\n"
)
GOOD_TEST = "from calc import last\n\n\ndef test_last():\n    assert last([1, 2, 3]) == 3\n"
ISSUE = "last() raises IndexError on every list. It should return the final element."

GENERATED = """from calc import last


def test_ok():
    assert last([1, 2]) == 2


def test_wrong_expectation():
    assert last([1, 2]) == 99


def test_crashy():
    last()


def test_real_edge():
    assert last([5]) == 4
"""


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    def boom(*a, **k):
        raise AssertionError("network access attempted in tests")

    monkeypatch.setattr(urllib.request, "urlopen", boom)


class FakeLLM:
    """Routes by system prompt: generator vs judge. Records every prompt it receives."""

    def __init__(self, generated=GENERATED, judge=lambda prompt: True, repair=None):
        self.generated, self.judge, self.repair = generated, judge, repair
        self.prompts: list[str] = []
        self.gen_calls = 0

    def __call__(self, messages):
        self.prompts.append("\n".join(m["content"] for m in messages))
        if "QA engineer" in messages[0]["content"]:
            self.gen_calls += 1
            code = self.generated if self.gen_calls == 1 or self.repair is None else self.repair
            return f"sure\n```python\n{code}```"
        ok = self.judge(messages[-1]["content"])
        return json.dumps({"consistent": ok, "reason": "because"})


def run(scenario, chat, **kw):
    repo, patch = scenario({"calc.py": BUGGY}, {"calc.py": FIXED, "tests/test_calc.py": GOOD_TEST})
    cfg = CheckConfig(
        repo=repo, patch_text=patch, test_cmd=PYTEST_CMD, timeout=60, runs=1, mutation=False,
        second_opinion=True, issue_text=ISSUE, chat=chat, **kw,
    )  # fmt: skip
    return check(cfg)


def test_confirmed_failure_demotes_to_weak_test(scenario):
    chat = FakeLLM(judge=lambda p: "== 4" in p)
    r = run(scenario, chat)
    assert r.verdict is Verdict.WEAK_TEST, r.to_dict()
    assert r.summary.startswith("fix appears wrong: independent test test_real_edge fails")
    ev = r.evidence["second_opinion"]
    assert ev["flagged"] and ev["generated"] == 4
    assert [c["test"] for c in ev["confirmed"]] == ["test_real_edge"]
    assert "== 4" in ev["confirmed"][0]["source"] and ev["confirmed"][0]["failure"]
    assert "opinion" in ev["note"]


def test_broken_tests_are_dropped_and_inconsistent_ones_not_confirmed(scenario):
    chat = FakeLLM(judge=lambda p: "== 4" in p)
    ev = run(scenario, chat).evidence["second_opinion"]
    dropped = {d["test"]: d["reason"] for d in ev["dropped"]}
    assert "test_crashy" in dropped and "not an assertion" in dropped["test_crashy"]
    judged = {j["test"]: j["consistent"] for j in ev["judged"]}
    assert judged == {"test_wrong_expectation": False, "test_real_edge": True}


def test_all_judged_inconsistent_stays_proven(scenario):
    r = run(scenario, FakeLLM(judge=lambda p: False))
    assert r.verdict is Verdict.PROVEN, r.to_dict()
    assert r.evidence["second_opinion"]["flagged"] is False


def test_llm_never_sees_patch_or_bodies(scenario):
    chat = FakeLLM(judge=lambda p: False)
    run(scenario, chat)
    seen = "\n".join(chat.prompts)
    assert ISSUE in seen
    assert "def last(xs)" in seen and "Return the last element" in seen  # contract
    for leaked in ("xs[-1]", "xs[len(xs)]", "if xs else None", "diff --git", "@@", "+++"):
        assert leaked not in seen, leaked


def test_judge_majority_vote(scenario):
    votes = iter([True, False, True] + [False] * 30)

    chat = FakeLLM(judge=lambda p: next(votes))
    r = run(scenario, chat)
    # first candidate (wrong_expectation) got T,F,T = consistent: flagged on it
    ev = r.evidence["second_opinion"]
    assert ev["judged"][0]["votes"] == [True, False, True] and ev["judged"][0]["consistent"]
    assert r.verdict is Verdict.WEAK_TEST


def test_min_confirmations_two_requires_two(scenario):
    r = run(scenario, FakeLLM(judge=lambda p: "== 4" in p), min_confirmations=2)
    assert r.verdict is Verdict.PROVEN
    assert r.evidence["second_opinion"]["min_confirmations"] == 2


def test_collection_error_gets_one_repair_round(scenario):
    broken = "from nonexistent import last\n\n\ndef test_x():\n    assert last([5]) == 4\n"
    chat = FakeLLM(generated=broken, repair=GENERATED, judge=lambda p: "== 4" in p)
    r = run(scenario, chat)
    assert chat.gen_calls == 2
    assert r.verdict is Verdict.WEAK_TEST


def test_unrepairable_collection_error_is_skipped(scenario):
    broken = "from nonexistent import last\n\n\ndef test_x():\n    assert last([5]) == 4\n"
    r = run(scenario, FakeLLM(generated=broken, repair=broken))
    assert r.verdict is Verdict.PROVEN
    assert "collection" in r.evidence["second_opinion"]["skipped"]


def test_passing_generated_tests_change_nothing(scenario):
    ok = "from calc import last\n\n\ndef test_a():\n    assert last([1, 2]) == 2\n"
    r = run(scenario, FakeLLM(generated=ok))
    assert r.verdict is Verdict.PROVEN
    assert r.evidence["second_opinion"]["confirmed"] == []


def test_no_issue_text_skips(scenario):
    r = (
        run(
            scenario,
            FakeLLM(),
        )
        if False
        else None
    )
    repo, patch = scenario({"calc.py": BUGGY}, {"calc.py": FIXED, "tests/test_calc.py": GOOD_TEST})
    cfg = CheckConfig(repo=repo, patch_text=patch, test_cmd=PYTEST_CMD, timeout=60, runs=1,
                      mutation=False, second_opinion=True, chat=FakeLLM())  # fmt: skip
    r = check(cfg)
    assert r.verdict is Verdict.PROVEN and "no issue" in r.evidence["second_opinion"]["skipped"]


def test_llm_outage_is_not_an_error(scenario):
    def down(messages):
        raise LLMError("connection refused")

    r = run(scenario, down)
    assert r.verdict is Verdict.PROVEN
    assert "LLM unavailable" in r.evidence["second_opinion"]["skipped"]


def test_off_by_default(scenario):
    repo, patch = scenario({"calc.py": BUGGY}, {"calc.py": FIXED, "tests/test_calc.py": GOOD_TEST})
    cfg = CheckConfig(repo=repo, patch_text=patch, test_cmd=PYTEST_CMD, timeout=60, runs=1,
                      mutation=False, issue_text=ISSUE, chat=FakeLLM())  # fmt: skip
    assert "second_opinion" not in check(cfg).evidence


# ---- interface extraction ----------------------------------------------------------------
SRC = '''
import os

SECRET = "x"


def public(a: int, b: str = "x") -> bool:
    """Docs for public."""
    return secret_body(a)


def _private_new(a):
    return a + 1


def untouched():
    return 1


class K:
    """Klass."""

    def method(self, n):
        """Method docs."""
        return n * 41
'''


def test_touched_interface_has_signatures_docstrings_no_bodies():
    lines = {i + 1 for i, ln in enumerate(SRC.splitlines()) if "secret_body" in ln or "41" in ln}
    private = {
        i + 1 for i, ln in enumerate(SRC.splitlines()) if "a + 1" in ln or "_private_new" in ln
    }
    out = so.touched_interface([("src/pkg/mod.py", SRC, lines | private)])
    assert "def public(a: int, b: str='x') -> bool:" in out.replace('"x"', "'x'")
    assert "Docs for public." in out and "Method docs." in out and "class K" in out
    assert "import as: pkg.mod" in out
    assert "secret_body" not in out and "n * 41" not in out and "a + 1" not in out
    assert "_private_new" not in out and "untouched" not in out


def test_module_name():
    assert so.module_name("src/pkg/mod.py") == "pkg.mod"
    assert so.module_name("calc.py") == "calc"
    assert so.module_name("pkg/__init__.py") == "pkg"


def test_strip_reasoning():
    assert strip_reasoning("<think>hmm\nlots</think>\n```python\nx\n```") == "```python\nx\n```"
    assert strip_reasoning("</think>answer") == "answer"
    assert strip_reasoning("<think>never ends") == ""
    assert strip_reasoning("plain") == "plain"


# ---- CLI wiring --------------------------------------------------------------------------
def test_cli_second_opinion_requires_issue(tmp_path, capsys):
    patch = tmp_path / "p.diff"
    patch.write_text("x")
    argv = ["check", "--repo", str(tmp_path), "--patch", str(patch), "--test-cmd", "true"]
    assert cli.main([*argv, "--second-opinion"]) == 2
    assert "--issue" in capsys.readouterr().err


def test_cli_env_switch_enables_second_opinion(monkeypatch, tmp_path):
    seen = {}

    def fake_check(cfg):
        seen["cfg"] = cfg
        from patchproof.pipeline import error_report

        return error_report("stub")

    monkeypatch.setattr(cli, "check", fake_check)
    patch = tmp_path / "p.diff"
    patch.write_text("x")
    argv = ["check", "--repo", str(tmp_path), "--patch", str(patch), "--test-cmd", "true", "--json"]
    cli.main(argv)
    assert seen["cfg"].second_opinion is False and seen["cfg"].min_mutants == 5
    monkeypatch.setenv("PATCHPROOF_SECOND_OPINION", "1")
    cli.main(argv)  # no --issue: switch on, but nothing to say -> no usage error
    assert seen["cfg"].second_opinion is True


def test_unanswered_judgements_are_all_requested_in_one_run(scenario):
    """An exchange-style backend must see every judge prompt before the stage gives up."""
    judge_prompts = []

    def chat(messages):
        if "QA engineer" in messages[0]["content"]:
            return f"```python\n{GENERATED}```"
        judge_prompts.append(messages[-1]["content"])
        raise LLMError("awaiting a reply")

    r = run(scenario, chat)
    assert r.verdict is Verdict.PROVEN
    assert "unanswered" in r.evidence["second_opinion"]["skipped"]
    assert len(judge_prompts) == 2  # test_wrong_expectation and test_real_edge
