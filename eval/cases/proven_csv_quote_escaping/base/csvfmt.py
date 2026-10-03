"""Minimal CSV row writer used by the export endpoint."""
from __future__ import annotations

from typing import Iterable


def quote_field(field: str) -> str:
    if "," in field:
        return '"' + field + '"'
    return field


def format_row(fields: Iterable[str]) -> str:
    return ",".join(quote_field(f) for f in fields)
