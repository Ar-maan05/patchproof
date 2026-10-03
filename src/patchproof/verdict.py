"""Verdicts, their priority order and exit codes."""

from __future__ import annotations

from enum import StrEnum


class Verdict(StrEnum):
    PROVEN = "PROVEN"
    WEAK_TEST = "WEAK_TEST"
    NO_OP = "NO_OP"
    PARTIAL = "PARTIAL"
    CANNOT_REPRODUCE = "CANNOT_REPRODUCE"
    FLAKY = "FLAKY"
    ERROR = "ERROR"


# Highest priority first.
PRIORITY = [
    Verdict.ERROR,
    Verdict.CANNOT_REPRODUCE,
    Verdict.FLAKY,
    Verdict.NO_OP,
    Verdict.WEAK_TEST,
    Verdict.PARTIAL,
    Verdict.PROVEN,
]

# Verdicts after which the later pipeline stages are meaningless and are skipped.
BLOCKING = {Verdict.ERROR, Verdict.CANNOT_REPRODUCE, Verdict.FLAKY, Verdict.NO_OP}


def combine(findings: list[tuple[Verdict, str]]) -> tuple[Verdict, str]:
    """Pick the highest-priority finding; PROVEN when there are none."""
    if not findings:
        return Verdict.PROVEN, "test fails without the fix, passes with it"
    best = min(findings, key=lambda f: PRIORITY.index(f[0]))
    return best


def exit_code(v: Verdict) -> int:
    if v is Verdict.PROVEN:
        return 0
    if v is Verdict.ERROR:
        return 2
    return 1
