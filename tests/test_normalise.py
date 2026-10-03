"""Equivalence of a suggestion after normalisation (NO_BEHAVIOUR_CHANGE)."""

from __future__ import annotations

from patchproof.normalise import equivalent, python_fingerprint, token_stream


def test_python_comments_whitespace_parens_and_quotes_are_ignored():
    old = "def f(x):\n    return x+1\n"
    new = "def f(x):\n    # add one\n    return (x + 1)\n"
    assert equivalent("a.py", old, new) == (True, "ast")
    assert equivalent("a.py", "s = 'a'\n", 's = "a"\n')[0]


def test_python_docstrings_are_ignored():
    old = 'def f():\n    """Old."""\n    return 1\n'
    new = 'def f():\n    """A much longer\n    docstring."""\n    return 1\n'
    assert equivalent("a.py", old, new)[0]
    assert equivalent("a.py", 'def f():\n    """Only."""\n', "def f():\n    pass\n")[0]
    assert equivalent("a.py", '"""mod doc"""\nx = 1\n', "x = 1\n")[0]


def test_python_real_changes_are_not_equivalent():
    assert not equivalent("a.py", "x = 1\n", "x = 2\n")[0]
    assert not equivalent("a.py", "return_ = a and b\n", "return_ = a or b\n")[0]
    assert not equivalent("a.py", "x = 'a'\n", "x = 'a '\n")[0]
    # a string statement that is not a docstring is code
    assert not equivalent("a.py", "x = 1\n'note'\n", "x = 1\n")[0]


def test_python_syntax_error_is_never_equivalent():
    assert not equivalent("a.py", "x = 1\n", "x = (1\n")[0]
    assert python_fingerprint("x = (") is None


C_OLD = "int f(int x)\n{\n\treturn x+1; /* inc */\n}\n"


def test_c_token_stream_ignores_comments_and_whitespace():
    new = "int f(int x) {\n    // add one\n    return x + 1;\n}\n"
    assert equivalent("a.c", C_OLD, new) == (True, "tokens")


def test_c_real_changes_are_not_equivalent():
    assert not equivalent("a.c", C_OLD, C_OLD.replace("x+1", "x-1"))[0]
    assert not equivalent("a.c", C_OLD, C_OLD.replace("x+1", "x+1 > 0"))[0]
    assert not equivalent("a.c", 'puts("a b");', 'puts("a  b");')[0]


def test_c_comment_markers_inside_strings_survive():
    assert token_stream('puts("// not a comment");', ".c") == [
        "puts",
        "(",
        '"// not a comment"',
        ")",
        ";",
    ]
    assert not equivalent("a.c", 'puts("// x");', 'puts("");')[0]


def test_hash_comments_for_shell_like_files():
    assert equivalent("a.sh", "echo hi # greet\n", "echo   hi\n")[0]
    assert not equivalent("a.c", "#define A 1\n", "\n")[0]  # preprocessor is code in C


def test_existence_and_identity():
    assert equivalent("a.py", None, None)[0]
    assert not equivalent("a.py", None, "x = 1\n")[0]
    assert equivalent("a.txt", "same", "same") == (True, "identical")
