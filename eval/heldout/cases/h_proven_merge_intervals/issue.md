# merge_intervals leaves touching ranges apart and shrinks nested ones

Calling `merge_intervals([(1, 3), (3, 5)])` returns `[(1, 3), (3, 5)]`. Ranges that share an endpoint are supposed to be treated as one contiguous range.

Also, `merge_intervals([(1, 10), (2, 3)])` returns `[(1, 3)]`: the big range lost most of its extent. I would expect `[(1, 10)]`.

Expected: `[(1, 5)]` for the first call.
