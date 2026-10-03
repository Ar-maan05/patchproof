"""Release helpers."""
from __future__ import annotations


def _key(version: str) -> tuple[int, ...]:
    return tuple(int(p) for p in version.split("."))


def latest_version(versions: list[str]) -> str | None:
    """Highest version in `versions`, or None if there are none."""
    return sorted(versions, key=_key)[-1]
