"""Assertion-strength classifier: many tiny snippets, including the tricky tautologies."""

from __future__ import annotations

import pytest

from patchproof.assertions import STRONG, UNKNOWN, WEAK, classify_source


def grade(body: str, prelude: str = "") -> str:
    lines = body.strip("\n").splitlines()
    src = f"{prelude}\n\ndef test_t():\n" + "\n".join("    " + ln for ln in lines) + "\n"
    return classify_source(src, "test_t").cls


STRONG_CASES = [
    "assert f(1) == 3",
    "assert f(1) != 3",
    "assert f(1) == []",
    "assert f(1) == None",
    "assert f(1) is None",
    "assert f(1) is True",
    "assert f(1) == pytest.approx(0.5)",
    "assert f(1) == g(1)",
    "assert 'k' in f(1)",
    "assert 'k' not in f(1)",
    "assert len(f(1)) == 3",
    "assert len(f(1)) == 0",
    "assert len(f(1)) > 2",
    "assert f(1) < 10",
    "assert is_valid('x')",
    "assert not is_valid('x')",
    "assert mock.called",
    "assert r == 1 and isinstance(r, int)",
    "assert isinstance(r, dict) and r == {'a': 1}",
    "assert r is not None and r == 'x'",
    "assert r == 1 or r == 2",
    "assert not (r == 5)",
    "assert not (r != 5) ",
    "assert r['a'] == 1",
    "with pytest.raises(ValueError):\n    f(1)",
    "with pytest.raises(ValueError, match='bad'):\n    f(1)",
    "with pytest.raises(Exception, match='bad'):\n    f(1)",
    "m.assert_called_once_with(1)",
    "np.testing.assert_array_equal(f(1), [1])",
    "assert_frame_equal(f(1), g(1))",
    "self.assertEqual(f(1), 2)",
    "self.assertIn('a', f(1))",
    "self.assertIsNone(f(1))",
    "self.assertTrue(is_ok(1))",
    "self.assertGreater(f(1), 4)",
    "self.assertRaises(ValueError, f, 1)",
    "check_result(f(1))",
    "for x in [1, 2]:\n    assert f(x) == x",
    "r = f(1)\nassert isinstance(r, int)\nassert r == 1",
    "try:\n    f(1)\nexcept KeyError:\n    pass\nassert f(2) == 2",
]

WEAK_CASES = [
    "f(1)",
    "pass",
    "r = f(1)",
    "assert True",
    "assert 1",
    "assert 'x'",
    "assert 1 == 1",
    "assert 2 + 2 == 4",
    "assert not False",
    "assert r == r",
    "assert f(1) == f(1)",
    "assert r is r",
    "assert r <= r",
    "assert len(r) >= 0",
    "assert 0 <= len(r)",
    "assert len(r) > -1",
    "assert abs(x) >= 0",
    "assert len(r) > 0",
    "assert len(r) >= 1",
    "assert len(r) != 0",
    "assert r != None",
    "assert r != ''",
    "assert r != 0",
    "assert r is not None",
    "assert None is not r",
    "assert not (r is None)",
    "assert r",
    "assert not r",
    "assert r.attr",
    "assert r['a']",
    "assert len(r)",
    "assert str(r)",
    "assert isinstance(r, dict)",
    "assert isinstance(r, (dict, list))",
    "assert type(r) == dict",
    "assert type(r) is dict",
    "assert callable(r)",
    "assert hasattr(r, 'x')",
    "assert r is not None and isinstance(r, dict)",
    "assert r is None or isinstance(r, str)",
    "assert r or True",
    "assert True or r == 1",
    "assert r == 1 or True",
    "assert isinstance(r, str) or r is not None",
    "assert not isinstance(r, int)",
    "with pytest.raises(Exception):\n    f(1)",
    "with pytest.raises(BaseException):\n    f(1)",
    "try:\n    f(1)\nexcept Exception:\n    pass",
    "try:\n    f(1)\n    assert f(1) == 2\nexcept Exception:\n    pass",
    "try:\n    f(1)\nexcept:\n    pass",
    "try:\n    f(1)\nexcept Exception as e:\n    pytest.fail(f'raised {e!r}')",
    "try:\n    f(1)\nexcept Exception:\n    assert False",
    "try:\n    f(1)\nexcept Exception:\n    raise AssertionError('boom')",
    "self.assertTrue(r)",
    "self.assertIsNotNone(r)",
    "self.assertIsInstance(r, str)",
    "self.assertEqual(r, r)",
    "self.assertGreaterEqual(len(r), 0)",
    "m.assert_called_once_with(1)" if False else "pass",
    "with pytest.deprecated_call():\n    f(1)",
]


@pytest.mark.parametrize("body", STRONG_CASES)
def test_strong(body):
    assert grade(body, "import pytest") == STRONG, body


@pytest.mark.parametrize("body", WEAK_CASES)
def test_weak(body):
    assert grade(body, "import pytest") == WEAK, body


def test_reasons_are_reported():
    src = "def test_t():\n    assert len(r) >= 0\n    assert isinstance(r, list)\n"
    s = classify_source(src, "test_t")
    assert s.cls == WEAK
    assert any("tautology" in r for r in s.reasons)
    assert any("type" in r for r in s.reasons)


def test_strong_check_anywhere_wins():
    src = "def test_t():\n    assert r is not None\n    assert r['k'] == 1\n"
    assert classify_source(src, "test_t").cls == STRONG


def test_helper_in_same_module_is_inlined():
    strong = "def verify(r):\n    assert r == 3\n\n\ndef test_t():\n    verify(f())\n"
    weak = "def helper(r):\n    assert isinstance(r, int)\n\n\ndef test_t():\n    helper(f())\n"
    assert classify_source(strong, "test_t").cls == STRONG
    assert classify_source(weak, "test_t").cls == WEAK


def test_method_in_class():
    src = "class TestX:\n    def test_a(self):\n        assert f() == 1\n    def test_b(self):\n        assert r\n"
    assert classify_source(src, "test_a", "TestX").cls == STRONG
    assert classify_source(src, "test_b", "TestX").cls == WEAK


def test_unknown_when_not_found_or_unparseable():
    assert classify_source("def test_a(): pass\n", "test_zzz").cls == UNKNOWN
    assert classify_source("def (:\n", "test_a").cls == UNKNOWN
    assert classify_source("class A: pass\n", "t", "Missing").cls == UNKNOWN
