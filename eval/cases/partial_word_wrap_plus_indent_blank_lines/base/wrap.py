"""Plain-text wrapping for the CLI help renderer."""
from __future__ import annotations


def word_wrap(text: str, width: int) -> list[str]:
    """Greedy word wrap; no returned line is longer than `width` (barring long words)."""
    lines: list[str] = []
    line = ""
    for word in text.split():
        if line and len(line) + len(word) > width:
            lines.append(line)
            line = word
        else:
            line = f"{line} {word}" if line else word
    if line:
        lines.append(line)
    return lines


# Indentation helper.
#
# The help renderer calls this for every option description. The prefix is
# applied uniformly; callers that do not want trailing whitespace on blank
# lines post-process the result themselves.
#
def indent(lines: list[str], prefix: str = "  ") -> list[str]:
    return [prefix + line for line in lines]
