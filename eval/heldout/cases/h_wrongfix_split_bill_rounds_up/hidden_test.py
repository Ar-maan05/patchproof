from billing import split_bill


def test_shares_sum_exactly():
    shares = split_bill(100, 3)
    assert sum(shares) == 100
    assert max(shares) - min(shares) <= 1
