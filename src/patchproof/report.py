"""Human-readable verdict cards."""

from __future__ import annotations

import io

from rich.console import Console, Group
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from .pipeline import Report
from .verdict import Verdict

STYLE = {
    Verdict.PROVEN: ("green", "PATCH IS LOAD-BEARING"),
    Verdict.WEAK_TEST: ("yellow", "TEST TOO WEAK TO TRUST"),
    Verdict.NO_OP: ("red", "PATCH DOES NOTHING"),
    Verdict.PARTIAL: ("yellow", "PART OF THE PATCH IS UNEXERCISED"),
    Verdict.CANNOT_REPRODUCE: ("red", "BUG NOT REPRODUCED"),
    Verdict.FLAKY: ("magenta", "RESULTS ARE NOT STABLE"),
    Verdict.ERROR: ("bold red", "COULD NOT RUN"),
}


def _runs(label: str, runs: list[dict]) -> str:
    if not runs:
        return ""
    passed = sum(r["passed"] for r in runs)
    return f"{label}: {passed}/{len(runs)} passed"


def render_card(report: Report, console: Console) -> None:
    color, tagline = STYLE[report.verdict]
    ev = report.evidence
    body: list = [Text(report.summary, style="bold"), Text("")]

    runs = "   ".join(x for x in (_runs("base", ev.get("base_runs", [])),
                                  _runs("head", ev.get("head_runs", []))) if x)  # fmt: skip
    if runs:
        body.append(Text(f"runs      {runs}"))
    cov = ev.get("coverage")
    if cov:
        if cov.get("available"):
            hunks = [h for h in report.hunks if h.get("executed") is not None]
            ran = sum(1 for h in hunks if h["executed"])
            body.append(Text(f"coverage  {ran}/{len(hunks)} changed code hunks executed on head"))
            fr = cov.get("base", {}).get("frames_in_patched_files", [])
            if fr:
                f = fr[0]
                body.append(Text(f"          base failure passes through {f['file']}:{f['line']}"))
        else:
            body.append(Text(f"coverage  unavailable ({cov.get('reason', '?')})", style="dim"))
    ab = ev.get("ablation")
    if ab and "minimal_hunks" in ab:
        body.append(
            Text(
                f"ablation  {len(ab['minimal_hunks'])}/{ab['hunks_total']} hunks needed "
                f"({ab['test_runs']} test runs)"
            )  # fmt: skip
        )
    ast_ev = ev.get("assertion_strength")
    if ast_ev:
        n_weak = sum(1 for v in ast_ev.values() if v["class"] == "WEAK")
        body.append(
            Text(
                f"asserts   {len(ast_ev) - n_weak} strong, {n_weak} weak discriminating test(s)"
            )  # fmt: skip
        )
        for name, v in ast_ev.items():
            if v["class"] == "WEAK":
                body.append(Text(f"          {name}: {'; '.join(v['reasons'])}", style="yellow"))
    so = ev.get("second_opinion")
    if so:
        if so.get("skipped"):
            body.append(Text(f"2nd opin. skipped ({so['skipped']})", style="dim"))
        else:
            body.append(
                Text(
                    f"2nd opin. {so['generated']} LLM test(s), {len(so['dropped'])} dropped, "
                    f"{len(so['confirmed'])} confirmed failing on head"
                )  # fmt: skip
            )
            body.append(Text("          (an LLM's opinion, not proof)", style="dim"))
            for c in so["confirmed"][:3]:
                body.append(Text(f"          FAILS: {c['test']}", style="yellow"))
    mu = ev.get("mutation")
    if mu and mu.get("status") == "inconclusive":
        body.append(
            Text(
                f"mutation  inconclusive ({mu.get('valid_mutants', 0)} mutants, need "
                f"{mu.get('min_mutants')}): not counted as evidence",
                style="dim",
            )  # fmt: skip
        )
    elif mu and mu.get("mutants_run"):
        body.append(
            Text(
                f"mutation  {mu['killed']} killed, {mu['survived']} survived"
                f" (threshold {mu['threshold']:.0%} survival)"
            )  # fmt: skip
        )
    elif mu and mu.get("skipped"):
        body.append(Text("mutation  skipped", style="dim"))

    if report.hunks:
        table = Table(show_header=True, header_style="dim", box=None, pad_edge=False)
        table.add_column("hunk")
        table.add_column("status")
        for h in report.hunks:
            st = h["status"]
            style = {"required": "green", "not_exercised": "yellow", "test": "cyan"}.get(st, "")
            table.add_row(h["id"], Text(st, style=style))
        body += [Text(""), table]
    survivors = (mu or {}).get("survivors") or []
    if survivors:
        body.append(Text(""))
        body.append(Text("surviving mutants (test did not notice):", style="yellow"))
        for s in survivors[:8]:
            body.append(Text(f"  {s['file']}:{s['line']}  {s['description']}"))
        if len(survivors) > 8:
            body.append(Text(f"  ... and {len(survivors) - 8} more"))
    if report.verdict is Verdict.CANNOT_REPRODUCE:
        t = ev.get("base_failure", {}).get("output_tail") or ev.get("head_failure_output_tail")
        if t:
            lines = t.splitlines()
            # A sanitizer report is trimmed to start at its ERROR line: show its top, not its tail.
            sanitizer = bool(lines) and "Sanitizer" in lines[0] and "ERROR" in lines[0]
            shown = lines[:12] if sanitizer else lines[-12:]
            body += [Text(""), Text("failure output (tail):", style="dim"),
                     Text("\n".join(shown), style="dim")]  # fmt: skip
    if report.verdict is Verdict.ERROR and ev.get("error") and ev["error"] != report.summary:
        body.append(Text(ev["error"]))

    total = report.timings.get("total")
    sub = f"{total:.1f}s" if total is not None else ""
    console.print(
        Panel(
            Group(*body),
            title=f"[{color}]{report.verdict}[/]  {tagline}",
            subtitle=sub,
            border_style=color,
            expand=False,
            width=min(console.width, 100),
        )
    )


def render_text(report: Report, width: int = 100) -> str:
    """Plain-text rendering (used by tests and when stdout is not a terminal)."""
    buf = io.StringIO()
    render_card(report, Console(file=buf, width=width, color_system=None, force_terminal=False))
    return buf.getvalue()
