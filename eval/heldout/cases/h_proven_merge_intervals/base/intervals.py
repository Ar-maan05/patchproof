"""Interval utilities for the calendar free/busy view."""
from __future__ import annotations


def merge_intervals(intervals):
    """Merge overlapping or touching (start, end) pairs; returns sorted tuples."""
    out = []
    for start, end in sorted(intervals):
        if out and start < out[-1][1]:
            out[-1][1] = end
        else:
            out.append([start, end])
    return [tuple(x) for x in out]
