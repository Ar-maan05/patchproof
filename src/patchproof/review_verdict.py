"""Verdicts for ``patchproof review`` (is a review suggestion necessary?)."""

from __future__ import annotations

from enum import StrEnum


class ReviewVerdict(StrEnum):
    NECESSARY = "NECESSARY"
    CLAIM_CONFIRMED = "CLAIM_CONFIRMED"
    NOT_SHOWN = "NOT_SHOWN"
    NO_BEHAVIOUR_CHANGE = "NO_BEHAVIOUR_CHANGE"
    HARMFUL = "HARMFUL"
    FLAKY = "FLAKY"
    ERROR = "ERROR"


# Highest priority first. NO_BEHAVIOUR_CHANGE is not in the list: it short-circuits the run.
PRIORITY = [
    ReviewVerdict.ERROR,
    ReviewVerdict.HARMFUL,
    ReviewVerdict.FLAKY,
    ReviewVerdict.NECESSARY,
    ReviewVerdict.CLAIM_CONFIRMED,
    ReviewVerdict.NOT_SHOWN,
]


def combine(findings: list[tuple[ReviewVerdict, str]]) -> tuple[ReviewVerdict, str]:
    """Pick the highest-priority finding; NOT_SHOWN when there are none."""
    if not findings:
        return ReviewVerdict.NOT_SHOWN, "nothing was shown"
    return min(findings, key=lambda f: PRIORITY.index(f[0]))


def exit_code(v: ReviewVerdict) -> int:
    """0 when the claim stands (NECESSARY, CLAIM_CONFIRMED), 2 for ERROR, 1 otherwise."""
    if v in (ReviewVerdict.NECESSARY, ReviewVerdict.CLAIM_CONFIRMED):
        return 0
    if v is ReviewVerdict.ERROR:
        return 2
    return 1
