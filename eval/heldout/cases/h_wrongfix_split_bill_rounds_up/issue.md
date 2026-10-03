# split_bill loses money

`split_bill(100, 3)` returns `[33, 33, 33]`, which sums to 99. One cent has vanished, and our ledger no longer balances.

Expected: the returned shares always sum to exactly `total_cents`, and differ by at most one cent from each other.
