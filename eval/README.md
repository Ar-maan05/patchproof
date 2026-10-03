# patchproof eval corpus

A labelled corpus of bug-fix patches plus two scripts: one that checks the labels are true (without patchproof), and one that scores patchproof against them.

## Case format

```
cases/<case_id>/
  base/             unpatched code, plain files (flat modules; tests live in tests/)
  fix.patch         git-format diff, a/ b/ prefixes, paths relative to base/
  case.toml         test_cmd, expected_verdict, category, source, notes, [repro_mode]
  partial.patch     PARTIAL only: the essential hunk(s) alone
  mutant.patch      WEAK_TEST: a different change that also passes the test
  hidden_test.py    wrong_fix: a test that passes on a correct fix and fails on head
```

`fix.patch` includes the regression test (a new `tests/test_*.py`). patchproof applies test-file changes to the base run as well, so the base run is "base + tests". Test commands start with `python -m pytest`; the harnesses substitute a concrete interpreter for the leading `python`.

`case.toml` fields: `test_cmd`, `expected_verdict` (PROVEN, WEAK_TEST, NO_OP, PARTIAL, CANNOT_REPRODUCE, FLAKY), `category`, `source` (`synthetic` or `adapted-from-<url>`), `notes` (why the verdict is correct), and for CANNOT_REPRODUCE a `repro_mode` of `base_import_error` or `head_fails`.

## What each label means in observable terms

| expected | base + tests | head | extra evidence checked by validate_corpus.py |
|---|---|---|---|
| PROVEN | fails (behavioural) | passes | none; assertions are exact so changed lines are covered |
| NO_OP | passes | passes | none |
| PARTIAL | fails | passes | >= 2 non-test hunks; `partial.patch` also passes |
| WEAK_TEST | fails (behavioural) | passes | `mutant.patch` passes; wrong_fix: `hidden_test.py` fails on head |
| CANNOT_REPRODUCE | ImportError (`base_import_error`) | passes | or head still fails (`head_fails`) |
| FLAKY | any | both outcomes within 40 runs | |

## Distribution (33 cases)

| expected verdict | cases | categories |
|---|---|---|
| PROVEN | 10 | real_fix 10 |
| NO_OP | 7 | noop 7 (reorder, impossible-branch guard, fix in dead duplicate, logging-only, equivalent refactor, unused mutable default, test that misses the bug) |
| PARTIAL | 4 | partial 4 (real fix + unrelated defensive hunk) |
| WEAK_TEST | 7 | weak_test 4 (no-exception / trivially-true assertions), wrong_fix 3 (headline: plausible but semantically wrong fixes) |
| CANNOT_REPRODUCE | 3 | wrong_reason 2 (ImportError of symbol/module the patch adds), still_failing 1 |
| FLAKY | 2 | flaky 2 (str hash-seed ordering, unseeded random jitter) |

## Running

Both scripts need an interpreter with pytest. From the repo root:

```
uv run --python 3.14 --with pytest python eval/validate_corpus.py [--only GLOB] [--jobs N] [-v]
uv run --python 3.14 --with pytest python eval/run_eval.py [--only GLOB] [--jobs N] [--cli CMD]
```

`validate_corpus.py` does not use patchproof. It checks each patch applies with `git apply` and that the observed pytest results match the expected verdict. Every case must pass before it is scored.

`run_eval.py` finds the CLI as `uv run --project <patchproof root> patchproof`, falling back to `patchproof` on PATH; `--cli "CMD"` overrides. Each case runs on a throwaway copy of `base/`. Per-case harness timeout `--timeout` (default 300s); `--tool-timeout` is forwarded to `patchproof --timeout`; `--runs` and `--no-mutation` are forwarded too. A missing CLI, crash, timeout or unparsable output is recorded as `ERROR`. Output: confusion matrix, per-verdict precision/recall, accuracy, flag precision/recall (flagged = any verdict other than PROVEN; the false-PROVEN rate on bad patches is the number that matters most), mean/median time, and the misclassified cases with the tool's summary. Full results go to `eval/results/<timestamp>.json` (git-ignored).

## Held-out set and issue.md

`heldout/cases/` (same format, 16 cases: 3 PROVEN, 2 NO_OP, 1 PARTIAL, 4 weak_test, 5 wrong_fix, 1 CANNOT_REPRODUCE) is for measuring generalization after tuning against `cases/`; the tool's developer should not read it. Select it with `--cases-dir eval/heldout/cases` on either script. Some cases (all wrong_fix, several PROVEN) ship an `issue.md`: a bug report that does not describe the fix. `run_eval.py --with-issue` passes `--issue <case>/issue.md` to patchproof when the file exists.

## Adding a case

1. Create `cases/<id>/base/` with the buggy code, and write the fix plus a regression test under `tests/` as a working-tree change.
2. Produce `fix.patch` with `git diff` so paths are `a/...`, `b/...` relative to `base/`.
3. Write `case.toml` (copy an existing one) and any `partial.patch`, `mutant.patch`, `hidden_test.py` the verdict requires.
4. Run `validate_corpus.py --only <id>`; keep each test run under 2 seconds.

## Caveats

- The corpus is synthetic and small. Per-class numbers on 2-10 cases are anecdotes, not statistics.
- Author bias: the same person wrote the cases and the tool, and the cases were written to match the verdict definitions. Expect inflated scores; the ambiguous and adversarial cases are where the tool is really being tested.
- Labels are defined by observable behaviour (see the table), not by an independent oracle. A case where the label is debatable (e.g. what counts as "most mutants survive") should be treated as a spec question for the tool, not a bug.
- Everything is Python + pytest, flat modules, small, deterministic apart from the FLAKY cases; real repositories bring import structure, slow suites, fixtures and environment effects that this corpus does not.

## Planned real-world additions

- BugsInPy: real fixes with their regression tests (expected PROVEN, with some tests that turn out weak).
- SWE-bench: patches known to overfit (pass the shipped tests, fail the hidden tests), as natural WEAK_TEST / wrong_fix cases.
- systemd history: fixes with and without regression tests (C, so a different language backend is needed).
