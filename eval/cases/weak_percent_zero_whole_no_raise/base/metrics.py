"""Dashboard metric helpers."""


def percent(part: float, whole: float) -> float:
    """`part` as a percentage of `whole`."""
    return part / whole * 100
