import pytest

from patchproof.classify import added_symbols, classify_failure

ASSERT_OUT = """\
    def test_x():
>       assert f() == 3
E       assert 2 == 3
FAILED tests/test_x.py::test_x - assert 2 == 3
"""


def test_behavioural_assertion():
    c = classify_failure(ASSERT_OUT)
    assert c.kind == "behavioural" and c.behavioural


def test_syntax_error():
    c = classify_failure('  File "x.py", line 3\n    def (\nSyntaxError: invalid syntax\n')
    assert c.kind == "syntax" and not c.behavioural


@pytest.mark.parametrize(
    "out",
    [
        "E   ModuleNotFoundError: No module named 'foo'",
        "ImportError: cannot import name 'bar' from 'pkg'",
    ],
)
def test_import_errors(out):
    assert classify_failure(out).kind == "import"


def test_collection_error():
    out = "ERROR collecting tests/test_a.py\n!!! Interrupted: 1 error during collection !!!"
    assert classify_failure(out).kind == "collection"


def test_no_tests_ran():
    assert classify_failure("no tests ran in 0.01s", returncode=5).kind == "collection"


def test_name_error_only_wrong_when_symbol_added_by_patch():
    out = "E   NameError: name 'new_helper' is not defined\nFAILED t.py::t - NameError"
    assert classify_failure(out, added_symbols={"new_helper"}).kind == "missing_symbol"
    assert classify_failure(out, added_symbols={"other"}).behavioural


def test_attribute_error_on_added_symbol():
    out = "AttributeError: module 'calc' has no attribute 'shiny'"
    assert classify_failure(out, added_symbols={"shiny"}).kind == "missing_symbol"
    assert classify_failure(out, added_symbols=set()).behavioural


def test_compile_error():
    out = "src/a.c:10:5: error: 'x' undeclared\nmake: *** [Makefile:3: all] Error 1"
    assert classify_failure(out).kind == "compile"


def test_timeout_and_command_not_found():
    assert classify_failure("", timed_out=True).kind == "timeout"
    assert classify_failure("sh: foo: command not found", returncode=127).kind == "command"


def test_unknown_failure_is_treated_as_behavioural():
    c = classify_failure("something exploded", returncode=3)
    assert c.kind == "unknown" and c.behavioural


def test_added_symbols():
    syms = added_symbols(["def foo(x):", "    y = 3", "class Bar:", "BAZ = 4", "async def qux():"])
    assert syms == {"foo", "Bar", "BAZ", "qux"}


ASAN = (
    "==41654==ERROR: AddressSanitizer: global-buffer-overflow on address 0x00000054f1c7\n"
    "READ of size 1 at 0x00000054f1c7 thread T0\n"
)
NINJA_COMPILE = (
    "[3/10] Compiling C object src/basic/libbasic.a.p/strv.c.o\n"
    "FAILED: [code=1] src/basic/libbasic.a.p/strv.c.o\n"
    "../src/basic/strv.c:1275:20: error: use of undeclared identifier 'q'\n"
    "ninja: build stopped: subcommand failed.\n"
)


def test_c_asan_report_is_behavioural():
    c = classify_failure(ASAN, returncode=1)
    assert c.kind == "behavioural" and c.behavioural


def test_c_assertion_failure_is_behavioural():
    out = "Assertion 'strv_equal(l, b)' failed at src/test/test-strv.c:1252, function x(). Aborting.\n"
    assert classify_failure(out, returncode=1).behavioural
    assert classify_failure("prog: t.c:5: main: Assertion `x == 1' failed.\n", 1).kind == (
        "behavioural"
    )


@pytest.mark.parametrize("rc", [-6, -11, 134, 139])
def test_c_crash_signals_are_behavioural(rc):
    c = classify_failure("", returncode=rc)
    assert c.kind == "behavioural"
    assert "SIG" in c.reason


def test_c_runtime_report_beats_ninja_failed_noise():
    out = "FAILED: test-strv\n" + ASAN
    assert classify_failure(out, 1).kind == "behavioural"


def test_c_compile_and_link_errors_are_not_behavioural():
    assert classify_failure(NINJA_COMPILE, returncode=1).kind == "compile"
    link = "ld.lld: error: undefined symbol: foo\nclang: error: linker command failed\n"
    assert classify_failure(link, 1).kind == "compile"
    assert classify_failure("/usr/bin/ld: x.o: undefined reference to `foo'\n", 1).kind == "compile"
    # a crash exit code with a compile error in the output is still a build problem
    assert classify_failure(NINJA_COMPILE, returncode=139).kind == "compile"


def test_ninja_failed_line_alone_is_not_a_test_failure():
    out = "FAILED: [code=1] src/foo.o\nclang: error: no such file\n"
    assert classify_failure(out, 1).kind == "compile"


def test_tail_shows_sanitizer_report_from_its_error_line():
    from patchproof.runner import tail

    out = "noise\n" * 800 + ASAN + "  frame\n" * 50 + "shadow legend\n" * 400
    t = tail(out, 600)
    assert t.startswith("==41654==ERROR: AddressSanitizer")
    assert tail("short", 600) == "short"
    assert tail("x" * 700 + "end", 600).endswith("end")
