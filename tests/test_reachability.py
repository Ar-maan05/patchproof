"""The static reachability check for reproducers."""

from __future__ import annotations

from patchproof.reachability import (
    check_c,
    check_python,
    module_names,
    static_functions,
)

PY_SRC = """\
_CACHE = {}


def _helper(n):
    return n


class Box:
    def _inner(self):
        return 1

    def run(self):
        return self._inner()


def public(x):
    return _helper(x)
"""


def py(code):
    return check_python(code, "src/pkg/mod.py", PY_SRC)


def test_module_names():
    assert module_names("src/pkg/mod.py") == {"src.pkg.mod", "pkg.mod", "mod"}
    assert module_names("pkg/__init__.py") == {"pkg"}


def test_public_use_is_fine():
    assert py("from pkg.mod import public\n\n\ndef test_a():\n    assert public(1) == 1\n") == []


def test_import_private_name():
    v = py("from pkg.mod import _helper\n")
    assert v and "_helper" in v[0]


def test_import_private_name_via_other_module_spelling():
    assert py("from src.pkg.mod import _helper as h\n")
    assert py("from mod import _CACHE\n")


def test_private_attribute_of_imported_module():
    assert py("import pkg.mod\n\n\ndef test_a():\n    pkg.mod._helper(0)\n")
    assert py("import pkg.mod as m\n\n\ndef test_a():\n    m._helper(0)\n")
    assert py("from pkg import mod\n\n\ndef test_a():\n    mod._helper(0)\n")


def test_dunder_names_are_not_private():
    assert py("from pkg.mod import __version__\nimport pkg.mod as m\nm.__name__\n") == []


def test_private_method_defined_by_the_reviewed_module():
    v = py("from pkg.mod import Box\n\n\ndef test_a():\n    assert Box()._inner() == 1\n")
    assert v and "_inner" in v[0]


def test_private_names_of_unrelated_modules_are_fine():
    assert py("import other\n\n\ndef test_a():\n    other._thing()\n") == []


def test_the_test_may_use_its_own_private_helpers():
    code = "def _inner():\n    return 1\n\n\ndef test_a():\n    assert _inner() == 1\n"
    assert py(code) == []


def test_self_private_in_test_classes_is_fine():
    code = "class TestA:\n    def test_a(self):\n        assert self._inner() == 1\n"
    assert py(code) == []


def test_getattr_private():
    assert py("import pkg.mod as m\ngetattr(m, '_helper')(1)\n")


def test_monkeypatch_setattr_object_and_string_forms():
    assert py(
        "import pkg.mod as m\n\n\ndef test_a(monkeypatch):\n    monkeypatch.setattr(m, 'public', len)\n"
    )
    assert py("def test_a(monkeypatch):\n    monkeypatch.setattr('pkg.mod.public', len)\n")
    assert py(
        "from pkg.mod import Box\n\n\ndef test_a(monkeypatch):\n    monkeypatch.setattr(Box, 'run', len)\n"
    )
    assert py(
        "import pkg.mod as m\n\n\ndef test_a(monkeypatch):\n    monkeypatch.setattr(m.Box, 'run', len)\n"
    )


def test_mock_patch_forms():
    assert py(
        "from unittest import mock\n\n\ndef test_a():\n    with mock.patch('pkg.mod._helper'):\n        pass\n"
    )
    assert py(
        "from unittest.mock import patch\n\n\ndef test_a():\n    with patch('pkg.mod.public'):\n        pass\n"
    )
    assert py(
        "from unittest.mock import patch\nimport pkg.mod as m\n\n\ndef test_a():\n    with patch.object(m, 'public'):\n        pass\n"
    )


def test_patching_something_else_is_fine():
    assert py("def test_a(monkeypatch):\n    monkeypatch.setattr('os.getcwd', lambda: '/')\n") == []
    assert py("def test_a(monkeypatch):\n    monkeypatch.setenv('A', '1')\n") == []


def test_assignment_into_the_module():
    assert py("import pkg.mod as m\nm.public = len\n")


def test_unparseable_reproducer():
    assert "does not parse" in py("def (:\n")[0]


C_SRC = """\
#include <stdio.h>

static int helper(int x)
{
        return x;
}

static const char *
name_of(int x)
{
        return "x";
}

static int table[] = { 1, 2, 3 };

/* static int commented_out(void) */
int public_fn(int x)
{
        return helper(x);
}
"""


def test_static_functions_regex():
    assert static_functions(C_SRC) == {"helper", "name_of"}


def test_c_public_call_is_fine():
    code = '#include "foo.h"\nint main(void) { return public_fn(3) != 3; }\n'
    assert check_c(code, "src/foo.c", C_SRC) == []


def test_c_static_call_is_rejected():
    code = "int main(void) { return helper(3) != 3; }\n"
    v = check_c(code, "src/foo.c", C_SRC)
    assert v and "helper" in v[0]
    assert check_c("int main(void){ return !name_of(1); }", "src/foo.c", C_SRC)


def test_c_include_of_the_c_file_is_rejected():
    v = check_c('#include "foo.c"\nint main(void){return 0;}\n', "src/foo.c", C_SRC)
    assert v and "#includes foo.c" in v[0]


def test_c_calls_in_comments_and_strings_are_ignored():
    code = 'int main(void) { /* helper(1) */ puts("helper(2)"); return 0; }\n'
    assert check_c(code, "src/foo.c", C_SRC) == []


def test_c_reproducer_defining_its_own_helper_is_fine():
    code = "static int helper(int x) { return x; }\nint main(void){ return helper(0); }\n"
    assert check_c(code, "src/foo.c", C_SRC) == []
