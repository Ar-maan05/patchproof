"""Measure the second-opinion judge on its own, with labelled consistent/inconsistent tests.

The end-to-end eval cannot measure the judge's strictness: when the test writer is accurate,
nothing reaches the judge on a good fix. This feeds the judge tests whose label is known (does the
issue entail the asserted value or not) and scores its answers.

    python eval/judge_eval.py emit  --dataset D.json --exchange DIR   # write judge requests
    (someone answers DIR/<key>.request.json with DIR/<key>.reply.md, seeing only the prompt)
    python eval/judge_eval.py score --dataset D.json --exchange DIR

Dataset items: {seed_id, test_source, failure, label: consistent|inconsistent, kind, rationale},
plus a seeds file {id, issue, interface} next to it (--seeds, default seeds.json beside D.json).
The answerer never sees labels, kinds or rationales: only the judge prompt patchproof would send.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter, defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from patchproof.llm import request_key  # noqa: E402
from patchproof.second_opinion import judge_messages, parse_judgement  # noqa: E402


def load(args: argparse.Namespace) -> list[tuple[dict, list[dict]]]:
    items = json.loads(args.dataset.read_text())
    seeds_path = args.seeds or args.dataset.with_name("seeds.json")
    seeds = {s["id"]: s for s in json.loads(seeds_path.read_text())}
    out = []
    for it in items:
        s = seeds[it["seed_id"]]
        out.append(
            (it, judge_messages(s["issue"], s["interface"], it["test_source"], it["failure"]))
        )
    return out


def emit(args: argparse.Namespace) -> int:
    args.exchange.mkdir(parents=True, exist_ok=True)
    n = 0
    for _, msgs in load(args):
        req = args.exchange / f"{request_key(msgs)}.request.json"
        if not req.exists():
            req.write_text(json.dumps({"messages": msgs}, indent=2, ensure_ascii=False))
            n += 1
    print(f"{n} request(s) written to {args.exchange}")
    return 0


def score(args: argparse.Namespace) -> int:
    tally: Counter = Counter()
    by_kind: dict[str, Counter] = defaultdict(Counter)
    wrong = []
    missing = 0
    for it, msgs in load(args):
        reply = args.exchange / f"{request_key(msgs)}.reply.md"
        if not reply.exists():
            missing += 1
            continue
        said, reason = parse_judgement(reply.read_text(errors="replace"))
        truth = it["label"] == "consistent"
        key = ("accept" if said else "reject") + "_" + it["label"]
        tally[key] += 1
        by_kind[it.get("kind", "?")]["right" if said == truth else "wrong"] += 1
        if said != truth:
            wrong.append((it, reason))
    inc = tally["accept_inconsistent"] + tally["reject_inconsistent"]
    con = tally["accept_consistent"] + tally["reject_consistent"]
    print(f"answered {inc + con}, missing {missing}")
    if inc:
        print(f"inconsistent tests rejected (strictness): {tally['reject_inconsistent']}/{inc}"
              f" = {tally['reject_inconsistent'] / inc:.0%}")  # fmt: skip
    if con:
        print(f"consistent tests accepted (keeps real catches): {tally['accept_consistent']}/{con}"
              f" = {tally['accept_consistent'] / con:.0%}")  # fmt: skip
    print("\nby kind:")
    for kind, c in sorted(by_kind.items()):
        print(f"  {kind:<14} {c['right']}/{c['right'] + c['wrong']} right")
    if wrong:
        print(f"\nwrong judgements ({len(wrong)}):")
        for it, reason in wrong:
            print(f"  [{it['seed_id']} {it['label']}/{it.get('kind')}] judge: {reason[:140]}")
    if args.json:
        args.json.write_text(json.dumps({"tally": tally, "by_kind": by_kind}, indent=2))
    return 0


def main() -> int:
    p = argparse.ArgumentParser(description="Score the second-opinion judge on labelled tests.")
    p.add_argument("mode", choices=["emit", "score"])
    p.add_argument("--dataset", type=Path, required=True)
    p.add_argument("--seeds", type=Path)
    p.add_argument("--exchange", type=Path, required=True)
    p.add_argument("--json", type=Path, help="score: also write the tallies here")
    args = p.parse_args()
    return emit(args) if args.mode == "emit" else score(args)


if __name__ == "__main__":
    raise SystemExit(main())
