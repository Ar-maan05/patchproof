"""Full-jitter exponential backoff."""
import random


def jittered_delay(attempt: int, base: float = 1.0, cap: float = 30.0) -> float:
    return random.uniform(0, base * 2 ** attempt)
