"""Parse 'a=1;b;c=3' option strings. A bare key is a flag whose value is ''."""
from __future__ import annotations


def parse_kv(text: str) -> dict[str, str]:
    out: dict[str, str] = {}
    for part in text.split(";"):
        part = part.strip()
        if not part:
            continue
        key, value = part.split("=", 1)
        out[key.strip()] = value.strip()
    return out
