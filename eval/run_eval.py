#!/usr/bin/env python3
"""Run `patchproof check --json` over the eval corpus and score the verdicts.

Usage:
  uv run --python 3.14 --with pytest python eval/run_eval.py [--only GLOB] [--jobs N]
         [--cli "CMD ..."] [--timeout SECONDS] [--runs N] [--no-mutation]

The test commands in cases start with `python -m pytest`; the leading `python` is
replaced by --python (default: the interpreter running this script, which must have
pytest installed) so the result does not depend on the tool's own virtualenv.
"""
from __future__ import annotations

import sys

sys.dont_write_bytecode = True

import argparse
import json
import os
import shlex
import shutil
import signal
import statistics
import subprocess
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path

from _common import CASES_DIR, EVAL_DIR, PATCHPROOF_ROOT, VERDICTS, Case, load_cases, with_python


def resolve_cli(override: str | None) -> list[str] | None:
    if override:
        return shlex.split(override)
    if shutil.which("uv") and (PATCHPROOF_ROOT / "pyproject.toml").exists():
        return ["uv", "run", "--project", str(PATCHPROOF_ROOT), "patchproof"]
    exe = shutil.which("patchproof")
    return [exe] if exe else None


def extract_json(text: str) -> dict | None:
    """Parse the JSON object from stdout, tolerating stray log lines around it."""
    text = text.strip()
    try:
        obj = json.loads(text)
        return obj if isinstance(obj, dict) else None
    except json.JSONDecodeError:
        pass
    dec = json.JSONDecoder()
    for i, ch in enumerate(text):
        if ch == "{":
            try:
                obj, _ = dec.raw_decode(text[i:])
            except json.JSONDecodeError:
                continue
            if isinstance(obj, dict) and "verdict" in obj:
                return obj
    return None


def run_case(case: Case, cli: list[str] | None, a: argparse.Namespace) -> dict:
    res = {"id": case.id, "category": case.category, "expected": case.expected_verdict,
           "actual": "ERROR", "summary": "", "seconds": 0.0, "returncode": None}
    if cli is None:
        res["summary"] = "patchproof CLI not found (use --cli or install it)"
        return res
    with tempfile.TemporaryDirectory(prefix=f"pp-eval-{case.id}-") as w:
        repo = Path(w) / "repo"
        shutil.copytree(case.base, repo)
        cmd = [*cli, "check", "--repo", str(repo), "--patch", str(case.patch.resolve()),
               "--test-cmd", with_python(case.test_cmd, a.python), "--json",
               "--timeout", str(a.tool_timeout)]
        if a.runs:
            cmd += ["--runs", str(a.runs)]
        if a.no_mutation:
            cmd.append("--no-mutation")
        issue = case.path / "issue.md"
        if a.with_issue and issue.exists():
            cmd += ["--issue", str(issue.resolve())]
        env = {**os.environ, "PYTHONDONTWRITEBYTECODE": "1"}
        t0 = time.perf_counter()
        try:
            proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                                    env=env, start_new_session=True)
        except OSError as e:
            res["summary"] = f"could not launch CLI: {e}"
            return res
        try:
            out, err = proc.communicate(timeout=a.timeout)
        except subprocess.TimeoutExpired:
            os.killpg(proc.pid, signal.SIGKILL)
            proc.communicate()
            res["seconds"] = time.perf_counter() - t0
            res["summary"] = f"harness timeout after {a.timeout}s"
            return res
        res["seconds"] = time.perf_counter() - t0
        res["returncode"] = proc.returncode
        obj = extract_json(out)
        if obj is None:
            tail = (err or out).strip().splitlines()[-3:]
            res["summary"] = f"no JSON from CLI (exit {proc.returncode}): " + " | ".join(tail)[:300]
            return res
        verdict = str(obj.get("verdict", "")).upper()
        res["actual"] = verdict if verdict in VERDICTS else "ERROR"
        res["summary"] = str(obj.get("summary", ""))
        if verdict not in VERDICTS:
            res["summary"] = f"unrecognised verdict {obj.get('verdict')!r}: {res['summary']}"
        res["timings"] = obj.get("timings")
        res["raw"] = obj
    return res


def prf(tp: int, fp: int, fn: int) -> tuple[float | None, float | None]:
    return (tp / (tp + fp) if tp + fp else None, tp / (tp + fn) if tp + fn else None)


def fmt(x: float | None) -> str:
    return "  n/a" if x is None else f"{x:5.2f}"


def score(results: list[dict]) -> dict:
    labels = [v for v in VERDICTS if any(r["expected"] == v or r["actual"] == v for r in results)]
    matrix = {e: {a: 0 for a in labels} for e in labels}
    for r in results:
        matrix[r["expected"]][r["actual"]] += 1
    per = {}
    for v in labels:
        tp = matrix[v][v]
        fp = sum(matrix[e][v] for e in labels if e != v)
        fn = sum(matrix[v][a] for a in labels if a != v)
        p, rcl = prf(tp, fp, fn)
        per[v] = {"precision": p, "recall": rcl, "support": tp + fn}
    n = len(results)
    correct = sum(r["expected"] == r["actual"] for r in results)
    # binary: positive class = "patch is bad" = anything except PROVEN
    tp = sum(r["expected"] != "PROVEN" and r["actual"] != "PROVEN" for r in results)
    fp = sum(r["expected"] == "PROVEN" and r["actual"] != "PROVEN" for r in results)
    fn = sum(r["expected"] != "PROVEN" and r["actual"] == "PROVEN" for r in results)
    p, rcl = prf(tp, fp, fn)
    bad = sum(r["expected"] != "PROVEN" for r in results)
    times = [r["seconds"] for r in results]
    return {
        "labels": labels, "matrix": matrix, "per_verdict": per,
        "accuracy": correct / n if n else None, "n": n, "correct": correct,
        "flag": {"precision": p, "recall": rcl, "tp": tp, "fp": fp, "fn": fn,
                 "false_proven_rate": fn / bad if bad else None},
        "errors": sum(r["actual"] == "ERROR" for r in results),
        "time": {"mean": statistics.fmean(times) if times else None,
                 "median": statistics.median(times) if times else None,
                 "total": sum(times)},
    }


def report(results: list[dict], s: dict) -> None:
    labels = s["labels"]
    w = max([len(x) for x in labels] + [8]) + 1
    print("\nConfusion matrix (rows = expected, columns = actual)")
    print(" " * (w + 2) + "".join(f"{a[:w - 1]:>{w}}" for a in labels))
    for e in labels:
        print(f"{e:<{w + 2}}" + "".join(f"{s['matrix'][e][a]:>{w}}" for a in labels))
    print("\nPer-verdict          precision  recall  support")
    for v in labels:
        d = s["per_verdict"][v]
        print(f"  {v:<18} {fmt(d['precision'])}     {fmt(d['recall'])}   {d['support']:>5}")
    print(f"\nOverall accuracy: {s['correct']}/{s['n']} = {s['accuracy']:.1%}" if s["n"] else "\nNo results")
    f = s["flag"]
    print("\nBinary flagging (positive = patch is bad = verdict other than PROVEN; ERROR counts as flagged)")
    print(f"  precision {fmt(f['precision'])}  recall {fmt(f['recall'])}  "
          f"(TP {f['tp']}, FP {f['fp']} good patches flagged, FN {f['fn']} bad patches passed as PROVEN)")
    if f["false_proven_rate"] is not None:
        print(f"  false-PROVEN rate on bad patches: {f['false_proven_rate']:.1%}")
    print(f"  ERROR verdicts: {s['errors']}")
    if s["time"]["mean"] is not None:
        print(f"\nTime per case: mean {s['time']['mean']:.2f}s, median {s['time']['median']:.2f}s, "
              f"total {s['time']['total']:.1f}s (summed across jobs)")
    bad = [r for r in results if r["expected"] != r["actual"]]
    print(f"\nMisclassified ({len(bad)}):")
    for r in bad:
        print(f"  {r['id']}: expected {r['expected']}, got {r['actual']}\n      {r['summary'][:200]}")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--cli", help='command to invoke patchproof, e.g. "uv run --project ../patchproof patchproof"')
    ap.add_argument("--cases-dir", default=str(CASES_DIR), help="corpus directory (default eval/cases; e.g. eval/heldout/cases)")
    ap.add_argument("--with-issue", action="store_true", help="pass --issue <case>/issue.md to patchproof when the file exists")
    ap.add_argument("--only", help="glob on case id, e.g. 'proven_*'")
    ap.add_argument("--jobs", type=int, default=2)
    ap.add_argument("--timeout", type=float, default=300, help="harness timeout per case (seconds)")
    ap.add_argument("--tool-timeout", type=float, default=60, help="value passed to patchproof --timeout")
    ap.add_argument("--runs", type=int, default=0, help="pass --runs N to patchproof (0 = tool default)")
    ap.add_argument("--no-mutation", action="store_true")
    ap.add_argument("--python", default=sys.executable, help="interpreter substituted for `python` in test commands")
    ap.add_argument("--results-dir", default=str(EVAL_DIR / "results"))
    a = ap.parse_args()

    cases = load_cases(a.only, Path(a.cases_dir).resolve())
    if not cases:
        print("no cases matched")
        return 2
    cli = resolve_cli(a.cli)
    print("CLI:", " ".join(cli) if cli else "NOT FOUND (all cases will be recorded as ERROR)")

    with ThreadPoolExecutor(a.jobs) as ex:
        futs = [ex.submit(run_case, c, cli, a) for c in cases]
        results = []
        for fut in futs:
            r = fut.result()
            results.append(r)
            mark = "ok  " if r["expected"] == r["actual"] else "MISS"
            print(f"{mark} {r['id']:<48} expected {r['expected']:<17} got {r['actual']:<17} {r['seconds']:5.1f}s")

    s = score(results)
    report(results, s)

    out_dir = Path(a.results_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    out = out_dir / (datetime.now().strftime("%Y%m%dT%H%M%S") + ".json")
    out.write_text(json.dumps({"cli": cli, "args": vars(a), "score": s, "results": results}, indent=2, default=str))
    print(f"\nResults written to {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
