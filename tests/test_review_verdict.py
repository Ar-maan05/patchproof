from patchproof.review_verdict import ReviewVerdict as V
from patchproof.review_verdict import combine, exit_code


def test_priority_order():
    order = [V.ERROR, V.HARMFUL, V.FLAKY, V.NECESSARY, V.CLAIM_CONFIRMED, V.NOT_SHOWN]
    for i, hi in enumerate(order):
        for lo in order[i + 1 :]:
            assert combine([(lo, "lo"), (hi, "hi")]) == (hi, "hi")
            assert combine([(hi, "hi"), (lo, "lo")]) == (hi, "hi")


def test_no_findings_is_not_shown():
    assert combine([])[0] is V.NOT_SHOWN


def test_exit_codes():
    assert exit_code(V.NECESSARY) == 0 and exit_code(V.CLAIM_CONFIRMED) == 0
    assert exit_code(V.ERROR) == 2
    for v in (V.NOT_SHOWN, V.NO_BEHAVIOUR_CHANGE, V.HARMFUL, V.FLAKY):
        assert exit_code(v) == 1
