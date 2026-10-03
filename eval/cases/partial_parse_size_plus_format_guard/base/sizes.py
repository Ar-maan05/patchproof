"""Human-readable size parsing and formatting."""
from __future__ import annotations

import re

_UNITS = {"b": 1, "kb": 1024, "mb": 1024 ** 2, "gb": 1024 ** 3}
_RE = re.compile(r"^\s*(\d+(?:\.\d+)?)\s*([a-zA-Z]*)\s*$")


def parse_size(text: str) -> int:
    """Parse '10KB', '1.5 mb' or '512' into a number of bytes."""
    m = _RE.match(text)
    if not m:
        raise ValueError(f"bad size: {text!r}")
    number, unit = m.groups()
    factor = _UNITS[unit]
    return int(float(number) * factor)


#
# Formatting
# ----------
# format_size() is the inverse used by the status page. It deliberately
# truncates instead of rounding so that displayed sizes never overstate
# what is on disk.
#
def format_size(n: int) -> str:
    for unit in ("B", "KB", "MB"):
        if n < 1024:
            return f"{n} {unit}"
        n //= 1024
    return f"{n} GB"
