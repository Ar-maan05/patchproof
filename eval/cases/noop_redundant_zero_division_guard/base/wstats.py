"""Weighted statistics for the latency dashboard."""
from __future__ import annotations

from typing import Sequence


def weighted_mean(values: Sequence[float], weights: Sequence[float]) -> float:
    if len(values) != len(weights):
        raise ValueError("values and weights differ in length")
    if any(w <= 0 for w in weights):
        raise ValueError("weights must be positive")
    if not values:
        return 0.0
    total = sum(weights)
    return sum(v * w for v, w in zip(values, weights)) / total
