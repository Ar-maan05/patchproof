#!/usr/bin/env python3
"""End-to-end evaluation of `patchproof review` on real systemd review comments.

    python3 eval/review/run_e2e.py [--only CASE ...] [--scratch DIR] [--timeout S]

For every case in eval/review/e2e/<case>/case.json this
  1. makes sure a source tree at the comment's head SHA exists (a git worktree of one scratch
     clone, fetched by full SHA from the local systemd clone; neither that clone nor its sibling
     worktrees are touched),
  2. runs `patchproof review --json` with a persistent workdir (a warm clang+ASAN meson build that
     later runs reuse), the case's suite as --test-cmd, the case's suggestion patch, and
     eval/review/build_repro.sh as the reproducer command,
  3. prints a table of ground truth, expected verdict and actual verdict.

The LLM is the file-exchange backend: PATCHPROOF_LLM_EXCHANGE_DIR=<scratch>/exchange. Unanswered
requests are reported ("awaiting LLM replies: N pending requests"); answer them by writing
<hash>.reply.md next to each <hash>.request.json and run this script again. Cases run
sequentially. Re-runs reuse the build trees.
"""

from __future__ import annotations

import argparse
import json
import os
import shlex
import shutil
import subprocess
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent.parent
CASES_DIR = HERE / "e2e"
BUILD_REPRO = HERE / "build_repro.sh"
SYSTEMD_SRC = Path(os.environ.get("SYSTEMD_SRC", ROOT.parent / "systemd"))
DEFAULT_SCRATCH = Path(os.environ.get("XDG_CACHE_HOME", Path.home() / ".cache")) / "patchproof-review-e2e"
ORDER = ["strv-1", "strv-2", "cunescape", "mdns-ttl", "journal-cycle", "ellipsize-tab"]
JOBS = int(os.environ.get("JOBS", "6"))  # ninja parallelism, never more than 6
JOBS = min(JOBS, 6)

# Same build as demo/systemd-strv/run.sh (clang + ASAN), configured on first use by the test command.
SETUP = (
    '[ -f build/build.ninja ] || meson setup build -Dmode=developer -Dtests=true -Dman=false '
    "-Dtranslations=false -Db_sanitize=address -Db_lundef=false >/dev/null"
)


def sh(*args: str, cwd: Path | None = None, check: bool = True) -> subprocess.CompletedProcess:
    return subprocess.run(args, cwd=cwd, check=check, capture_output=True, text=True)


def load_cases(only: list[str] | None) -> list[dict]:
    names = [n for n in ORDER if (CASES_DIR / n / "case.json").exists()]
    if only:
        unknown = set(only) - set(names)
        if unknown:
            sys.exit(f"unknown case(s): {', '.join(sorted(unknown))}")
        names = [n for n in names if n in only]
    return [json.loads((CASES_DIR / n / "case.json").read_text()) for n in names]


def ensure_clone(scratch: Path) -> Path:
    sd = scratch / "sd"
    if not (sd / ".git").exists():
        sh("git", "clone", "--no-checkout", str(SYSTEMD_SRC), str(sd))
    return sd


def ensure_tree(scratch: Path, sd: Path, case: dict) -> Path:
    """A checkout of the head SHA; patchproof only ever reads it (--repo is never modified)."""
    tree = scratch / "trees" / case["id"]
    sha = case["head_sha"]
    if tree.exists():
        head = sh("git", "rev-parse", "HEAD", cwd=tree).stdout.strip()
        if head != sha:
            sys.exit(f"{tree} is at {head}, expected {sha}")
        return tree
    if sh("git", "cat-file", "-e", f"{sha}^{{commit}}", cwd=sd, check=False).returncode:
        # Some SHAs are force-pushed history: fetch by full SHA from the local clone.
        sh("git", "fetch", "-q", str(SYSTEMD_SRC), sha, cwd=sd)
    tree.parent.mkdir(parents=True, exist_ok=True)
    sh("git", "worktree", "add", "-q", "--detach", str(tree), sha, cwd=sd)
    return tree


def test_cmd(case: dict) -> str:
    targets = " ".join(case["build_targets"])
    # Run every suite command even when an earlier one fails, so that the output shows all of them
    # (for cunescape, test-escape failing must not hide whether test-fstab-generator fails too).
    suite = "{ fail=0; " + " ".join(f"{c} || fail=1;" for c in case["suite_commands"]) + " [ $fail = 0 ]; }"
    return " && ".join([SETUP, f"ninja -C build -j{JOBS} {targets}", suite])


def repro_cmd(case: dict) -> str:
    # Runs in the workdir tree; {path} is replaced by patchproof.
    return f"{shlex.quote(str(BUILD_REPRO))} . {case['reference_test']} {{path}}"


def patchproof_argv() -> list[str]:
    exe = os.environ.get("PATCHPROOF") or str(ROOT / ".venv" / "bin" / "patchproof")
    if Path(exe).exists() or shutil.which(exe):
        return [exe]
    return [sys.executable, "-m", "patchproof"]


def run_case(case: dict, scratch: Path, sd: Path, timeout: float, runs: int,
             extra: list[str], env: dict) -> dict:
    tree = ensure_tree(scratch, sd, case)
    cdir = CASES_DIR / case["id"]
    results = scratch / "results"
    results.mkdir(exist_ok=True)
    argv = [
        *patchproof_argv(), "review", "--json",
        "--repo", str(tree),
        "--comment-json", str(HERE / case["comment_json"]),
        "--suggestion-patch", str(cdir / case["suggestion"]["file"]),
        "--test-cmd", test_cmd(case),
        "--reproducer-path", case["reproducer_path"],
        "--reproducer-cmd", repro_cmd(case),
        "--persistent-workdir", str(scratch / "workdirs" / case["id"]),
        "--runs", str(runs), "--timeout", str(timeout),
        *extra,
    ]
    (scratch / "workdirs").mkdir(exist_ok=True)
    t0 = time.monotonic()
    proc = subprocess.run(argv, capture_output=True, text=True, env=env)
    wall = time.monotonic() - t0
    (results / f"{case['id']}.stderr.txt").write_text(proc.stderr)
    try:
        report = json.loads(proc.stdout)
    except json.JSONDecodeError:
        report = {"verdict": "ERROR",
                  "summary": f"no JSON from patchproof (exit {proc.returncode}): "
                             f"{(proc.stderr or proc.stdout).strip()[-300:]}",
                  "evidence": {}, "timings": {}}
    report["wall_seconds"] = round(wall, 1)
    (results / f"{case['id']}.json").write_text(json.dumps(report, indent=2) + "\n")
    return report


def pending_requests(exchange: Path) -> list[Path]:
    if not exchange.is_dir():
        return []
    return sorted(
        p for p in exchange.glob("*.request.json")
        if not p.with_name(p.name.replace(".request.json", ".reply.md")).exists()
    )


def suite_cell(report: dict) -> str:
    s = report.get("evidence", {}).get("suite", {})
    if not s:
        return "-"
    return f"head {s.get('head_passed', '?')}, sugg {s.get('suggestion_passed', '?')}"


def actual_summary(report: dict) -> str:
    ev = report.get("evidence", {})
    summary = report.get("summary", "")
    if "awaiting a reply" in summary or "awaiting a reply" in str(ev.get("reproducer", {}).get("error", "")):
        return "LLM unavailable / awaiting reply"
    return summary


def table(rows: list[list[str]], header: list[str]) -> str:
    rows = [header, *rows]
    widths = [max(len(r[i]) for r in rows) for i in range(len(header))]
    lines = []
    for n, r in enumerate(rows):
        lines.append("  ".join(c.ljust(widths[i]) for i, c in enumerate(r)).rstrip())
        if n == 0:
            lines.append("  ".join("-" * w for w in widths))
    return "\n".join(lines)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--only", nargs="+", metavar="CASE", help="run only these cases")
    ap.add_argument("--scratch", type=Path, default=Path(os.environ.get("PATCHPROOF_E2E_SCRATCH", DEFAULT_SCRATCH)))
    ap.add_argument("--timeout", type=float, default=3600.0,
                    help="patchproof --timeout per command (the first call includes the full build)")
    ap.add_argument("--runs", type=int, default=1, help="patchproof --runs (default 1: the build is slow)")
    ap.add_argument("--max-attempts", type=int, default=3)
    args = ap.parse_args()

    scratch: Path = args.scratch
    exchange = scratch / "exchange"
    exchange.mkdir(parents=True, exist_ok=True)
    sd = ensure_clone(scratch)

    env = dict(os.environ)
    env["PATCHPROOF_LLM_EXCHANGE_DIR"] = str(exchange)
    if shutil.which("ccache"):
        # Six trees of one source base: share compiled objects between them.
        env.setdefault("CCACHE_DIR", str(scratch / "ccache"))
        env.setdefault("CCACHE_BASEDIR", str(scratch))
        env.setdefault("CCACHE_NOHASHDIR", "1")
        env.setdefault("CC", "ccache clang")
        env.setdefault("CXX", "ccache clang++")
    else:
        env.setdefault("CC", "clang")
        env.setdefault("CXX", "clang++")
    env.setdefault("REPRO_TIMEOUT", "60")
    env["JOBS"] = str(JOBS)

    cases = load_cases(args.only)
    rows = []
    for case in cases:
        print(f"[run_e2e] {case['id']} ...", file=sys.stderr, flush=True)
        report = run_case(case, scratch, sd, args.timeout, args.runs,
                          ["--max-attempts", str(args.max_attempts)], env)
        gt = case["ground_truth"]
        rows.append([
            case["id"], f"{gt['outcome']}/{gt['claim_class']}", case["expected_verdict"],
            report["verdict"], suite_cell(report), f"{report['wall_seconds']:.0f}s",
            actual_summary(report),
        ])
        print(f"[run_e2e] {case['id']}: {report['verdict']} in {report['wall_seconds']}s",
              file=sys.stderr, flush=True)

    print()
    print(table(rows, ["case", "ground truth", "expected", "actual", "suite (passed runs)", "wall", "summary"]))
    print()
    ok = sum(r[2] == r[3] for r in rows)
    print(f"{ok}/{len(rows)} verdicts match the expectation")
    pend = pending_requests(exchange)
    if pend:
        print(f"awaiting LLM replies: {len(pend)} pending requests in {exchange}")
        return 3
    print(f"results: {scratch / 'results'}")
    return 0 if ok == len(rows) else 1


if __name__ == "__main__":
    sys.exit(main())
