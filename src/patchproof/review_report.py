"""Verdict card, JSON-ready dict and pasteable reply for ``patchproof review``."""

from __future__ import annotations

import io
import re

from rich.console import Console, Group
from rich.panel import Panel
from rich.text import Text

from .review import ReviewReport
from .review_verdict import ReviewVerdict

V = ReviewVerdict

STYLE = {
    V.NECESSARY: ("green", "SUGGESTION IS NECESSARY"),
    V.CLAIM_CONFIRMED: ("yellow", "CLAIM REPRODUCED"),
    V.NOT_SHOWN: ("yellow", "CLAIM NOT REPRODUCED"),
    V.NO_BEHAVIOUR_CHANGE: ("cyan", "SUGGESTION CHANGES NOTHING"),
    V.HARMFUL: ("red", "SUGGESTION BREAKS EXISTING TESTS"),
    V.FLAKY: ("magenta", "RESULTS ARE NOT STABLE"),
    V.ERROR: ("bold red", "COULD NOT RUN"),
}


def _runs(runs: list[dict] | None) -> str:
    if not runs:
        return "not run"
    return f"{sum(r['passed'] for r in runs)}/{len(runs)} passed"


def _failed(runs: list[dict] | None) -> str:
    """'2/2 runs failed' style summary."""
    if not runs:
        return "not run"
    return f"{sum(not r['passed'] for r in runs)}/{len(runs)} runs failed"


def _clip(text: str, n: int = 160) -> str:
    text = " ".join(text.split())
    return text if len(text) <= n else text[: n - 3] + "..."


def render_card(report: ReviewReport, console: Console) -> None:
    color, tagline = STYLE[report.verdict]
    ev = report.evidence
    body: list = [Text(report.summary, style="bold"), Text("")]
    c = ev.get("comment") or {}
    if c:
        rng = c.get("line_range") or []
        where = (
            f"{c.get('path')}:{rng[0]}"
            + (f"-{rng[1]}" if len(rng) > 1 and rng[1] != rng[0] else "")
            if c.get("path") and rng
            else "(PR level)"
        )
        who = f"{c['user']} on " if c.get("user") else ""
        body.append(Text(f"comment   {who}{where}"))
    if ev.get("claim"):
        body.append(Text(f"claim     {_clip(ev['claim'])}"))
    s = ev.get("suggestion")
    if s:
        if s.get("range"):
            n = len(s.get("replacement") or [])
            body.append(Text(f"suggest   lines {s['range'][0]}-{s['range'][1]} -> {n} line(s)"))
        else:
            body.append(Text(f"suggest   {s.get('source')}"))
    elif "suggestion" in ev:
        body.append(Text("suggest   none (the comment describes the change in prose)"))
    for f in (ev.get("normalisation") or {}).get("files", []):
        mark = "equivalent" if f["equivalent"] else "differs"
        body.append(Text(f"normalise {f['file']}: {mark} ({f['method']})"))
    suite = ev.get("suite")
    if suite:
        line = f"suite     head: {_runs(suite.get('head_runs'))}"
        if "suggestion_runs" in suite:
            line += f"   with suggestion: {_runs(suite['suggestion_runs'])}"
        body.append(Text(line))
        if suite.get("harmful_check"):
            body.append(Text(f"          {suite['harmful_check']}", style="dim"))
    rp = ev.get("reproducer")
    if rp:
        if rp.get("skipped"):
            body.append(Text(f"repro     skipped ({rp['skipped']})", style="dim"))
        else:
            body.append(Text(f"repro     {rp.get('source')}, {rp.get('path')}"))
            line = ""
            if "head_runs" in rp:
                line += f"head: {_runs(rp['head_runs'])}"
            if "suggestion_runs" in rp:
                line += f"   with suggestion: {_runs(rp['suggestion_runs'])}"
            if line:
                body.append(Text(f"          {line}"))
            for a in rp.get("attempts", []):
                if a.get("outcome") != "accepted":
                    why = _clip(a.get("reason") or a.get("outcome", ""), 100)
                    body.append(Text(f"          attempt {a['attempt']}: {a['outcome']}: {why}",
                                     style="yellow"))  # fmt: skip
            if rp.get("error"):
                body.append(Text(f"          {rp['error']}", style="red"))
    tail_text = None
    if report.verdict is V.HARMFUL:
        tail_text = (suite or {}).get("suggestion_failure_output_tail")
        label = "test failure with the suggestion (tail):"
    elif report.verdict in (V.NECESSARY, V.CLAIM_CONFIRMED):
        tail_text = (rp or {}).get("failure_output_tail")
        label = "reproducer failure on the PR head (tail):"
    else:
        label = ""
    if tail_text:
        body += [Text(""), Text(label, style="dim"),
                 Text("\n".join(tail_text.splitlines()[-12:]), style="dim")]  # fmt: skip
    if report.verdict is V.ERROR and ev.get("error") and ev["error"] != report.summary:
        body.append(Text(ev["error"]))
    total = report.timings.get("total")
    console.print(
        Panel(
            Group(*body),
            title=f"[{color}]{report.verdict}[/]  {tagline}",
            subtitle=f"{total:.1f}s" if total is not None else "",
            border_style=color,
            expand=False,
            width=min(console.width, 100),
        )
    )


def render_text(report: ReviewReport, width: int = 100) -> str:
    buf = io.StringIO()
    render_card(report, Console(file=buf, width=width, color_system=None, force_terminal=False))
    return buf.getvalue()


# -- the reply -----------------------------------------------------------------------------------
_ASCII_MAP = {
    "\u2013": "-", "\u2014": "-", "\u2018": "'", "\u2019": "'", "\u201c": '"', "\u201d": '"',
    "\u2026": "...", "\u00a0": " ", "\u2192": "->",
}  # fmt: skip


def ascii_only(text: str) -> str:
    for k, v in _ASCII_MAP.items():
        text = text.replace(k, v)
    return text.encode("ascii", "replace").decode("ascii")


def _fence(code: str) -> str:
    longest = max((len(m) for m in re.findall(r"`+", code)), default=0)
    return "`" * max(3, longest + 1)


def _evidence_paragraph(report: ReviewReport) -> str:
    ev = report.evidence
    rp = ev.get("reproducer") or {}
    suite = ev.get("suite") or {}
    v = report.verdict
    head = _failed(rp.get("head_runs"))
    sugg = _runs(rp.get("suggestion_runs"))
    if v is V.NECESSARY:
        text = (
            "I wrote a reproducer of the claim without looking at the suggestion, restricted to "
            "the project's public entry points (checked statically). It fails on the PR head "
            f"({head}) and passes with the suggestion applied ({sugg}), so the suggestion is "
            "what fixes it."
        )
    elif v is V.CLAIM_CONFIRMED:
        if rp.get("suggestion_runs"):
            text = (
                "The claim reproduces: the reproducer (written without seeing the suggestion, "
                f"public entry points only) fails on the PR head ({head}). But the suggestion "
                f"does not fix it: the reproducer still fails with the suggestion applied ({sugg})."
            )
        else:
            text = (
                "The claim reproduces: the reproducer (written without seeing any suggestion, "
                f"public entry points only) fails on the PR head ({head}). The comment gives no "
                "concrete change, so no fix was tested."
            )
    elif v is V.NOT_SHOWN:
        text = (
            "I could not reproduce the claim through the project's public entry points. "
            f"{len(rp.get('attempts', []))} reproducer attempt(s): "
            + "; ".join(
                f"#{a['attempt']} {_clip(a.get('reason') or a.get('outcome', ''), 120)}"
                for a in rp.get("attempts", [])
            )
            + ". This is evidence, not proof, that the change is unnecessary: the reachability "
            "check is a heuristic and a better reproducer may exist."
        )
    elif v is V.NO_BEHAVIOUR_CHANGE:
        text = (
            "The suggested change is equivalent to the current code after normalisation "
            f"({report.summary.split(': ', 1)[-1]}), so it cannot change behaviour."
        )
    elif v is V.HARMFUL:
        text = (
            "The project's test command passes on the PR head "
            f"({_runs(suite.get('head_runs'))}) and fails with the suggestion applied "
            f"({_runs(suite.get('suggestion_runs'))})."
        )
        failed = [
            ln.strip()
            for ln in (suite.get("suggestion_failure_output_tail") or "").splitlines()
            if ln.startswith("FAILED")
        ]
        if failed:
            text += f" First failure: `{_clip(failed[0], 140)}`."
    elif v is V.FLAKY:
        text = f"Results differ between identical runs, so nothing is concluded ({report.summary})."
    else:
        text = f"patchproof could not run: {report.summary}."
    return text


def _plain_cmd(cmd: str) -> str:
    """The command without the absolute path of the interpreter (it says nothing to a reader)."""
    first, _, rest = cmd.partition(" ")
    return (first.rsplit("/", 1)[-1] + " " + rest).strip() if first.startswith("/") else cmd


def render_reply(report: ReviewReport) -> str:
    """Short plain-ASCII Markdown the author could paste under the review comment."""
    lines = [f"**patchproof verdict: {report.verdict}**", "", _evidence_paragraph(report)]
    rp = report.evidence.get("reproducer") or {}
    code = rp.get("content")
    label = "Reproducer"
    if not code:
        attempts = [a for a in rp.get("attempts", []) if a.get("content")]
        if attempts:
            code, label = attempts[-1]["content"], "Last attempt (not accepted)"
    if code and report.verdict not in (V.HARMFUL, V.NO_BEHAVIOUR_CHANGE, V.ERROR):
        lang = "c" if str(rp.get("path", "")).endswith((".c", ".h", ".cc", ".cpp")) else "python"
        fence = _fence(code)
        cmd = rp.get("cmd")
        how = f"saved as `{rp.get('path')}`" + (f", run with `{_plain_cmd(cmd)}`" if cmd else "")
        lines += ["", f"{label}, {how}:", "", f"{fence}{lang}", code.rstrip("\n"), fence]
    return ascii_only("\n".join(lines)) + "\n"
