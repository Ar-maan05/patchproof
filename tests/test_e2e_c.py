"""A non-Python fixture: text-level mutation on C, build+test through a shell command."""

import shutil

import pytest

from patchproof.pipeline import CheckConfig, check
from patchproof.verdict import Verdict

pytestmark = pytest.mark.skipif(shutil.which("gcc") is None, reason="gcc not installed")

LIB_BUGGY = "int positive(int x) {\n    return x >= 0;\n}\n"
LIB_FIXED = "int positive(int x) {\n    return x > 0;\n}\n"
TEST_C = (
    "#include <assert.h>\nint positive(int);\n\n"
    "int main(void) {\n    assert(positive(1) == 1);\n    assert(positive(0) == 0);\n"
    "    assert(positive(-3) == 0);\n    return 0;\n}\n"
)
CMD = "gcc -o t tests/test_pos.c lib.c && ./t"


def test_c_fix_is_proven_with_text_mutants(scenario):
    repo, patch = scenario({"lib.c": LIB_BUGGY}, {"lib.c": LIB_FIXED, "tests/test_pos.c": TEST_C})
    r = check(CheckConfig(repo=repo, patch_text=patch, test_cmd=CMD, runs=1, timeout=60))
    assert r.verdict is Verdict.PROVEN, r.to_dict()
    assert r.evidence["coverage"]["available"] is False
    assert r.evidence["mutation"]["mutants_run"] >= 1
    assert r.evidence["mutation"]["survived"] == 0


def test_c_weak_test_survives_mutants(scenario):
    weak = TEST_C.replace("    assert(positive(0) == 0);\n", "")
    # base passes this weaker test too -> the patch is a NO_OP as far as the test can tell
    repo, patch = scenario({"lib.c": LIB_BUGGY}, {"lib.c": LIB_FIXED, "tests/test_pos.c": weak})
    r = check(CheckConfig(repo=repo, patch_text=patch, test_cmd=CMD, runs=1, timeout=60))
    assert r.verdict is Verdict.NO_OP
