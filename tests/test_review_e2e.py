"""End-to-end `patchproof review` on tiny fixture repos with a mocked ChatFn.

These really run pytest in temp workdirs. Network and gh are blocked.
"""

from __future__ import annotations

import json
import os
import shutil
import stat
import urllib.request

import pytest
from conftest import PYTEST_CMD, write_tree

from patchproof import cli, github
from patchproof.review import ReviewConfig, review
from patchproof.review_report import render_reply, render_text
from patchproof.review_verdict import ReviewVerdict as V
from patchproof.suggestion import Comment


@pytest.fixture(autouse=True)
def no_network_no_gh(monkeypatch, tmp_path_factory):
    def boom(*a, **k):
        raise AssertionError("network access attempted in tests")

    monkeypatch.setattr(urllib.request, "urlopen", boom)
    bindir = tmp_path_factory.mktemp("fakebin")
    marker = bindir / "gh-was-called"
    gh = bindir / "gh"
    gh.write_text(f"#!/bin/sh\ntouch {marker}\nexit 97\n")
    gh.chmod(gh.stat().st_mode | stat.S_IEXEC)
    monkeypatch.setenv("PATH", f"{bindir}{os.pathsep}{os.environ['PATH']}")
    yield
    assert not marker.exists(), "gh was invoked"


class FakeChat:
    def __init__(self, *replies):
        self.replies = list(replies)
        self.seen: list[list[dict]] = []

    def __call__(self, messages):
        self.seen.append([dict(m) for m in messages])
        return self.replies.pop(0)

    def prompts(self) -> str:
        return "\n".join(m["content"] for conv in self.seen for m in conv)


def fenced(code: str) -> str:
    return f"```python\n{code}```\n"


def make_comment(body, path="textutil.py", line=2, **kw):
    return Comment.from_json({"body": body, "path": path, "line": line, "side": "RIGHT",
                              "user": {"login": "review-bot"}, "diff_hunk": "@@ -1,2 +1,2 @@\n def first_word(text):\n+    return text.split()[0]",
                              "html_url": "https://github.com/o/r/pull/1#discussion_r1", **kw})  # fmt: skip


def run(repo, comment, chat=None, **kw):
    cfg = ReviewConfig(repo=repo, comment=comment, test_cmd=PYTEST_CMD, timeout=60, runs=2,
                       chat=chat, **kw)  # fmt: skip
    return review(cfg)


TEXTUTIL = "def first_word(text):\n    return text.split()[0]\n"
TEXT_TEST = (
    "from textutil import first_word\n\n\n"
    "def test_first_word():\n    assert first_word('hello world') == 'hello'\n"
)
EMPTY_REPRO = (
    "from textutil import first_word\n\n\n"
    "def test_blank_input():\n    assert first_word('   ') == ''\n"
)


@pytest.fixture
def textrepo(tmp_path):
    return write_tree(
        tmp_path / "repo", {"textutil.py": TEXTUTIL, "tests/test_textutil.py": TEXT_TEST}
    )


FIX = '    words = text.split()\n    return words[0] if words else ""\n'


def test_real_bug_with_working_suggestion_is_necessary(textrepo):
    chat = FakeChat(fenced(EMPTY_REPRO))
    c = make_comment(
        f"first_word('') raises IndexError instead of returning ''.\n\n```suggestion\n{FIX}```"
    )
    r = run(textrepo, c, chat)
    assert r.verdict is V.NECESSARY, r.to_dict()
    rp = r.evidence["reproducer"]
    assert [x["passed"] for x in rp["head_runs"]] == [False, False]
    assert [x["passed"] for x in rp["suggestion_runs"]] == [True, True]
    assert rp["failure_class"]["behavioural"]
    assert r.evidence["suite"]["head_passed"] == 2 and r.evidence["suite"]["suggestion_passed"] == 2
    assert rp["path"] == "tests/test_patchproof_review.py"
    # the writer never saw the suggested code
    assert "words[0] if words" not in chat.prompts()
    assert "raises IndexError" in chat.prompts()
    # the repo given to review is never modified
    assert (textrepo / "textutil.py").read_text() == TEXTUTIL
    assert not (textrepo / "tests" / "test_patchproof_review.py").exists()


def test_existing_tests_and_numbered_code_reach_the_writer(textrepo):
    chat = FakeChat(fenced(EMPTY_REPRO))
    run(textrepo, make_comment(f"IndexError on blank.\n```suggestion\n{FIX}```"), chat)
    user = chat.seen[0][1]["content"]
    assert "tests/test_textutil.py" in user and "test_first_word" in user
    assert ">2 |     return text.split()[0]" in user
    assert "REACHABILITY RULE" in chat.seen[0][0]["content"]


def test_card_and_reply_for_necessary(textrepo):
    chat = FakeChat(fenced(EMPTY_REPRO))
    r = run(textrepo, make_comment(f"IndexError on blank.\n```suggestion\n{FIX}```"), chat)
    card = render_text(r)
    assert "NECESSARY" in card and "head: 0/2 passed" in card
    reply = render_reply(r)
    assert reply.startswith("**patchproof verdict: NECESSARY**")
    assert "```python" in reply and "def test_blank_input" in reply
    reply.encode("ascii")
    # no hard wraps: every non-code paragraph is a single line
    prose = reply.split("```python")[0].strip().split("\n\n")
    assert all("\n" not in p for p in prose)


PRIVATE = (
    "from batches import _chunk\n\n\ndef test_zero_size():\n    assert _chunk([1], 0) == [[1]]\n"
)
PUBLIC_PASSES = (
    "from batches import batches\n\n\n"
    "def test_small():\n    assert batches([1, 2, 3]) == [[1, 2, 3]]\n"
)
BATCHES = (
    "def _chunk(items, size):\n"
    "    return [items[i:i + size] for i in range(0, len(items), size)]\n\n\n"
    "def batches(items):\n    return _chunk(items, 10)\n"
)
BATCH_TEST = (
    "from batches import batches\n\n\n"
    "def test_batches():\n    assert batches(list(range(25)))[2] == [20, 21, 22, 23, 24]\n"
)


def test_unreachable_defensive_check_is_not_shown(tmp_path):
    repo = write_tree(
        tmp_path / "repo", {"batches.py": BATCHES, "tests/test_batches.py": BATCH_TEST}
    )
    guard = '    if size <= 0:\n        raise ValueError("size must be positive")\n    return [items[i:i + size] for i in range(0, len(items), size)]\n'
    c = make_comment(
        "_chunk() raises ValueError('range() arg 3 must not be zero') when size is 0; add a guard.\n\n"
        f"```suggestion\n{guard}```", path="batches.py", line=2)  # fmt: skip
    chat = FakeChat(fenced(PRIVATE), fenced(PUBLIC_PASSES), fenced(PUBLIC_PASSES))
    r = run(repo, c, chat)
    assert r.verdict is V.NOT_SHOWN, r.to_dict()
    outcomes = [a["outcome"] for a in r.evidence["reproducer"]["attempts"]]
    assert outcomes == ["rejected_reachability", "passes_on_head", "passes_on_head"]
    assert "_chunk" in r.evidence["reproducer"]["attempts"][0]["reason"]
    # the rejected attempt was fed back to the writer
    assert "reachability rule" in chat.seen[1][-1]["content"].lower()
    assert "evidence, not proof" in r.summary
    assert "not reproduced through public entry points" in r.summary
    assert "NOT_SHOWN" in render_reply(r)


def test_reformat_only_suggestion_has_no_behaviour_change(textrepo):
    chat = FakeChat()  # must not be called
    c = make_comment(
        "Add a clarifying comment.\n\n```suggestion\n    return (text.split()[0])  # first token\n```"
    )
    r = run(textrepo, c, chat)
    assert r.verdict is V.NO_BEHAVIOUR_CHANGE, r.to_dict()
    assert chat.seen == []
    assert "reproducer" not in r.evidence and "suite" not in r.evidence
    assert r.evidence["normalisation"]["files"][0]["method"] == "ast"


def test_suggestion_that_breaks_an_existing_test_is_harmful(textrepo):
    chat = FakeChat(fenced(
        "from textutil import first_word\n\n\ndef test_upper():\n    assert first_word('hello world') == 'HELLO'\n"))  # fmt: skip
    c = make_comment(
        "The word should be upper-cased.\n\n```suggestion\n    return text.split()[0].upper()\n```"
    )
    r = run(textrepo, c, chat)
    assert r.verdict is V.HARMFUL, r.to_dict()
    suite = r.evidence["suite"]
    assert suite["head_passed"] == 2 and suite["suggestion_passed"] == 0
    # the reproducer alone would have said NECESSARY: HARMFUL outranks it
    assert [x["passed"] for x in r.evidence["reproducer"]["suggestion_runs"]] == [True, True]
    assert any(f["verdict"] == "NECESSARY" for f in r.evidence["findings"])
    assert "test_first_word" in suite["suggestion_failure_output_tail"]


def test_prose_only_comment_with_real_bug_is_claim_confirmed(textrepo):
    chat = FakeChat(fenced(EMPTY_REPRO))
    r = run(
        textrepo, make_comment("first_word('   ') raises IndexError; it should return ''."), chat
    )
    assert r.verdict is V.CLAIM_CONFIRMED, r.to_dict()
    assert "no suggestion" in r.summary
    assert "suggestion_runs" not in r.evidence["reproducer"]
    assert r.evidence["suggestion"] is None


def test_suggestion_that_does_not_fix_it_is_claim_confirmed(textrepo):
    chat = FakeChat(fenced(EMPTY_REPRO))
    c = make_comment(
        "Blank input crashes.\n\n```suggestion\n    return text.split()[0].strip()\n```"
    )
    r = run(textrepo, c, chat)
    assert r.verdict is V.CLAIM_CONFIRMED, r.to_dict()
    assert "does not fix it" in r.summary
    assert "does not fix it" in render_reply(r)


def test_suggestion_patch_overrides_the_block(textrepo):
    from conftest import make_patch

    patch = make_patch({"textutil.py": TEXTUTIL}, {"textutil.py": "def first_word(text):\n" + FIX})
    chat = FakeChat(fenced(EMPTY_REPRO))
    r = run(textrepo, make_comment("Blank input crashes."), chat, suggestion_patch=patch)
    assert r.verdict is V.NECESSARY, r.to_dict()
    assert r.evidence["suggestion"]["source"] == "--suggestion-patch"


def test_hand_written_reproducer_skips_the_llm(textrepo):
    chat = FakeChat()
    c = make_comment(f"Blank input crashes.\n```suggestion\n{FIX}```")
    r = run(textrepo, c, chat, reproducer=EMPTY_REPRO)
    assert r.verdict is V.NECESSARY and chat.seen == []
    assert r.evidence["reproducer"]["source"] == "provided"


def test_hand_reproducer_with_private_name_is_rejected_unless_disabled(tmp_path):
    repo = write_tree(
        tmp_path / "repo", {"batches.py": BATCHES, "tests/test_batches.py": BATCH_TEST}
    )
    c = make_comment("_chunk with 0 raises.", path="batches.py", line=2)
    r = run(repo, c, None, reproducer=PRIVATE)
    assert r.verdict is V.NOT_SHOWN
    r = run(repo, c, None, reproducer=PRIVATE, reachability=False)
    assert r.verdict is V.CLAIM_CONFIRMED


def test_flaky_reproducer(textrepo):
    flaky = (
        "import os\nfrom textutil import first_word\n\n\n"
        "def test_toggle():\n"
        "    if os.path.exists('.seen'):\n        return\n"
        "    open('.seen', 'w').close()\n"
        "    assert first_word('   ') == ''\n"
    )
    r = run(textrepo, make_comment("Blank input crashes."), FakeChat(fenced(flaky)))
    assert r.verdict is V.FLAKY, r.to_dict()


def test_llm_unavailable_is_an_error(textrepo):
    from patchproof.llm import LLMError

    def dead(messages):
        raise LLMError("awaiting a reply in /x")

    r = run(textrepo, make_comment("Blank input crashes."), dead)
    assert r.verdict is V.ERROR and "awaiting a reply" in r.summary


def test_wrong_reason_failures_and_missing_blocks_use_up_attempts(textrepo):
    syntax = "def test_x(:\n"
    chat = FakeChat(
        fenced("import nonexistent_mod_zz\n\n\ndef test_x():\n    pass\n"),
        "no code here",
        fenced(syntax),
    )
    r = run(textrepo, make_comment("Blank input crashes."), chat)
    assert r.verdict is V.NOT_SHOWN
    assert [a["outcome"] for a in r.evidence["reproducer"]["attempts"]] == [
        "wrong_reason_failure", "no_code_block", "rejected_reachability"]  # fmt: skip


def test_bad_inputs_are_errors(textrepo):
    r = run(textrepo, make_comment("x\n```suggestion\ny\n```", line=99), FakeChat())
    assert r.verdict is V.ERROR and "outside" in r.summary
    r = run(textrepo / "missing", make_comment("x"), FakeChat())
    assert r.verdict is V.ERROR


def test_non_pytest_command_needs_a_reproducer_cmd(textrepo):
    cfg = ReviewConfig(repo=textrepo, comment=make_comment("crash"), test_cmd="make test",
                       chat=FakeChat(), runs=1)  # fmt: skip
    r = review(cfg)
    assert r.verdict is V.ERROR and "--reproducer-cmd" in r.summary


def test_reproducer_cmd_template(textrepo):
    cfg = ReviewConfig(repo=textrepo, comment=make_comment("Blank input crashes."), runs=1,
                       test_cmd="true", reproducer=EMPTY_REPRO, timeout=60,
                       reproducer_cmd=f"{PYTEST_CMD} {{path}}",
                       reproducer_path="tests/test_mine.py")  # fmt: skip
    r = review(cfg)
    assert r.verdict is V.CLAIM_CONFIRMED, r.to_dict()
    assert r.evidence["reproducer"]["path"] == "tests/test_mine.py"
    assert "tests/test_mine.py" in r.evidence["reproducer"]["cmd"]


def test_persistent_workdir(textrepo, tmp_path):
    chat = FakeChat(fenced(EMPTY_REPRO))
    c = make_comment(f"Blank input crashes.\n```suggestion\n{FIX}```")
    r = run(textrepo, c, chat, persistent_workdir=tmp_path / "pw")
    assert r.verdict is V.NECESSARY, r.to_dict()
    tree = tmp_path / "pw" / "tree"
    assert (tree / "textutil.py").read_text() == TEXTUTIL  # restored
    assert not (tree / "tests" / "test_patchproof_review.py").exists()


# -- CLI ---------------------------------------------------------------------------------------


def _write_comment(tmp_path, body, **kw):
    p = tmp_path / "comment.json"
    p.write_text(json.dumps({"body": body, "path": "textutil.py", "line": 2, "side": "RIGHT",
                             "user": {"login": "bot"}, **kw}))  # fmt: skip
    return p


def test_cli_json_and_exit_code(textrepo, tmp_path, monkeypatch, capsys):
    monkeypatch.setattr("patchproof.review.make_chat", lambda: FakeChat(fenced(EMPTY_REPRO)))
    cj = _write_comment(tmp_path, f"Blank input crashes.\n```suggestion\n{FIX}```")
    code = cli.main(["review", "--repo", str(textrepo), "--comment-json", str(cj), "--test-cmd",
                     PYTEST_CMD, "--runs", "1", "--json"])  # fmt: skip
    data = json.loads(capsys.readouterr().out)
    assert code == 0 and data["verdict"] == "NECESSARY"
    assert set(data) == {"verdict", "summary", "evidence", "hunks", "timings"}


def test_cli_reply_and_card(textrepo, tmp_path, monkeypatch, capsys):
    cj = _write_comment(tmp_path, "```suggestion\n    return (text.split()[0])\n```")
    base = ["review", "--repo", str(textrepo), "--comment-json", str(cj), "--test-cmd", PYTEST_CMD]
    assert cli.main([*base, "--reply"]) == 1
    out = capsys.readouterr().out
    assert out.startswith("**patchproof verdict: NO_BEHAVIOUR_CHANGE**")
    assert cli.main(base) == 1
    assert "NO_BEHAVIOUR_CHANGE" in capsys.readouterr().out


def test_cli_hand_reproducer_and_patch_files(textrepo, tmp_path, capsys):
    cj = _write_comment(tmp_path, "Blank input crashes.")
    rf = tmp_path / "repro.py"
    rf.write_text(EMPTY_REPRO)
    code = cli.main(["review", "--repo", str(textrepo), "--comment-json", str(cj), "--test-cmd",
                     PYTEST_CMD, "--reproducer", str(rf), "--runs", "1", "--json"])  # fmt: skip
    assert code == 0 and json.loads(capsys.readouterr().out)["verdict"] == "CLAIM_CONFIRMED"


def test_cli_usage_errors(textrepo, tmp_path, capsys):
    with pytest.raises(SystemExit):
        cli.main(["review", "--repo", str(textrepo), "--test-cmd", "true"])
    assert cli.main(["review", "--repo", str(textrepo), "--test-cmd", "true",
                     "--github", "o/r#1"]) == 2  # fmt: skip
    bad = tmp_path / "bad.json"
    bad.write_text("not json")
    code = cli.main(["review", "--repo", str(textrepo), "--test-cmd", "true",
                     "--comment-json", str(bad), "--json"])  # fmt: skip
    assert code == 2 and json.loads(capsys.readouterr().out)["verdict"] == "ERROR"


def test_cli_github_fetch_uses_gh_api_read_only(textrepo, monkeypatch, capsys):
    calls = []

    def fake_gh(endpoint):
        calls.append(endpoint)
        if "/pulls/comments/" not in endpoint:
            raise github.GithubError("404")
        return json.dumps({"body": "```suggestion\n    return (text.split()[0])\n```",
                           "path": "textutil.py", "line": 2, "side": "RIGHT",
                           "pull_request_url": "https://api.github.com/repos/o/r/pulls/7"})  # fmt: skip

    monkeypatch.setattr(github, "run_gh", fake_gh)
    code = cli.main(["review", "--repo", str(textrepo), "--test-cmd", PYTEST_CMD, "--github",
                     "o/r#7", "--comment-id", "123", "--json"])  # fmt: skip
    assert calls == ["repos/o/r/pulls/comments/123"]
    assert json.loads(capsys.readouterr().out)["verdict"] == "NO_BEHAVIOUR_CHANGE" and code == 1


def test_github_falls_back_to_issue_comments_and_checks_the_pr(monkeypatch):
    def fake_gh(endpoint):
        if "/pulls/" in endpoint:
            raise github.GithubError("404")
        return json.dumps({"body": "hi", "issue_url": "https://api.github.com/repos/o/r/issues/9"})

    monkeypatch.setattr(github, "run_gh", fake_gh)
    assert github.fetch_comment("o/r#9", 5)["body"] == "hi"
    with pytest.raises(github.GithubError, match="not to PR #3"):
        github.fetch_comment("o/r#3", 5)
    with pytest.raises(github.GithubError):
        github.parse_spec("nonsense")


# -- C (basic: the reproducer is placed at --reproducer-path and the user's command builds it) --

FOO_C = (
    "static int off_by_one(int n)\n{\n    return n;\n}\n\n"
    "int last_index(int n)\n{\n    return off_by_one(n);\n}\n"
)
C_REPRO = (
    "#include <assert.h>\nint last_index(int n);\n\n"
    "int main(void)\n{\n    assert(last_index(3) == 2);\n    return 0;\n}\n"
)
C_PRIVATE = (
    "#include <assert.h>\nint off_by_one(int n);\n\n"
    "int main(void)\n{\n    assert(off_by_one(3) == 2);\n    return 0;\n}\n"
)
C_CMD = "cc -o repro {path} foo.c && ./repro"


@pytest.mark.skipif(shutil.which("cc") is None, reason="no C compiler")
def test_c_reproducer(tmp_path):
    repo = write_tree(tmp_path / "repo", {"foo.c": FOO_C})
    c = make_comment(
        "last_index(3) returns 3, it should be 2.\n\n```suggestion\n    return n - 1;\n```",
        path="foo.c",
        line=3,
    )
    kw = dict(reproducer_path="tests/repro.c", reproducer_cmd=C_CMD)
    r = review(ReviewConfig(repo=repo, comment=c, test_cmd="true", runs=1, timeout=60,
                            reproducer=C_REPRO, **kw))  # fmt: skip
    assert r.verdict is V.NECESSARY, r.to_dict()
    assert "Assertion" in r.evidence["reproducer"]["failure_output_tail"]
    assert "```c" in render_reply(r)
    # a reproducer calling the static helper directly is rejected before it is built
    r = review(ReviewConfig(repo=repo, comment=c, test_cmd="true", runs=1, timeout=60,
                            reproducer=C_PRIVATE, **kw))  # fmt: skip
    assert r.verdict is V.NOT_SHOWN
    assert "static in foo.c" in r.evidence["reproducer"]["attempts"][0]["reason"]
