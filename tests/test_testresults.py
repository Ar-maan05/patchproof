"""Per-test outcome parsing and discrimination."""

from __future__ import annotations

from patchproof import testresults as tr

JUNIT_BASE = """<?xml version="1.0"?><testsuites><testsuite>
<testcase classname="tests.test_a" name="test_flip"><failure type="AssertionError" message="assert 1 == 2">tb</failure></testcase>
<testcase classname="tests.test_a" name="test_ok"/>
<testcase classname="tests.test_a" name="test_still_bad"><failure message="x">tb</failure></testcase>
<testcase classname="tests.test_a.TestK" name="test_m[1]"><error message="boom">tb</error></testcase>
<testcase classname="tests.test_a" name="test_skip"><skipped message="s"/></testcase>
</testsuite></testsuites>"""
JUNIT_HEAD = """<testsuites><testsuite>
<testcase classname="tests.test_a" name="test_flip"/>
<testcase classname="tests.test_a" name="test_ok"/>
<testcase classname="tests.test_a" name="test_still_bad"><failure message="x">tb</failure></testcase>
<testcase classname="tests.test_a.TestK" name="test_m[1]"/>
<testcase classname="tests.test_a" name="test_skip"><skipped message="s"/></testcase>
</testsuite></testsuites>"""


def test_parse_junit_statuses():
    out = tr.parse_junit(JUNIT_BASE)
    assert out["tests.test_a::test_flip"].status == "failed"
    assert out["tests.test_a::test_flip"].err_type == "AssertionError"
    assert out["tests.test_a::test_ok"].status == "passed"
    assert out["tests.test_a.TestK::test_m[1]"].status == "error"
    assert out["tests.test_a::test_skip"].status == "skipped"


def test_parse_junit_garbage():
    assert tr.parse_junit("not xml") == {}
    assert tr.parse_junit("") == {}


def test_discriminating_is_fail_to_pass_only():
    disc = tr.discriminating(tr.parse_junit(JUNIT_BASE), tr.parse_junit(JUNIT_HEAD))
    assert [o.key for o in disc] == ["tests.test_a.TestK::test_m[1]", "tests.test_a::test_flip"]


def test_discriminating_ignores_tests_missing_from_head():
    base = tr.parse_junit(JUNIT_BASE)
    assert tr.discriminating(base, {}) == []


def test_with_junit_rewrites_pytest_commands():
    out = tr.with_junit("python -m pytest -x -q tests/t.py", "/tmp/j.xml")  # type: ignore[arg-type]
    assert out is not None and "--junitxml=/tmp/j.xml" in out and " -x" not in out
    out = tr.with_junit("pytest --maxfail=1 --junitxml=old.xml tests", "/tmp/j.xml")  # type: ignore[arg-type]
    assert out is not None and "maxfail" not in out and "old.xml" not in out
    assert tr.with_junit("make test", "/tmp/j.xml") is None  # type: ignore[arg-type]
    assert tr.with_junit("pytest && echo hi", "/tmp/j.xml") is None  # type: ignore[arg-type]


def test_pytest_prefix():
    assert tr.pytest_prefix("/usr/bin/python3 -m pytest -q tests") == [
        "/usr/bin/python3",
        "-m",
        "pytest",
    ]
    assert tr.pytest_prefix("pytest tests") == ["pytest"]
    assert tr.pytest_prefix("go test ./...") is None


def test_locate_and_grade(tmp_path):
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_a.py").write_text(
        "def test_flip():\n    assert isinstance(f(), dict)\n\n\n"
        "class TestK:\n    def test_m(self):\n        assert f() == 1\n"
    )
    disc = tr.discriminating(tr.parse_junit(JUNIT_BASE), tr.parse_junit(JUNIT_HEAD))
    graded = tr.grade_tests(tmp_path, disc)
    assert graded["tests/test_a.py::test_flip"].cls == "WEAK"
    assert graded["tests/test_a.py::TestK::test_m"].cls == "STRONG"
    assert not tr.all_weak(graded)
    assert tr.all_weak({"a": graded["tests/test_a.py::test_flip"]})
    assert not tr.all_weak({})


def test_unlocatable_test_is_unknown_not_weak(tmp_path):
    disc = tr.discriminating(tr.parse_junit(JUNIT_BASE), tr.parse_junit(JUNIT_HEAD))
    graded = tr.grade_tests(tmp_path, disc)
    assert {s.cls for s in graded.values()} == {"UNKNOWN"}
    assert not tr.all_weak(graded)
