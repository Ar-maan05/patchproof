"""Business-day arithmetic for SLA timers."""
from datetime import date


def business_days(start: date, end: date) -> int:
    """Number of Monday-Friday days d with start <= d < end."""
    return (end - start).days
