import pytest

from patchproof.verdict import PRIORITY, Verdict, combine, exit_code


def test_priority_order_exact():
    assert [str(v) for v in PRIORITY] == [
        "ERROR", "CANNOT_REPRODUCE", "FLAKY", "NO_OP", "WEAK_TEST", "PARTIAL", "PROVEN",
    ]  # fmt: skip


def test_no_findings_is_proven():
    assert combine([])[0] is Verdict.PROVEN


@pytest.mark.parametrize("i", range(len(PRIORITY) - 1))
def test_higher_priority_wins_regardless_of_order(i):
    hi, lo = PRIORITY[i], PRIORITY[i + 1]
    assert combine([(lo, "lo"), (hi, "hi")]) == (hi, "hi")
    assert combine([(hi, "hi"), (lo, "lo")]) == (hi, "hi")


def test_weak_test_beats_partial():
    assert combine([(Verdict.PARTIAL, "p"), (Verdict.WEAK_TEST, "w")])[0] is Verdict.WEAK_TEST


def test_exit_codes():
    assert exit_code(Verdict.PROVEN) == 0
    assert exit_code(Verdict.ERROR) == 2
    for v in (Verdict.WEAK_TEST, Verdict.NO_OP, Verdict.PARTIAL, Verdict.FLAKY,
              Verdict.CANNOT_REPRODUCE):  # fmt: skip
        assert exit_code(v) == 1


def test_verdict_strings_are_exact():
    assert Verdict.CANNOT_REPRODUCE == "CANNOT_REPRODUCE"
    assert f"{Verdict.NO_OP}" == "NO_OP"
