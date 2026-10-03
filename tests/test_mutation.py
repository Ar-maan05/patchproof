import ast

from patchproof.mutation import c_like_mutants, python_mutants, sample


def muts(src, executed=None):
    n = len(src.splitlines())
    return python_mutants(src, "m.py", set(range(1, n + 1)), executed)


def sources(ms, operator=None):
    return [m.source for m in ms if operator is None or m.operator == operator]


def test_compare_flips():
    src = "def f(a, b):\n    return a < b\n"
    out = sources(muts(src), "compare")
    assert "    return a <= b\n" in out[0]


def test_all_comparison_operators():
    for a, b in [("<", "<="), ("<=", "<"), (">", ">="), (">=", ">"), ("==", "!="), ("!=", "==")]:
        src = f"def f(a, b):\n    return a {a} b\n"
        out = sources(muts(src), "compare")
        assert any(f"a {b} b" in s for s in out), (a, b)


def test_negate_if_while_ifexp():
    src = "def f(x):\n    if x:\n        return 1\n    while x:\n        x -= 1\n    return 2 if x else 3\n"
    out = sources(muts(src), "negate")
    assert any("if not (x):" in s for s in out)
    assert any("while not (x):" in s for s in out)
    assert any("2 if not (x) else 3" in s for s in out)


def test_off_by_one_constants():
    out = sources(muts("x = 5\n"), "const")
    assert sorted(out) == ["x = 4\n", "x = 6\n"]


def test_negative_constant_parenthesised():
    out = sources(muts("x = 0\n"), "const")
    assert "x = (-1)\n" in out


def test_booleans_not_treated_as_ints():
    ms = muts("flag = True\n")
    assert [m.operator for m in ms if m.operator == "const"] == []
    assert "flag = False\n" in sources(ms, "bool")


def test_and_or_swap():
    out = sources(muts("def f(a, b, c):\n    return a and b and c\n"), "boolop")
    assert out == ["def f(a, b, c):\n    return a or b or c\n"]
    out = sources(muts("z = a or b\n"), "boolop")
    assert out == ["z = a and b\n"]


def test_delete_statement_becomes_pass():
    out = sources(muts("def f(x):\n    x.append(1)\n    raise ValueError('no')\n"), "delete")
    assert any("    pass\n    raise" in s for s in out)
    assert any("append(1)\n    pass\n" in s for s in out)


def test_docstrings_are_not_deleted():
    ms = muts('def f():\n    """doc"""\n    return 1\n')
    assert all(m.operator != "delete" or '"""doc"""' in m.source for m in ms)


def test_return_none():
    out = sources(muts("def f():\n    return 1 + 2\n"), "return_none")
    assert out == ["def f():\n    return None\n"]
    assert sources(muts("def f():\n    return None\n"), "return_none") == []


def test_only_changed_lines_are_mutated():
    src = "a = 1\nb = 2\nc = 3\n"
    ms = python_mutants(src, "m.py", {2})
    assert ms and all(m.line == 2 for m in ms)


def test_executed_filter_skips_unexecuted_statements():
    src = "a = 1\nb = 2\n"
    ms = python_mutants(src, "m.py", {1, 2}, executed_stmt_lines={2})
    assert ms and all(m.line == 2 for m in ms)


def test_every_mutant_is_valid_python_and_differs():
    src = "def f(a, b):\n    if a < b and b != 3:\n        return a + 1\n    return None\n"
    ms = muts(src)
    assert ms
    for m in ms:
        ast.parse(m.source)
        assert m.source != src


def test_unicode_offsets_are_handled():
    src = 's = "héllo"; n = 3\n'
    out = sources(muts(src), "const")
    assert 's = "héllo"; n = 4\n' in out


def test_syntax_error_source_yields_nothing():
    assert python_mutants("def (:\n", "m.py", {1}) == []


def test_c_like_operators():
    src = "int f(int a, int b) {\n  if (a < b && a == 3) return a + 1;\n  return 0;\n}\n"
    ms = c_like_mutants(src, "a.c", {2})
    descs = {m.description for m in ms}
    assert "`<` -> `<=`" in descs
    assert "`&&` -> `||`" in descs
    assert "`==` -> `!=`" in descs
    assert any("a - 1" in m.source for m in ms)
    assert all(m.line == 2 for m in ms)


def test_c_like_ignores_comments_strings_and_shifts():
    src = '#include <stdio.h>\n// a < b\nx = y << 2; s = "a == b";\n'
    assert c_like_mutants(src, "a.c", {1, 2, 3}) == []


def test_sample_is_deterministic_and_bounded():
    ms = muts("\n".join(f"v{i} = {i}" for i in range(30)) + "\n")
    s1, s2 = sample(ms, 10), sample(ms, 10)
    assert len(s1) == 10 and [m.source for m in s1] == [m.source for m in s2]
    assert sample(ms, 0) == ms
