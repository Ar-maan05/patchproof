# Page 1 returns the second page of results

`paginate(list(range(10)), 1, 3)` returns `[3, 4, 5]`. Pages are 1-indexed in the API docs, so I expect `[0, 1, 2]`. The last page and out-of-range pages seem shifted by the same amount.
