# business_days counts weekends

`business_days(date(2026, 10, 5), date(2026, 10, 12))` returns 7 for the Monday-to-Monday week; I expected 5. Weekends must not count.

Counting convention is unchanged: days d with `start <= d < end`.
