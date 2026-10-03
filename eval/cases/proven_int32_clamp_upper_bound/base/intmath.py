"""Saturating integer arithmetic for the binary wire format."""
INT32_MIN = -(2 ** 31)
INT32_MAX = 2 ** 31


def clamp_int32(n: int) -> int:
    """Clamp `n` into the signed 32-bit range."""
    return max(INT32_MIN, min(n, INT32_MAX))


def add_saturating(a: int, b: int) -> int:
    return clamp_int32(a + b)
