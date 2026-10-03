from patchproof.ddmin import ddmin


def run(items, needed):
    calls = []

    def passes(subset):
        calls.append(tuple(subset))
        return needed <= set(subset)

    return ddmin(items, passes), calls


def test_single_needed():
    res, _ = run(list(range(8)), {3})
    assert res == [3]


def test_two_needed():
    res, _ = run(list(range(10)), {2, 7})
    assert res == [2, 7]


def test_all_needed():
    res, _ = run([1, 2, 3], {1, 2, 3})
    assert res == [1, 2, 3]


def test_single_item():
    res, calls = run(["a"], {"a"})
    assert res == ["a"] and calls == []


def test_never_tests_same_subset_twice():
    _, calls = run(list(range(12)), {1, 5, 9})
    assert len(calls) == len(set(map(frozenset, calls)))


def test_or_semantics_finds_a_minimal_subset():
    # passes if 2 OR 6 is present: either alone is a valid minimal answer
    res = ddmin(list(range(8)), lambda s: 2 in s or 6 in s)
    assert len(res) == 1 and res[0] in (2, 6)


def test_few_calls_for_single_culprit():
    _, calls = run(list(range(64)), {40})
    assert len(calls) < 25
