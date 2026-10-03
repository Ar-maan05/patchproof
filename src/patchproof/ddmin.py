"""Delta debugging (Zeller's ddmin) to find a minimal passing subset of hunks."""

from __future__ import annotations

from collections.abc import Callable, Sequence


def ddmin[T](items: Sequence[T], passes: Callable[[list[T]], bool]) -> list[T]:
    """Return a 1-minimal-ish subset of ``items`` for which ``passes`` is True.

    Assumes ``passes(list(items))`` is True and ``passes([])`` is False. Results are cached by the
    set of selected indices, so ``passes`` is called at most once per distinct subset.
    """
    cache: dict[frozenset[int], bool] = {}

    def test(idx: list[int]) -> bool:
        key = frozenset(idx)
        if key not in cache:
            cache[key] = passes([items[i] for i in sorted(key)])
        return cache[key]

    current = list(range(len(items)))
    n = 2
    while len(current) >= 2:
        size = len(current)
        n = min(n, size)
        chunk = size / n
        subsets = [current[round(i * chunk) : round((i + 1) * chunk)] for i in range(n)]
        subsets = [s for s in subsets if s]
        reduced = False
        for s in subsets:
            if len(s) < size and test(s):
                current, n, reduced = s, 2, True
                break
        if not reduced:
            for s in subsets:
                comp = [i for i in current if i not in set(s)]
                if comp and len(comp) < size and test(comp):
                    current, n, reduced = comp, max(n - 1, 2), True
                    break
        if not reduced:
            if n >= size:
                break
            n = min(size, n * 2)
    return [items[i] for i in current]
