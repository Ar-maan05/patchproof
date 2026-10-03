from leap import is_leap_year


def test_century_divisible_by_400_is_leap():
    assert is_leap_year(2000)
    assert not is_leap_year(2100)
