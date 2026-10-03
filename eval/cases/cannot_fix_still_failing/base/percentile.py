"""Nearest-rank percentiles."""
from __future__ import annotations

import math


def percentile(values: list[float], p: float) -> float:
    """Nearest-rank percentile, 0 < p <= 100."""
    s = sorted(values)
    idx = int(len(s) * p / 100)
    return s[idx]
