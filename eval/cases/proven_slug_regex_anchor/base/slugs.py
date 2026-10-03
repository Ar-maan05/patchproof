"""Slug validation for user-chosen URL names."""
import re

_SLUG_RE = re.compile(r"[a-z0-9]+(?:-[a-z0-9]+)*")


def is_valid_slug(value: str) -> bool:
    """Lowercase alphanumerics separated by single hyphens."""
    return _SLUG_RE.match(value) is not None
