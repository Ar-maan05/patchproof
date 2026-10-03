"""Billing-period helpers."""
from __future__ import annotations

from datetime import date


def in_period(day: date, start: date, end: date) -> bool:
    """True if `day` falls in the billing period [start, end], both inclusive."""
    if end < start:
        raise ValueError("period ends before it starts")
    return start <= day < end


def days_in_period(start: date, end: date) -> int:
    return (end - start).days + 1
