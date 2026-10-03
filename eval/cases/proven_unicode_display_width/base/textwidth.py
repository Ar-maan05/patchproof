"""Terminal column width helpers used by the table renderer."""
from __future__ import annotations


def display_width(text: str) -> int:
    """Number of terminal columns `text` occupies."""
    return len(text)


def ljust_display(text: str, width: int, fill: str = " ") -> str:
    """Left-justify `text` to `width` terminal columns."""
    pad = width - display_width(text)
    return text + fill * max(pad, 0)
