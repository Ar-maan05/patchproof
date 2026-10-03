"""gen-test with a mocked chat backend; no network is ever touched."""

import json
import urllib.request

import pytest

from patchproof import cli, llm

BUGGY = "def last(xs):\n    return xs[len(xs)]\n"
FIXED = "def last(xs):\n    return xs[-1] if xs else None\n"
GOOD = "from calc import last\n\n\ndef test_repro():\n    assert last([1, 2, 3]) == 3\n"
PASSING_ON_BASE = "from calc import last\n\n\ndef test_repro():\n    assert True\n"


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    def boom(*a, **k):
        raise AssertionError("network access attempted in tests")

    monkeypatch.setattr(urllib.request, "urlopen", boom)


class FakeChat:
    def __init__(self, *replies):
        self.replies = list(replies)
        self.seen = []

    def __call__(self, messages):
        self.seen.append([dict(m) for m in messages])
        return self.replies.pop(0)


def fenced(code):
    return f"Here you go:\n```python\n{code}```\n"


@pytest.fixture
def case(scenario):
    return scenario({"calc.py": BUGGY}, {"calc.py": FIXED})


def test_extract_code():
    assert llm.extract_code("x ```python\na = 1\n``` y") == "a = 1\n"
    assert llm.extract_code("no code at all") is None


def test_new_file_diff_applies_and_parses():
    from patchproof.diff import parse_patch

    fp = parse_patch(llm.new_file_diff("tests/test_x.py", "a = 1\nb = 2\n")).files[0]
    assert fp.is_new and fp.hunks[0].added == [(1, "a = 1"), (2, "b = 2")]


def test_first_attempt_validates(case):
    repo, patch = case
    chat = FakeChat(fenced(GOOD))
    gt = llm.generate_reproducer(repo, patch, "last() crashes", chat=chat, max_attempts=3)
    assert gt.validated and gt.attempts == 1
    assert gt.path == "test_patchproof_repro.py"
    assert "last() crashes" in chat.seen[0][1]["content"]
    assert "xs[len(xs)]" in chat.seen[0][1]["content"]  # pre-patch source is shown


def test_retry_loop_feeds_back_output(case):
    repo, patch = case
    chat = FakeChat(fenced(PASSING_ON_BASE), fenced(GOOD))
    gt = llm.generate_reproducer(repo, patch, chat=chat, max_attempts=3)
    assert gt.validated and gt.attempts == 2
    feedback = chat.seen[1][-1]["content"]
    assert "PASSES on the unpatched code" in feedback
    assert gt.notes and "attempt 1" in gt.notes[0]


def test_wrong_reason_feedback(case):
    repo, patch = case
    bad = "import nonexistent_module_xyz\n\n\ndef test_r():\n    assert True\n"
    chat = FakeChat(fenced(bad), fenced(GOOD))
    gt = llm.generate_reproducer(repo, patch, chat=chat)
    assert gt.validated
    assert "wrong reason" in chat.seen[1][-1]["content"]


def test_gives_up_after_max_attempts(case):
    repo, patch = case
    chat = FakeChat(*[fenced(PASSING_ON_BASE)] * 2)
    gt = llm.generate_reproducer(repo, patch, chat=chat, max_attempts=2)
    assert not gt.validated and gt.attempts == 2


def test_no_usable_reply_raises(case):
    repo, patch = case
    with pytest.raises(llm.LLMError):
        llm.generate_reproducer(repo, patch, chat=FakeChat("sorry", "nope"), max_attempts=2)


def test_check_with_generated_test_end_to_end(case, monkeypatch, tmp_path, capsys):
    repo, patch = case
    pf = tmp_path / "p.patch"
    pf.write_text(patch)
    monkeypatch.setattr(llm, "make_chat", lambda *a, **k: FakeChat(fenced(GOOD)))
    code = cli.main(["check", "--repo", str(repo), "--patch", str(pf), "--gen-test", "--runs", "1",
                     "--json"])  # fmt: skip
    data = json.loads(capsys.readouterr().out)
    assert code == 0 and data["verdict"] == "PROVEN", data
    assert data["evidence"]["generated_test"]["validated"] is True


def test_gen_test_command_writes_file(case, monkeypatch, tmp_path, capsys):
    repo, patch = case
    pf = tmp_path / "p.patch"
    pf.write_text(patch)
    monkeypatch.setattr(llm, "make_chat", lambda *a, **k: FakeChat(fenced(GOOD)))
    out = tmp_path / "repro.py"
    code = cli.main(["gen-test", "--repo", str(repo), "--patch", str(pf), "--out", str(out)])
    assert code == 0 and "def test_repro" in out.read_text()


def test_llm_unreachable_is_error_verdict(case, monkeypatch, tmp_path, capsys):
    import urllib.error

    def refuse(req, timeout=None):
        raise urllib.error.URLError("connection refused")

    monkeypatch.setattr(urllib.request, "urlopen", refuse)
    repo, patch = case
    pf = tmp_path / "p.patch"
    pf.write_text(patch)
    code = cli.main(["check", "--repo", str(repo), "--patch", str(pf), "--gen-test", "--json"])
    data = json.loads(capsys.readouterr().out)
    assert code == 2 and data["verdict"] == "ERROR" and "gen-test failed" in data["summary"]


def test_make_chat_reads_env_and_parses_response(monkeypatch):
    captured = {}

    class Resp:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def read(self):
            return json.dumps({"choices": [{"message": {"content": "hi"}}]}).encode()

    def fake_urlopen(req, timeout=None):
        captured["url"] = req.full_url
        captured["auth"] = req.get_header("Authorization")
        captured["body"] = json.loads(req.data)
        return Resp()

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    monkeypatch.setenv("PATCHPROOF_LLM_BASE_URL", "http://example.invalid/v1/")
    monkeypatch.setenv("PATCHPROOF_LLM_MODEL", "my-model")
    monkeypatch.setenv("PATCHPROOF_LLM_API_KEY", "sekret")
    assert llm.make_chat()([{"role": "user", "content": "x"}]) == "hi"
    assert captured["url"] == "http://example.invalid/v1/chat/completions"
    assert captured["auth"] == "Bearer sekret"
    assert captured["body"]["model"] == "my-model"


def test_make_chat_wraps_network_errors(monkeypatch):
    import urllib.error

    def fail(req, timeout=None):
        raise urllib.error.URLError("refused")

    monkeypatch.setattr(urllib.request, "urlopen", fail)
    with pytest.raises(llm.LLMError):
        llm.make_chat()([{"role": "user", "content": "x"}])


def test_exchange_chat_writes_request_then_reads_reply(tmp_path):
    chat = llm.make_exchange_chat(tmp_path / "x")
    msgs = [{"role": "user", "content": "write tests"}]
    with pytest.raises(llm.LLMError, match="awaiting a reply"):
        chat(msgs)
    key = llm.request_key(msgs)
    req = json.loads((tmp_path / "x" / f"{key}.request.json").read_text())
    assert req == {"messages": msgs}
    (tmp_path / "x" / f"{key}.reply.md").write_text("<think>hmm</think>```python\nx = 1\n```")
    assert chat(msgs) == "```python\nx = 1\n```"


def test_exchange_key_ignores_nothing_but_content(tmp_path):
    a = [{"role": "user", "content": "a"}]
    b = [{"role": "user", "content": "b"}]
    assert llm.request_key(a) == llm.request_key([dict(a[0])])
    assert llm.request_key(a) != llm.request_key(b)


def test_make_chat_uses_exchange_dir_from_env(monkeypatch, tmp_path):
    monkeypatch.setenv("PATCHPROOF_LLM_EXCHANGE_DIR", str(tmp_path))
    chat = llm.make_chat()
    with pytest.raises(llm.LLMError):
        chat([{"role": "user", "content": "hi"}])
    assert len(list(tmp_path.glob("*.request.json"))) == 1
