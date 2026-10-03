import pytest
from conftest import make_patch

from patchproof.diff import DiffError, parse_patch, split_patch
from patchproof.testfiles import is_test_path

BASE = {
    "pkg/a.py": "".join(f"line{i}\n" for i in range(1, 41)),
    "pkg/b.py": "x = 1\n",
}


def two_hunk_patch():
    new_a = BASE["pkg/a.py"].replace("line2\n", "LINE2\n").replace("line35\n", "LINE35\nextra\n")
    return make_patch(BASE, {**BASE, "pkg/a.py": new_a})


def test_parse_two_hunks():
    ps = parse_patch(two_hunk_patch())
    assert len(ps.files) == 1
    fp = ps.files[0]
    assert fp.path == "pkg/a.py" and not fp.is_new and not fp.is_delete
    assert len(fp.hunks) == 2
    h0, h1 = fp.hunks
    assert h0.removed == [(2, "line2")] and h0.added == [(2, "LINE2")]
    assert h1.added == [(35, "LINE35"), (36, "extra")]
    assert h1.new_start == h1.old_start + 0  # first hunk is length-neutral


def test_new_and_deleted_files():
    patch = make_patch({"old.py": "a\nb\n"}, {"new.py": "c\n"})
    ps = parse_patch(patch)
    by_path = {f.path: f for f in ps.files}
    assert by_path["new.py"].is_new
    assert by_path["old.py"].is_delete
    assert by_path["new.py"].hunks[0].added == [(1, "c")]


def test_render_roundtrip_and_subset_renumbers():
    patch = two_hunk_patch()
    ps = parse_patch(patch)
    assert ps.render() == patch
    only_second = ps.render({(0, 1)})
    assert "LINE2" not in only_second and "LINE35" in only_second
    reparsed = parse_patch(only_second).files[0].hunks[0]
    assert reparsed.new_start == reparsed.old_start  # the skipped hunk was length-neutral
    only_first_then_second = ps.files[0].render({0, 1})
    assert only_first_then_second == patch


def test_subset_shift_when_skipped_hunk_changes_length():
    old = {"f.py": "".join(f"l{i}\n" for i in range(1, 41))}
    new_text = old["f.py"].replace("l3\n", "l3\nnew1\nnew2\n").replace("l35\n", "L35\n")
    ps = parse_patch(make_patch(old, {"f.py": new_text}))
    second = ps.files[0].render({1})
    h = parse_patch(second).files[0].hunks[0]
    assert h.new_start == h.old_start


def test_plain_unified_diff_without_git_header():
    text = "--- a/x.py\n+++ b/x.py\n@@ -1,2 +1,2 @@\n a\n-b\n+c\n"
    ps = parse_patch(text)
    assert ps.files[0].path == "x.py"
    assert ps.files[0].hunks[0].added == [(2, "c")]


def test_header_only_file_is_kept():
    text = "diff --git a/a.py b/b.py\nsimilarity index 100%\nrename from a.py\nrename to b.py\n"
    fp = parse_patch(text).files[0]
    assert (fp.old_path, fp.new_path) == ("a.py", "b.py")
    assert not fp.hunks
    assert parse_patch(text).render() == text


def test_corrupt_hunk_raises():
    with pytest.raises(DiffError):
        parse_patch("--- a/x\n+++ b/x\n@@ -1,3 +1,3 @@\n a\n")


def test_split_patch_tests_vs_code():
    patch = make_patch(
        {}, {"src/mod.py": "x=1\n", "tests/test_mod.py": "y=1\n", "docs/readme.md": "hi\n"}
    )
    tests, code = split_patch(parse_patch(patch), is_test_path)
    assert [f.path for f in tests] == ["tests/test_mod.py"]
    assert sorted(f.path for f in code) == ["docs/readme.md", "src/mod.py"]


def test_no_newline_marker_preserved():
    text = "--- a/x\n+++ b/x\n@@ -1 +1 @@\n-a\n\\ No newline at end of file\n+b\n\\ No newline at end of file\n"
    ps = parse_patch(text)
    assert ps.render().endswith("\\ No newline at end of file\n")
    assert ps.files[0].hunks[0].added == [(1, "b")]
