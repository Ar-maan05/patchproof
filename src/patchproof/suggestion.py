"""Review comments: parsing GitHub ``suggestion`` blocks into a unified diff and a claim."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath


class SuggestionError(ValueError):
    """The comment cannot be turned into a claim and a suggested change."""


@dataclass
class Comment:
    """The fields of a GitHub pull-request review comment that patchproof reads."""

    body: str
    path: str | None = None
    line: int | None = None
    start_line: int | None = None
    original_line: int | None = None
    side: str | None = None
    commit_id: str | None = None
    diff_hunk: str = ""
    html_url: str = ""
    user: str = ""

    @classmethod
    def from_json(cls, data: object) -> Comment:
        if not isinstance(data, dict):
            raise SuggestionError("the comment JSON must be one object")
        body = data.get("body")
        if not isinstance(body, str):
            raise SuggestionError("the comment JSON has no string 'body'")

        def num(key: str) -> int | None:
            v = data.get(key)
            return v if isinstance(v, int) and not isinstance(v, bool) else None

        def text(key: str) -> str | None:
            v = data.get(key)
            return v if isinstance(v, str) and v else None

        user = data.get("user")
        login = user.get("login") if isinstance(user, dict) else None
        return cls(
            body=body,
            path=text("path"),
            line=num("line"),
            start_line=num("start_line"),
            original_line=num("original_line"),
            side=text("side"),
            commit_id=text("commit_id"),
            diff_hunk=text("diff_hunk") or "",
            html_url=text("html_url") or "",
            user=login if isinstance(login, str) else "",
        )

    def line_range(self) -> tuple[int, int] | None:
        """(first, last) commented line (1-based, inclusive), or None for an unanchored comment.

        ``line`` is null once the comment is outdated; then ``original_line`` is the best
        available anchor (a single line: the original start is not among the fields we read).
        """
        end = self.line if self.line is not None else self.original_line
        if self.path is None or end is None:
            return None
        start = self.start_line if self.start_line is not None and self.line is not None else end
        return (min(start, end), end)

    def to_evidence(self) -> dict:
        return {
            "user": self.user,
            "html_url": self.html_url,
            "path": self.path,
            "line_range": list(self.line_range() or ()),
            "side": self.side,
            "commit_id": self.commit_id,
        }


_BLOCK = re.compile(
    r"^ {0,3}(?P<fence>`{3,})[ \t]*suggestion[^\n]*\n(?P<body>.*?)^ {0,3}(?P=fence)`*[ \t]*$",
    re.S | re.M,
)


def normalise_newlines(text: str) -> str:
    return text.replace("\r\n", "\n").replace("\r", "\n")


def parse_body(body: str) -> tuple[str, list[list[str]]]:
    """Split a comment body into (claim, suggestion blocks).

    The claim is the body with the suggestion blocks removed. Each block is the list of lines
    that replace the commented range; an empty list (``suggestion`` fence directly followed by
    the closing fence) means "delete those lines", whereas a block holding one empty line
    replaces the range with a single blank line.
    """
    body = normalise_newlines(body)
    blocks: list[list[str]] = []
    for m in _BLOCK.finditer(body):
        content = m.group("body")
        blocks.append(content[:-1].split("\n") if content else [])
    claim = _BLOCK.sub("", body)
    claim = re.sub(r"\n{3,}", "\n\n", claim).strip()
    return claim, blocks


def split_lines(text: str) -> list[str]:
    """Lines with their terminators (only ``\\n`` separates lines, as in git)."""
    return re.findall(r"[^\n]*\n|[^\n]+", text)


def _emit(prefix: str, line: str) -> str:
    if line.endswith("\n"):
        return prefix + line
    return prefix + line + "\n\\ No newline at end of file\n"


def build_patch(path: str, old_text: str, start: int, end: int, repl: list[str]) -> tuple[str, str]:
    """(unified diff, new file text) replacing lines ``start..end`` of ``old_text`` by ``repl``.

    Line endings follow the replaced lines (CRLF files stay CRLF); when the replaced range
    ends the file without a final newline, the replacement does too.
    """
    old = split_lines(old_text)
    if start < 1 or end < start or end > len(old):
        raise SuggestionError(
            f"suggestion range {start}-{end} is outside {path} ({len(old)} lines)"
        )
    replaced = old[start - 1 : end]
    eol = "\r\n" if replaced and replaced[0].endswith("\r\n") else "\n"
    last_has_nl = replaced[-1].endswith("\n")
    new_lines = [r + eol for r in repl]
    if new_lines and not last_has_nl:
        new_lines[-1] = new_lines[-1][: -len(eol)]
    s0, e0 = start - 1, end
    pre = max(0, s0 - 3)
    post = min(len(old), e0 + 3)
    old_len = post - pre
    new_len = old_len - (e0 - s0) + len(new_lines)
    body = [_emit(" ", ln) for ln in old[pre:s0]]
    body += [_emit("-", ln) for ln in old[s0:e0]]
    body += [_emit("+", ln) for ln in new_lines]
    body += [_emit(" ", ln) for ln in old[e0:post]]
    old_start = pre + 1 if old_len else pre
    new_start = pre + 1 if new_len else pre
    patch = (
        f"diff --git a/{path} b/{path}\n--- a/{path}\n+++ b/{path}\n"
        f"@@ -{old_start},{old_len} +{new_start},{new_len} @@\n" + "".join(body)
    )
    new_text = "".join(old[:s0] + new_lines + old[e0:])
    return patch, new_text


@dataclass
class Suggestion:
    path: str
    start: int
    end: int
    lines: list[str]
    patch: str
    new_text: str
    blocks: int = 1
    notes: list[str] = field(default_factory=list)

    def to_evidence(self) -> dict:
        return {
            "source": "suggestion block",
            "path": self.path,
            "range": [self.start, self.end],
            "replacement": self.lines,
            "blocks": self.blocks,
            "notes": self.notes,
            "patch": self.patch,
        }


def _safe_relpath(path: str) -> str:
    p = PurePosixPath(path.replace("\\", "/"))
    if p.is_absolute() or ".." in p.parts or not p.parts:
        raise SuggestionError(f"unsafe path in the comment: {path!r}")
    return str(p)


def suggestion_from_comment(comment: Comment, repo: Path) -> tuple[str, Suggestion | None]:
    """(claim, suggestion or None) for a comment, reading the anchored file from ``repo``."""
    claim, blocks = parse_body(comment.body)
    if not blocks:
        return claim, None
    distinct = {tuple(b) for b in blocks}
    if len(distinct) > 1:
        raise SuggestionError(
            f"the comment has {len(blocks)} suggestion blocks with different content for the "
            "same line range: ambiguous (use --suggestion-patch to say which one to test)"
        )
    if comment.path is None:
        raise SuggestionError("a suggestion block needs a review comment anchored to a file")
    if (comment.side or "RIGHT").upper() != "RIGHT":
        raise SuggestionError("suggestions can only target the RIGHT side of the diff")
    rng = comment.line_range()
    if rng is None:
        raise SuggestionError("the comment has no line / original_line to anchor the suggestion")
    path = _safe_relpath(comment.path)
    target = repo / path
    try:
        old_text = target.read_bytes().decode("utf-8")
    except (OSError, UnicodeDecodeError) as e:
        raise SuggestionError(f"cannot read {path} in --repo: {e}") from e
    start, end = rng
    patch, new_text = build_patch(path, old_text, start, end, list(blocks[0]))
    notes = []
    if comment.line is None:
        notes.append("comment is outdated (line is null): used original_line")
    return claim, Suggestion(path, start, end, list(blocks[0]), patch, new_text, len(blocks), notes)
