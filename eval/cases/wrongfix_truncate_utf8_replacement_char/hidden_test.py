from truncate import truncate_utf8


def test_result_fits_budget_exactly():
    out = truncate_utf8("日本語", 4)
    assert out == "日"
    assert len(out.encode("utf-8")) <= 4
