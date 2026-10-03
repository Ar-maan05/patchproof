"""Decide whether a failing test run failed for a behavioural reason or a trivial one."""

from __future__ import annotations

import re
from dataclasses import dataclass

# kind -> human reason. Everything but "behavioural"/"unknown" is a wrong reason.
WRONG_KINDS = {
    "syntax": "SyntaxError/IndentationError: the code does not even parse",
    "import": "ImportError/ModuleNotFoundError: the test cannot import what it needs",
    "collection": "test collection/discovery failed, no test actually ran",
    "missing_symbol": "the test references a symbol that only the patch adds",
    "compile": "build/compile error, the tests never ran",
    "command": "the test command could not be run",
    "timeout": "the test run timed out",
}


@dataclass
class FailureClass:
    kind: str
    reason: str
    detail: str = ""

    @property
    def behavioural(self) -> bool:
        return self.kind in ("behavioural", "unknown")

    def to_dict(self) -> dict:
        return {
            "kind": self.kind,
            "behavioural": self.behavioural,
            "reason": self.reason,
            "detail": self.detail,
        }


_SYNTAX = re.compile(r"^\s*(?:E\s+)?(SyntaxError|IndentationError|TabError)\b", re.M)
_IMPORT = re.compile(r"^\s*(?:E\s+)?(ModuleNotFoundError|ImportError)\b(.*)$", re.M)
_COLLECTION = re.compile(
    r"(ERROR collecting |errors? during collection|Interrupted: \d+ errors?|"
    r"ERROR: file or directory not found|ERROR: not found:|no tests ran|collected 0 items|"
    r"Ran 0 tests|^ImportError: Failed to import test module|^_ERROR collecting)",
    re.M,
)
_NAME = re.compile(r"NameError: name '([\w.]+)' is not defined")
_ATTR = re.compile(r"AttributeError: .*? has no attribute '(\w+)'")
_CANNOT_IMPORT = re.compile(r"cannot import name '(\w+)'")
_COMPILE = re.compile(
    r"(error: |fatal error: |undefined reference to|compilation terminated|"
    r"error\[E\d+\]|could not compile|^make(?:\[\d+\])?: \*\*\*|"
    r"go: build failed|\[build\] .*failed|# command-line-arguments|cannot find symbol|"
    r"ninja: build stopped|FAILED: .*\.o)",
    re.M,
)
# A compiler/linker diagnostic proper (not the build tool's own "FAILED:" bookkeeping).
_COMPILE_STRONG = re.compile(
    r"(\berror: |fatal error: |undefined reference to|ld\.lld: error|ld: error|"
    r"ninja: build stopped|compilation terminated)"
)
_BEHAVIOURAL = re.compile(
    r"(AssertionError|^E\s+assert |^FAIL(?:ED)?\b|--- FAIL:|\bassert \w)", re.M
)


# C/C++ runtime evidence that the program itself misbehaved (as opposed to not building).
_C_RUNTIME = re.compile(
    r"(ERROR: (?:Address|Leak|Memory|Undefined|Thread)Sanitizer|^SUMMARY: \w*Sanitizer|"
    r"runtime error: |Assertion '.*' failed|Assertion failed: |: Assertion `.*' failed|"
    r"Segmentation fault|Aborted \(core dumped\)|\bdouble free or corruption)",
    re.M,
)
# Exit codes of a test killed by SIGABRT / SIGSEGV / SIGBUS / SIGFPE: negative when run directly,
# 128+N when a shell reports the signal.
_CRASH_CODES = {-6: "SIGABRT", -7: "SIGBUS", -8: "SIGFPE", -11: "SIGSEGV",
                134: "SIGABRT", 135: "SIGBUS", 136: "SIGFPE", 139: "SIGSEGV"}  # fmt: skip
# Build-tool status lines such as "FAILED: build/foo.o" are not test failures.
_NINJA_FAILED = re.compile(r"^FAILED: .*$", re.M)


def classify_failure(
    output: str,
    returncode: int = 1,
    timed_out: bool = False,
    added_symbols: set[str] | None = None,
) -> FailureClass:
    """Classify a failing run's output. ``added_symbols`` are names the patch introduces."""
    added = added_symbols or set()
    if timed_out:
        return FailureClass("timeout", WRONG_KINDS["timeout"])
    if returncode in (126, 127):
        return FailureClass("command", WRONG_KINDS["command"], output.strip()[-300:])

    m = _C_RUNTIME.search(output)
    if m:
        return FailureClass("behavioural", "C runtime failure (sanitizer/assertion)", m.group(0))
    if returncode in _CRASH_CODES and not _COMPILE_STRONG.search(output):
        return FailureClass(
            "behavioural", f"test crashed with {_CRASH_CODES[returncode]}", f"exit {returncode}"
        )

    m = _SYNTAX.search(output)
    if m:
        return FailureClass("syntax", WRONG_KINDS["syntax"], m.group(0).strip())
    m = _IMPORT.search(output)
    if m:
        return FailureClass("import", WRONG_KINDS["import"], m.group(0).strip())
    m = _COLLECTION.search(output)
    if m:
        return FailureClass("collection", WRONG_KINDS["collection"], m.group(0).strip())
    for rx in (_NAME, _ATTR, _CANNOT_IMPORT):
        for m in rx.finditer(output):
            sym = m.group(1).split(".")[-1]
            if sym in added:
                return FailureClass(
                    "missing_symbol", WRONG_KINDS["missing_symbol"], m.group(0).strip()
                )
    if _COMPILE.search(output) and not _BEHAVIOURAL.search(_NINJA_FAILED.sub("", output)):
        m = _COMPILE.search(output)
        return FailureClass("compile", WRONG_KINDS["compile"], m.group(0).strip())
    if _BEHAVIOURAL.search(_NINJA_FAILED.sub("", output)):
        return FailureClass("behavioural", "test assertion failed / behaviour differs")
    return FailureClass("unknown", "test failed (no specific wrong-reason signature found)")


_PY_ADDED = re.compile(r"^\s*(?:async\s+def|def|class)\s+(\w+)|^(\w+)\s*(?::[^=]+)?=(?!=)")
_OTHER_ADDED = re.compile(r"\b(?:fn|func|function|struct|enum|trait|interface|class)\s+(\w+)")


def added_symbols(added_lines: list[str]) -> set[str]:
    """Names (functions/classes/top-level assignments) introduced by added lines."""
    out: set[str] = set()
    for ln in added_lines:
        m = _PY_ADDED.match(ln)
        if m:
            out.add(m.group(1) or m.group(2))
        out.update(_OTHER_ADDED.findall(ln))
    return out
