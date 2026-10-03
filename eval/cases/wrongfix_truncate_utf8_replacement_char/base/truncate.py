"""Byte-budget truncation for fields with a fixed storage size."""


def truncate_utf8(text: str, max_bytes: int) -> str:
    """Longest prefix of `text` whose UTF-8 encoding fits in `max_bytes`."""
    return text.encode("utf-8")[:max_bytes].decode("utf-8")
