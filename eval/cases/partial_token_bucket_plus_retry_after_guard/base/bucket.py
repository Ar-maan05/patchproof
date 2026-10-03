"""Token bucket rate limiter with an injectable clock."""
from __future__ import annotations

import time


class TokenBucket:
    def __init__(self, rate: float, capacity: int, clock=time.monotonic) -> None:
        self.rate = rate
        self.capacity = capacity
        self._clock = clock
        self._tokens = float(capacity)
        self._last = clock()

    def _refill(self) -> None:
        now = self._clock()
        elapsed = now - self._last
        self._tokens = min(self.capacity, self._tokens + int(elapsed * self.rate))
        self._last = now

    def allow(self, cost: int = 1) -> bool:
        self._refill()
        if self._tokens >= cost:
            self._tokens -= cost
            return True
        return False

    def retry_after(self, cost: int = 1) -> float:
        """Seconds until `cost` tokens will be available.

        Used to populate the Retry-After header on 429 responses. Returns 0.0
        when the request could be served right now. This is advisory only:
        another caller may take the tokens first.
        """
        self._refill()
        missing = cost - self._tokens
        return max(missing, 0.0) / self.rate
