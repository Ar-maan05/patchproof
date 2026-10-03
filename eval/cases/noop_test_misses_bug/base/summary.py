"""Summary statistics."""


def median(values):
    if not values:
        raise ValueError("median of empty sequence")
    s = sorted(values)
    mid = len(s) // 2
    if len(s) % 2:
        return s[mid]
    return s[mid]
