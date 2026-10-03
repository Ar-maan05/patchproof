from datetime import date

from bizdays import business_days


def test_friday_to_monday_is_one_day():
    assert business_days(date(2026, 10, 9), date(2026, 10, 12)) == 1


def test_weekend_only_is_zero():
    assert business_days(date(2026, 10, 10), date(2026, 10, 12)) == 0
