"""Command line interface: ``patchproof review``, ``check`` and ``gen-test``."""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

from . import __version__
from .pipeline import CheckConfig, Report, check, error_report
from .verdict import exit_code


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="patchproof",
        description="Is this review suggestion actually necessary? Does this patch do anything?",
    )
    p.add_argument("--version", action="version", version=f"patchproof {__version__}")
    sub = p.add_subparsers(dest="command", required=True)

    r = sub.add_parser("review", help="is a review comment's suggestion actually necessary?")
    r.add_argument(
        "--repo",
        required=True,
        type=Path,
        help="checkout of the PR head (the code under review; never modified)",
    )
    r.add_argument(
        "--test-cmd",
        required=True,
        help="the project's test command; exit 0 = pass; run in a copy of --repo",
    )
    src = r.add_mutually_exclusive_group(required=True)
    src.add_argument(
        "--comment-json",
        type=Path,
        metavar="FILE",
        help="one review comment object, as returned by `gh api repos/O/R/pulls/comments/ID`",
    )
    src.add_argument(
        "--github",
        metavar="OWNER/REPO#PR",
        help="fetch the comment with `gh api` (read-only); needs --comment-id",
    )
    r.add_argument("--comment-id", type=int, help="comment id for --github")
    r.add_argument(
        "--suggestion-patch",
        type=Path,
        metavar="FILE",
        help="unified diff to test instead of the comment's suggestion block(s)",
    )
    r.add_argument(
        "--reproducer",
        type=Path,
        metavar="FILE",
        help="hand-written reproducer of the claim (skips the LLM)",
    )
    r.add_argument(
        "--reproducer-path",
        metavar="RELPATH",
        help="where the reproducer is placed in the tree "
        "(default for Python: tests/test_patchproof_review.py)",
    )
    r.add_argument(
        "--reproducer-cmd",
        metavar="TEMPLATE",
        help="command that builds and runs the reproducer; {path} is replaced by "
        "--reproducer-path (default for pytest: the --test-cmd prefix + -x -q {path})",
    )
    r.add_argument("--max-attempts", type=int, default=3, help="reproducer attempts (LLM)")
    r.add_argument(
        "--no-reachability-check",
        action="store_true",
        help="do not reject reproducers that use private names or patch the module",
    )
    r.add_argument("--runs", type=int, default=3, help="runs per side, to detect flakiness")
    r.add_argument("--timeout", type=float, default=300.0, help="seconds per command run")
    r.add_argument(
        "--persistent-workdir",
        type=Path,
        metavar="DIR",
        help="reuse ONE working copy in DIR (incremental builds); see `check`",
    )
    out = r.add_mutually_exclusive_group()
    out.add_argument("--json", action="store_true", help="print one JSON object to stdout")
    out.add_argument(
        "--reply",
        action="store_true",
        help="print a short Markdown reply to paste under the comment (never posted anywhere)",
    )
    r.add_argument("-v", "--verbose", action="store_true", help="progress on stderr")

    c = sub.add_parser("check", help="prove (or disprove) that a patch is load-bearing")
    c.add_argument("--repo", required=True, type=Path, help="directory with the BASE code")
    c.add_argument("--patch", required=True, type=Path, help="unified diff (git format)")
    c.add_argument("--test-cmd", help="shell command; exit 0 = pass; run in the workdir")
    c.add_argument("--runs", type=int, default=3, help="runs per side, to detect flakiness")
    c.add_argument("--json", action="store_true", help="print one JSON object to stdout")
    c.add_argument("--format", choices=["text", "json"], default=None)
    c.add_argument("--no-mutation", action="store_true", help="skip mutation testing")
    c.add_argument("--timeout", type=float, default=300.0, help="seconds per test-command run")
    c.add_argument("--mutation-threshold", type=float, default=0.5,
                   help="survival ratio above which the verdict is WEAK_TEST")  # fmt: skip
    c.add_argument("--max-mutants", type=int, default=30)
    c.add_argument("--no-assertion-analysis", action="store_true",
                   help="skip the per-test assertion-strength analysis")  # fmt: skip
    c.add_argument("--min-mutants", type=int, default=5,
                   help="fewer valid mutants than this: mutation is 'inconclusive'")  # fmt: skip
    c.add_argument("--second-opinion", action="store_true",
                   help="LLM writes independent tests from --issue (never sees the patch); "
                   "also enabled by PATCHPROOF_SECOND_OPINION=1")  # fmt: skip
    c.add_argument("--min-confirmations", type=int, default=1,
                   help="confirmed failing independent tests needed to flag the fix")  # fmt: skip
    c.add_argument("--gen-test", action="store_true",
                   help="ask an LLM for a reproducer when --test-cmd is not given")  # fmt: skip
    c.add_argument("--issue", type=Path, help="issue text for --gen-test")
    c.add_argument("--persistent-workdir", type=Path, metavar="DIR",
                   help="reuse ONE working copy in DIR for every run (base, head, each ablation "
                   "subset, each mutant) instead of fresh temp copies, so incremental builds "
                   "(ninja, make) stay warm. Sequential only; runs share state. Created from "
                   "--repo on first use; touched files are restored afterwards")  # fmt: skip
    c.add_argument("--exclude", action="append", default=[], metavar="PATTERN",
                   help="extra fnmatch pattern not to copy from --repo (repeatable)")  # fmt: skip
    c.add_argument("-v", "--verbose", action="store_true", help="progress on stderr")

    g = sub.add_parser("gen-test", help="ask an LLM for a failing reproducer test")
    g.add_argument("--repo", required=True, type=Path)
    g.add_argument("--patch", required=True, type=Path)
    g.add_argument("--issue", type=Path)
    g.add_argument("--out", type=Path, help="write the test file here instead of stdout")
    g.add_argument("--max-attempts", type=int, default=3)
    g.add_argument("-v", "--verbose", action="store_true")
    return p


def _emit(report: Report, as_json: bool) -> int:
    if as_json:
        sys.stdout.write(json.dumps(report.to_dict(), indent=2) + "\n")
    else:
        from rich.console import Console

        from .report import render_card

        render_card(report, Console())
    return exit_code(report.verdict)


def _cmd_check(args: argparse.Namespace, as_json: bool) -> int:
    log = (
        (lambda m: print(f"[patchproof] {m}", file=sys.stderr))
        if args.verbose
        else (lambda m: None)
    )
    try:
        patch_text = args.patch.read_text(encoding="utf-8", errors="surrogateescape")
    except OSError as e:
        return _emit(error_report(f"cannot read --patch: {e}"), as_json)
    test_cmd = args.test_cmd
    gen_ev = None
    if not test_cmd:
        if not args.gen_test:
            print("patchproof check: --test-cmd is required (or use --gen-test)", file=sys.stderr)
            return 2
        from .llm import LLMError, generate_reproducer

        issue = args.issue.read_text(errors="replace") if args.issue else ""
        try:
            gt = generate_reproducer(args.repo, patch_text, issue, log=log)
        except LLMError as e:
            return _emit(error_report(f"gen-test failed: {e}"), as_json)
        patch_text = patch_text.rstrip("\n") + "\n" + gt.as_patch()
        test_cmd = gt.test_cmd
        gen_ev = {"path": gt.path, "validated": gt.validated, "attempts": gt.attempts,
                  "notes": gt.notes, "content": gt.content}  # fmt: skip
    explicit_so = args.second_opinion
    second_opinion = explicit_so or os.environ.get("PATCHPROOF_SECOND_OPINION", "") in (
        "1", "true", "yes",
    )  # fmt: skip
    if explicit_so and not args.issue:
        print("patchproof check: --second-opinion needs --issue FILE", file=sys.stderr)
        return 2
    issue_text = ""
    if args.issue:
        try:
            issue_text = args.issue.read_text(errors="replace")
        except OSError as e:
            return _emit(error_report(f"cannot read --issue: {e}"), as_json)
    cfg = CheckConfig(
        repo=args.repo,
        patch_text=patch_text,
        test_cmd=test_cmd,
        runs=args.runs,
        mutation=not args.no_mutation,
        timeout=args.timeout,
        mutation_threshold=args.mutation_threshold,
        max_mutants=args.max_mutants,
        min_mutants=args.min_mutants,
        assertion_analysis=not args.no_assertion_analysis,
        second_opinion=second_opinion,
        issue_text=issue_text,
        min_confirmations=args.min_confirmations,
        persistent_workdir=args.persistent_workdir,
        exclude=tuple(args.exclude),
        log=log,
    )
    report = check(cfg)
    if gen_ev:
        report.evidence["generated_test"] = gen_ev
    return _emit(report, as_json)


def _cmd_gen_test(args: argparse.Namespace) -> int:
    from .llm import LLMError, generate_reproducer

    log = (
        (lambda m: print(f"[patchproof] {m}", file=sys.stderr))
        if args.verbose
        else (lambda m: None)
    )
    try:
        patch_text = args.patch.read_text(encoding="utf-8", errors="surrogateescape")
        issue = args.issue.read_text(errors="replace") if args.issue else ""
        gt = generate_reproducer(
            args.repo, patch_text, issue, max_attempts=args.max_attempts, log=log
        )
    except (OSError, LLMError) as e:
        print(f"patchproof gen-test: {e}", file=sys.stderr)
        return 2
    if args.out:
        args.out.write_text(gt.content)
    else:
        sys.stdout.write(gt.content)
    status = "validated (fails on base, passes with patch)" if gt.validated else "NOT validated"
    print(f"[patchproof] {gt.path}: {status} after {gt.attempts} attempt(s)", file=sys.stderr)
    print(f"[patchproof] run with: {gt.test_cmd}", file=sys.stderr)
    return 0 if gt.validated else 1


def _cmd_review(args: argparse.Namespace) -> int:
    from .github import GithubError, fetch_comment
    from .review import ReviewConfig, error_report, review
    from .review_report import render_card, render_reply
    from .review_verdict import exit_code as review_exit_code
    from .suggestion import Comment, SuggestionError

    log = (
        (lambda m: print(f"[patchproof] {m}", file=sys.stderr))
        if args.verbose
        else (lambda m: None)
    )

    def emit(report) -> int:
        if args.json:
            sys.stdout.write(json.dumps(report.to_dict(), indent=2) + "\n")
        elif args.reply:
            sys.stdout.write(render_reply(report))
        else:
            from rich.console import Console

            render_card(report, Console())
        return review_exit_code(report.verdict)

    try:
        if args.github:
            if args.comment_id is None:
                print("patchproof review: --github needs --comment-id", file=sys.stderr)
                return 2
            data = fetch_comment(args.github, args.comment_id)
        else:
            data = json.loads(args.comment_json.read_text(encoding="utf-8"))
        comment = Comment.from_json(data)
        patch_text = (
            args.suggestion_patch.read_text(encoding="utf-8", errors="surrogateescape")
            if args.suggestion_patch
            else None
        )
        reproducer = args.reproducer.read_text(encoding="utf-8") if args.reproducer else None
    except (OSError, ValueError, GithubError, SuggestionError) as e:
        return emit(error_report(f"cannot read the inputs: {e}"))
    cfg = ReviewConfig(
        repo=args.repo,
        comment=comment,
        test_cmd=args.test_cmd,
        suggestion_patch=patch_text,
        reproducer=reproducer,
        reproducer_path=args.reproducer_path,
        reproducer_cmd=args.reproducer_cmd,
        runs=args.runs,
        timeout=args.timeout,
        persistent_workdir=args.persistent_workdir,
        max_attempts=args.max_attempts,
        reachability=not args.no_reachability_check,
        log=log,
    )
    return emit(review(cfg))


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    if args.command == "review":
        return _cmd_review(args)
    if args.command == "check":
        as_json = args.json or args.format == "json"
        return _cmd_check(args, as_json)
    return _cmd_gen_test(args)


if __name__ == "__main__":
    sys.exit(main())
