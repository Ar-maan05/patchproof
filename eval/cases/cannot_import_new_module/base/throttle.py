"""Minimum-interval throttle."""
import time


class Throttle:
    def __init__(self, min_interval: float) -> None:
        self.min_interval = min_interval
        self._last = None

    def allow(self) -> bool:
        now = time.monotonic()
        if self._last is not None and now - self._last < self.min_interval:
            return False
        self._last = now
        return True
