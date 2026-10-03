"""Version ordering for the update checker."""


def compare(a: str, b: str) -> int:
    """Return -1, 0 or 1 if `a` is older than, equal to or newer than `b`."""
    if a == b:
        return 0
    return -1 if a < b else 1
