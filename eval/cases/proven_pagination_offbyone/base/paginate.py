"""Pagination helpers for the list endpoints."""
from __future__ import annotations

from typing import Sequence, TypeVar

T = TypeVar("T")

MAX_PER_PAGE = 100


def page_count(total: int, per_page: int) -> int:
    """Number of pages needed to show `total` items."""
    if per_page <= 0:
        raise ValueError("per_page must be positive")
    return (total + per_page - 1) // per_page


def paginate(items: Sequence[T], page: int, per_page: int) -> list[T]:
    """Return the items on `page` (1-indexed)."""
    if per_page <= 0:
        raise ValueError("per_page must be positive")
    per_page = min(per_page, MAX_PER_PAGE)
    start = page * per_page
    return list(items[start:start + per_page])
