"""Duration strings like '1h30m'."""
import re

_UNITS = {"s": 1, "m": 60, "h": 3600}


def parse_duration(text: str) -> int:
    """Total seconds in a string made of <int><s|m|h> parts."""
    m = re.match(r"(\d+)([smh])", text)
    if not m:
        raise ValueError(f"bad duration: {text!r}")
    return int(m.group(1)) * _UNITS[m.group(2)]


#
# Rendering
# ---------
# The inverse of parse_duration, used in log messages. Output always
# uses the largest units that fit, without zero-valued parts.
#
# Seconds are never fractional here.
#
def format_duration(seconds: int) -> str:
    parts = []
    for unit, size in (("h", 3600), ("m", 60), ("s", 1)):
        n, seconds = divmod(seconds, size)
        if n:
            parts.append(f"{n}{unit}")
    return "".join(parts) or "0s"
