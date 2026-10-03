import pytest

from patchproof.testfiles import is_test_path


@pytest.mark.parametrize(
    "path",
    [
        "test_foo.py",
        "pkg/test_foo.py",
        "pkg/foo_test.py",
        "tests/anything.py",
        "src/test/test-strv.c",
        "test/helper.sh",
        "foo/bar_test.go",
        "web/app.test.ts",
        "web/app.spec.js",
        "conftest.py",
        "src/__tests__/x.js",
        "./tests/x.py",
        "src/main/FooTest.java",
        "tests/testdata/input.json",
    ],
)
def test_detected_as_test(path):
    assert is_test_path(path)


@pytest.mark.parametrize(
    "path",
    [
        "src/foo.py",
        "pkg/contest.py",
        "src/latest.c",
        "README.md",
        "lib/attestation.go",
        "src/protest/x.py",
    ],
)
def test_not_a_test(path):
    assert not is_test_path(path)
