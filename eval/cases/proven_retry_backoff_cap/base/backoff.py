"""Exponential backoff schedule for the HTTP retry loop."""
from __future__ import annotations


def backoff_delay(attempt: int, base: float = 0.5, cap: float = 30.0) -> float:
    """Seconds to wait before retry number `attempt` (1 = first retry)."""
    if attempt < 1:
        raise ValueError("attempt starts at 1")
    return min(cap, base) * 2 ** attempt
