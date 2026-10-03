"""Parsing GitHub suggestion blocks into a claim and a unified diff."""

from __future__ import annotations

import json

import pytest

from patchproof.diff import parse_patch
from patchproof.suggestion import (
    Comment,
    SuggestionError,
    build_patch,
    parse_body,
    suggestion_from_comment,
)
from patchproof.workspace import apply_patch

SRC = "def f(x):\n    a = 1\n    b = 2\n    return x + a + b\n\n\ndef g():\n    pass\n"


def comment(body, **kw):
    base = {"body": body, "path": "m.py", "line": 2, "side": "RIGHT"}
    return Comment.from_json({**base, **kw})


@pytest.fixture
def repo(tmp_path):
    (tmp_path / "m.py").write_text(SRC)
    return tmp_path


def apply(repo, patch):
    apply_patch(repo, patch)
    return (repo / "m.py").read_text()


def test_single_line_block(repo):
    c = comment("Use a constant.\n\n```suggestion\n    a = 10\n```\n")
    claim, s = suggestion_from_comment(c, repo)
    assert claim == "Use a constant."
    assert (s.start, s.end, s.lines) == (2, 2, ["    a = 10"])
    assert apply(repo, s.patch) == SRC.replace("    a = 1\n", "    a = 10\n")
    assert s.new_text == SRC.replace("    a = 1\n", "    a = 10\n")


def test_multi_line_range_and_longer_replacement(repo):
    c = comment("```suggestion\n    a, b = 1, 2\n    c = 3\n    d = 4\n```", start_line=2, line=3)
    _, s = suggestion_from_comment(c, repo)
    assert (s.start, s.end) == (2, 3)
    out = apply(repo, s.patch)
    assert out.splitlines()[1:4] == ["    a, b = 1, 2", "    c = 3", "    d = 4"]
    assert out.splitlines()[4] == "    return x + a + b"


def test_empty_suggestion_deletes_the_lines(repo):
    c = comment("Drop this.\n```suggestion\n```", start_line=2, line=3)
    claim, s = suggestion_from_comment(c, repo)
    assert claim == "Drop this."
    assert s.lines == []
    assert apply(repo, s.patch) == "def f(x):\n    return x + a + b\n\n\ndef g():\n    pass\n"


def test_block_with_one_blank_line_is_not_a_deletion(repo):
    claim, blocks = parse_body("x\n```suggestion\n\n```")
    assert blocks == [[""]]
    c = comment("```suggestion\n\n```", line=2)
    _, s = suggestion_from_comment(c, repo)
    assert apply(repo, s.patch) == SRC.replace("    a = 1\n", "\n")


def test_crlf_comment_body(repo):
    body = "Typo.\r\n\r\n```suggestion\r\n    a = 7\r\n    a2 = 8\r\n```\r\nthanks\r\n"
    claim, blocks = parse_body(body)
    assert blocks == [["    a = 7", "    a2 = 8"]]
    assert "\r" not in claim and claim == "Typo.\n\nthanks"
    _, s = suggestion_from_comment(comment(body), repo)
    assert apply(repo, s.patch).splitlines()[1:3] == ["    a = 7", "    a2 = 8"]


def test_crlf_source_file_keeps_crlf(tmp_path):
    (tmp_path / "m.py").write_bytes(SRC.replace("\n", "\r\n").encode())
    c = comment("```suggestion\n    a = 5\n    z = 6\n```")
    _, s = suggestion_from_comment(c, tmp_path)
    out = tmp_path / "m.py"
    out.write_bytes(s.new_text.encode())
    assert b"    a = 5\r\n    z = 6\r\n" in out.read_bytes()
    assert b"\r\n" in s.patch.encode() and "    a = 5\r\n" in s.patch
    # the patch applies byte-exactly to the CRLF file
    (tmp_path / "m.py").write_bytes(SRC.replace("\n", "\r\n").encode())
    apply_patch(tmp_path, s.patch)
    assert (tmp_path / "m.py").read_bytes() == s.new_text.encode()


def test_last_line_without_trailing_newline(tmp_path):
    (tmp_path / "m.py").write_text("a = 1\nb = 2")
    c = comment("```suggestion\nb = 3\n```", line=2)
    _, s = suggestion_from_comment(c, tmp_path)
    assert "\\ No newline at end of file" in s.patch
    apply_patch(tmp_path, s.patch)
    assert (tmp_path / "m.py").read_text() == "a = 1\nb = 3"


def test_first_line_replacement(repo):
    c = comment("```suggestion\ndef f(x, y=0):\n```", line=1)
    _, s = suggestion_from_comment(c, repo)
    assert apply(repo, s.patch).startswith("def f(x, y=0):\n    a = 1\n")


def test_patch_parses_with_our_diff_parser(repo):
    _, s = suggestion_from_comment(comment("```suggestion\n    a = 9\n```"), repo)
    ps = parse_patch(s.patch)
    assert [f.path for f in ps.files] == ["m.py"]
    assert len(ps.files[0].hunks) == 1


def test_four_backtick_fence_and_info_string(repo):
    body = "````suggestion\n    a = '```'\n````\nok"
    claim, blocks = parse_body(body)
    assert blocks == [["    a = '```'"]] and claim == "ok"


def test_ordinary_code_fences_are_part_of_the_claim():
    claim, blocks = parse_body("Run:\n```python\nf(1)\n```\nand it crashes.")
    assert blocks == []
    assert "f(1)" in claim


def test_identical_duplicate_blocks_collapse(repo):
    body = "```suggestion\n    a = 2\n```\nor\n```suggestion\n    a = 2\n```"
    claim, s = suggestion_from_comment(comment(body), repo)
    assert s.blocks == 2 and claim == "or"


def test_different_blocks_for_one_range_are_an_error(repo):
    body = "```suggestion\n    a = 2\n```\nor\n```suggestion\n    a = 3\n```"
    with pytest.raises(SuggestionError, match="ambiguous"):
        suggestion_from_comment(comment(body), repo)


def test_prose_only_comment_has_no_suggestion(repo):
    claim, s = suggestion_from_comment(comment("This overflows when x is huge."), repo)
    assert s is None and claim == "This overflows when x is huge."


def test_issue_level_comment_has_no_anchor(repo):
    c = Comment.from_json({"body": "Overall this looks risky.", "html_url": "u"})
    assert c.line_range() is None and c.path is None
    assert suggestion_from_comment(c, repo)[1] is None


def test_issue_level_comment_cannot_carry_a_suggestion(repo):
    c = Comment.from_json({"body": "```suggestion\nx\n```"})
    with pytest.raises(SuggestionError, match="anchored"):
        suggestion_from_comment(c, repo)


def test_left_side_suggestion_is_rejected(repo):
    with pytest.raises(SuggestionError, match="RIGHT"):
        suggestion_from_comment(comment("```suggestion\nx\n```", side="LEFT"), repo)


def test_outdated_comment_falls_back_to_original_line(repo):
    c = comment("```suggestion\n    a = 4\n```", line=None, original_line=2)
    _, s = suggestion_from_comment(c, repo)
    assert (s.start, s.end) == (2, 2) and s.notes


def test_range_outside_the_file(repo):
    with pytest.raises(SuggestionError, match="outside"):
        suggestion_from_comment(comment("```suggestion\nx\n```", line=99), repo)


def test_missing_file_and_unsafe_path(repo):
    with pytest.raises(SuggestionError, match="cannot read"):
        suggestion_from_comment(comment("```suggestion\nx\n```", path="nope.py"), repo)
    with pytest.raises(SuggestionError, match="unsafe"):
        suggestion_from_comment(comment("```suggestion\nx\n```", path="../etc/passwd"), repo)


def test_from_json_reads_only_the_documented_fields():
    real = {
        "url": "https://api.github.com/repos/o/r/pulls/comments/1",
        "id": 1,
        "body": "b",
        "path": "src/a.c",
        "line": 12,
        "start_line": 10,
        "original_line": 11,
        "side": "RIGHT",
        "commit_id": "abc",
        "diff_hunk": "@@ -1 +1 @@\n-a\n+b",
        "html_url": "https://github.com/o/r/pull/3#discussion_r1",
        "user": {"login": "claude[bot]", "id": 5},
        "reactions": {"total_count": 0},
        "start_side": "RIGHT",
    }
    c = Comment.from_json(json.loads(json.dumps(real)))
    assert (c.user, c.path, c.line_range(), c.commit_id) == (
        "claude[bot]",
        "src/a.c",
        (10, 12),
        "abc",
    )
    assert c.diff_hunk.startswith("@@")


def test_from_json_requires_a_body():
    with pytest.raises(SuggestionError):
        Comment.from_json({"path": "x"})
    with pytest.raises(SuggestionError):
        Comment.from_json([])


def test_build_patch_rejects_bad_ranges():
    with pytest.raises(SuggestionError):
        build_patch("m.py", SRC, 0, 1, [])
    with pytest.raises(SuggestionError):
        build_patch("m.py", SRC, 3, 2, [])
