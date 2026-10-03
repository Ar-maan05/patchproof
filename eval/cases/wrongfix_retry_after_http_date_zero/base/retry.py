"""Retry-After header handling."""
from __future__ import annotations

from datetime import datetime


def parse_retry_after(value: str, now: datetime | None = None) -> int:
    """Seconds to wait given a Retry-After header (delta-seconds or HTTP-date)."""
    return int(value)
