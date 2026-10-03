# patchproof

**Is this review suggestion actually necessary?**

Review bots and humans leave comments like "this can overflow when X, suggest adding a bounds check" or "UAF: `service` is used after `revisit_cache()` frees it". Some are real bugs. Many are defensive scope creep for situations no caller can create. As the author you need evidence, not a debate.

`patchproof review` takes the review comment and your checkout of the PR head, and answers with a verdict. It writes a reproducer of the reviewer's CLAIM without ever seeing the suggested change, makes it drive the code only through the project's real entry points, and runs it on the PR head and on the head with the suggestion applied.

```
$ patchproof review --repo ./pr-head --comment-json comment.json --test-cmd "pytest -x -q"
╭─────────────────────── NECESSARY  SUGGESTION IS NECESSARY ───────────────────────╮
│ the reproducer fails on the PR head and passes with the suggestion applied       │
│                                                                                  │
│ comment   review-bot on textutil.py:2                                            │
│ claim     first_word('') raises IndexError instead of returning ''.              │
│ suggest   lines 2-2 -> 2 line(s)                                                 │
│ normalise textutil.py: differs (ast)                                             │
│ suite     head: 2/2 passed   with suggestion: 2/2 passed                         │
│ repro     generated, tests/test_patchproof_review.py                             │
│           head: 0/2 passed   with suggestion: 2/2 passed                         │
│                                                                                  │
│ reproducer failure on the PR head (tail):                                        │
│ text = '   '                                                                     │
│                                                                                  │
│     def first_word(text):                                                        │
│ >       return text.split()[0]                                                   │
│                ^^^^^^^^^^^^^^^                                                   │
│ E       IndexError: list index out of range                                      │
│                                                                                  │
│ textutil.py:2: IndexError                                                        │
│ =========================== short test summary info ============================ │
│ FAILED tests/test_patchproof_review.py::test_blank_input - IndexError: list i... │
│ !!!!!!!!!!!!!!!!!!!!!!!!!! stopping after 1 failures !!!!!!!!!!!!!!!!!!!!!!!!!!! │
│ 1 failed in 0.01s                                                                │
╰────────────────────────────────────── 1.2s ──────────────────────────────────────╯
```

patchproof is two tools on one engine. `review` (this half) judges a reviewer's suggestion. [`check`](#checking-a-fix-is-load-bearing-check) (second half) is the same machinery pointed the other way: it judges whether a bug-fix patch is load-bearing.

## Origin story

The author's own systemd PRs got review comments from a review bot. Some were real: one was a reset-path out-of-bounds read that a second review caught. Many were scope creep, defensive checks for situations no caller can create, and taking them all would have ballooned the PR. Telling the two apart meant arguing from the code each time. patchproof turns that into evidence: either a test that fails on the PR as it stands for the reason the comment gives, or a record of honest attempts that could not make it fail through the public API.

## How `review` works

```
 PR head (--repo) + review comment (claim, suggestion) + --test-cmd
        |
        v
 [1] parse the comment: claim text | ```suggestion blocks -> unified diff
        |
        v
 [2] normalise: is the suggestion equivalent to the code it replaces?
        |-- yes (same AST / same tokens) .............. NO_BEHAVIOUR_CHANGE, stop
        v
 [3] the project's test command on HEAD and on HEAD+suggestion (--runs each)
        |-- passes on head, fails with the suggestion ... HARMFUL
        |-- results differ between runs ................ FLAKY
        v
 [4] reproducer of the CLAIM (LLM, or --reproducer), up to 3 attempts:
        reachability check, then it must fail on HEAD for a behavioural reason
        |-- no valid failing reproducer ................ NOT_SHOWN
        v
 [5] the reproducer on HEAD+suggestion (--runs times)
        |-- no suggestion, or it still fails ........... CLAIM_CONFIRMED
        |-- passes every run ........................... NECESSARY
```

The mapping is: base = the PR head as it is, patch = the reviewer's suggestion, test = a reproducer of the reviewer's claim written without seeing the suggestion.

- **Suggestion parsing.** A GitHub ```` ```suggestion ```` block replaces lines `start_line..line` (or just `line`) on the RIGHT side of `path` with the block's content; an empty block deletes those lines. The block becomes a unified diff against the file in `--repo` (CRLF files stay CRLF, a missing final newline is handled). The claim is the comment body with the blocks removed. Several blocks with identical content count as one; blocks that differ are an error (they target the same range, so it is ambiguous which one to test). `--suggestion-patch FILE` overrides all of this, for comments that describe a change in prose. Issue-level comments (no `path`) are accepted: they have a claim but no anchored code and no suggestion.
- **The writer never sees the suggestion.** The prompt holds the claim, the anchored file with line numbers around the commented range, the comment's `diff_hunk`, and the project's existing test file for the module when a conventional name (`test_<module>.py`, `test-<module>.c`, ...) is found. Replies that fail get feedback (rejected by the reachability rule, passes on head, fails for the wrong reason such as an import or compile error) and another try, up to `--max-attempts` (default 3). Failure classification is the one `check` uses, so an AddressSanitizer report or `assert()` abort counts as behavioural. Any `ChatFn` works, including the file-exchange backend (`PATCHPROOF_LLM_EXCHANGE_DIR`, see below).
- **The project's own suite.** `--test-cmd` always runs on the head and, when there is a suggestion, on the head with the suggestion applied. A suggestion that turns a green suite red is `HARMFUL`, whatever the reproducer says.
- **`--repo` is never modified.** Everything runs in copies (or in the `--persistent-workdir` tree, where the reproducer file is tracked and removed again).

### The reachability rule

A reproducer may only drive the code through the project's real entry points, not by calling internals with arguments no caller can produce. Otherwise "a bounds check is missing" is trivially provable by passing the helper a value nothing ever passes it. The rule is in the writer's prompt and checked statically on every reply (also on a hand-written `--reproducer`; `--no-reachability-check` turns it off). A violation counts as a failed attempt, and the reason goes into the evidence.

- **Python:** rejected if the reproducer imports a name with a leading underscore from the module under review (`from pkg.mod import _helper`), uses one as an attribute of that module or of an object (`mod._helper`, `Box()._inner()` when the module defines `_inner`), reaches one through `getattr`, or monkeypatches, mocks (`mock.patch`, `patch.object`) or assigns into the module under review. Dunder names are fine, and so are underscore helpers the reproducer defines itself.
- **C:** rejected if it calls a function declared `static` in the reviewed file (found with a regex over the file) or `#include`s that `.c` file to get at them. Functions the reproducer defines itself are fine.
- **Other languages:** no static check; only a hand-written reproducer makes sense there.

The limits, honestly:

- It is a heuristic. Public functions can still be called with inputs real callers never pass, and nothing here detects that. A reproducer that passes the public API a `NULL` no caller sends is accepted.
- It is lexical. Private access through `importlib`, `vars()`, `eval`, a re-export under a public name, or a C wrapper that exposes a static function is not seen. A name-only match can also reject a legitimate call (a public method that happens to share a name with a private one in the reviewed file).
- It only protects against the reproducer cheating on the module under review. If the bug lives in another module, that module is not checked.
- So `NOT_SHOWN` is evidence, not proof. A better reproducer may exist than the ones the model found in 3 attempts.

### Review verdicts

| Verdict | Meaning |
| --- | --- |
| `NECESSARY` | The reproducer fails on the PR head for a behavioural reason (every run) and passes with the suggestion applied (every run). Without the suggestion the claim is real and this change is what fixes it. |
| `CLAIM_CONFIRMED` | The reproducer fails on the PR head, but either the comment has no suggestion (prose only), or the suggestion does not make it pass. The summary says which: "the comment gives no suggestion to test" versus "the suggestion does not fix it". The claim is real; this change is not shown to be the fix. |
| `NOT_SHOWN` | No valid reproducer could make the head fail after the attempts (rejected by the reachability rule, passed on head, or failed for a wrong reason). The claim was not reproduced through public entry points, which is evidence, not proof, that the suggestion is unnecessary. |
| `NO_BEHAVIOUR_CHANGE` | The suggestion is equivalent to the code it replaces after normalisation: Python, identical `ast.dump` ignoring docstrings and comments; C and other languages, identical token stream after stripping comments and whitespace. Checked first; the reproducer stage is skipped. |
| `HARMFUL` | The project's test command passes on the PR head and fails with the suggestion applied. |
| `FLAKY` | Repeated runs of the suite or the reproducer disagree. |
| `ERROR` | patchproof could not run: unreadable comment, a suggestion that does not apply, no reproducer command for the project, LLM unreachable, internal error. |

Priority when several apply: `ERROR > HARMFUL > FLAKY > NECESSARY > CLAIM_CONFIRMED > NOT_SHOWN`, and `NO_BEHAVIOUR_CHANGE` short-circuits before any of them. Exit code: `0` for `NECESSARY` and `CLAIM_CONFIRMED`, `1` for the rest, `2` for `ERROR` and usage errors.

Not necessary does not mean wrong: a `NOT_SHOWN` defensive check may still be good hygiene. The point is that you can decide with the evidence in front of you.

### Input and usage

```
patchproof review --repo DIR --test-cmd "CMD" \
                  (--comment-json FILE | --github OWNER/REPO#PR --comment-id ID) \
                  [--suggestion-patch FILE] [--reproducer FILE] [--reproducer-path RELPATH] \
                  [--reproducer-cmd TEMPLATE] [--json | --reply] [--runs N] [--timeout S] \
                  [--persistent-workdir DIR] [--max-attempts N] [--no-reachability-check] [-v]
```

- `--repo` is a checkout of the PR head (the code being reviewed). It is never modified.
- `--comment-json` is one pull-request review comment object, exactly as `gh api repos/O/R/pulls/comments/ID` returns it. Only `body`, `path`, `line`, `start_line`, `original_line`, `side`, `commit_id`, `diff_hunk`, `html_url` and `user.login` are read. An outdated comment (null `line`) falls back to `original_line`.
- `--github OWNER/REPO#PR --comment-id ID` fetches the same object with `gh api` (read-only, falling back to the issue-comment endpoint). patchproof never posts anything anywhere.
- `--test-cmd` is the project's suite, run in the workdir through the shell; exit 0 = pass.
- `--reproducer FILE` supplies a hand-written reproducer and skips the LLM. `--reproducer-path` is where it goes in the tree (default for Python: `tests/test_patchproof_review.py`, or the repo root without a `tests/` directory).
- `--reproducer-cmd` is the command that builds and runs it, with `{path}` replaced by the path. The default, when `--test-cmd` is a plain `pytest` command, is its prefix plus `-x -q {path}`.
- `--runs` (default 3) applies to every side: the suite and the reproducer are each run that many times on head and on head+suggestion, so a slow suite wants `--runs 1`.
- The LLM is configured as for `check`: `PATCHPROOF_LLM_BASE_URL`, `PATCHPROOF_LLM_MODEL`, `PATCHPROOF_LLM_API_KEY`, or `PATCHPROOF_LLM_EXCHANGE_DIR` (see "File-exchange backend" below).

Output is a verdict card, `--json` (one object with `verdict`, `summary`, `evidence`, `hunks`, `timings`; the evidence holds the parsed claim and suggestion, normalisation, suite runs, every reproducer attempt with its outcome, the accepted reproducer and its failure output), or `--reply`, a short plain-ASCII Markdown reply you could paste under the review comment: the verdict, one paragraph of evidence, and the reproducer in a code block. `--reply` only prints; nothing is posted.

### C and other compiled projects

C reproducer support is deliberately basic: the reproducer is written to `--reproducer-path` and your `--reproducer-cmd` must build and run it, with that path picked up by the build. patchproof does not know your build system. For a meson project that might be a test source you add to `meson.build` beforehand and:

```
patchproof review --repo ./pr-head --comment-json c.json \
    --test-cmd "ninja -C build && meson test -C build" \
    --reproducer-path src/test/test-patchproof-review.c \
    --reproducer-cmd "ninja -C build test-patchproof-review && build/test-patchproof-review" \
    --persistent-workdir /tmp/pp-review --runs 1 --timeout 120
```

Use `--persistent-workdir` so the build stays incremental (the same trade-offs as in `check`, below). A sanitizer report or `assert()` abort on the head counts as a behavioural failure. Without `--reproducer-path` for C the run stops with an error: only your build knows where a test must live.

### Evaluation on real review comments

`eval/review/` holds 35 real review comments from the author's own PRs (systemd and lance, mostly from review bots), each labelled with what actually happened: applied, declined with a reason, unresolved or superseded. Six systemd comments that can be tested with an existing test binary were run end to end (`eval/review/run_e2e.py`). Each PR head is built with clang and AddressSanitizer at the commit the comment was made on. The reproducers were written by Claude Sonnet as a blind agent through the file-exchange backend: it saw each prompt (the comment, the anchored code, the existing test file) and nothing else, never the suggestion. Writing them took up to 3 attempts with compiler and test feedback.

| comment | what happened | patchproof |
| --- | --- | --- |
| strv: the `cw < 0` branch is dead, the OOB read remains | real bug, fixed | `NECESSARY` |
| strv: `invalid` is stale after the line-break reset | real bug, fixed | `NECESSARY` |
| cunescape: use `unichar_is_valid()` in the `\u` case | wrong, declined (breaks `test-fstab-generator`) | `HARMFUL` |
| `ellipsize_mem` emits a UTF-8 ellipsis for ASCII with a tab in the C locale | real bug, left unfixed | `NECESSARY` |
| mDNS maintenance re-arm ignores the capped TTL | real bug, fixed | `CLAIM_CONFIRMED` (miss) |
| journal hole punching loops forever on a hash cycle | real bug, fixed | `NOT_SHOWN` (miss) |

4 of 6 match. The two strv reproducers are the cases that matter for the origin story. The second one is the heap string `"a \xF0" "b"` at width 3, the same input that exposed the second out-of-bounds read in July, written blind from the comment. The `ellipsize_mem` reproducer shows the bug through the public API: under `LC_ALL=C`, `"abcd...xyz"` without a tab and `"abcd<U+2026>yz"` (a UTF-8 ellipsis) with one.

Both misses are claims that need internal state to trigger, and they show where the method breaks:

- **mDNS (`CLAIM_CONFIRMED` instead of `NECESSARY`).** The reproducer built a `DnssdDiscoveredService` by hand (`new0()` plus field assignments) instead of reaching it through the browse path. That state is not what real callers produce, so the test failed with the real fix applied too. The reachability check only rejects calls to `static` functions; it does not notice fabricated internal state. A stricter rule (no direct construction of the module's structs) would have rejected this attempt.
- **journal (`NOT_SHOWN`, a false "unnecessary" signal).** Triggering the loop needs a journal file with a data-hash cycle, that is a deliberately corrupted file. Three attempts could not build one through public entry points: one did not compile and two passed on the head. This is the failure mode to remember: `NOT_SHOWN` only means the writer could not reproduce the claim, and for bugs that need corrupted or rare state it says little.

The six cases are the author's own PRs, where the bots were mostly right. One negative case (cunescape) is not enough to measure how often `NOT_SHOWN` is right about a suggestion being unnecessary. Two of the suggestion patches (cunescape, ellipsize) were written from the comment text, because no upstream fix exists, and several comments spell the fix out in prose, so the writer was not fully blind on those. Per-case results are in `eval/review/e2e/results/`.

### Limits of `review`

- **Evidence, not proof**, in both directions. `NECESSARY` shows that one reproducer separates head from head+suggestion; it does not show the suggestion is the best fix or that the reproducer is a realistic input. `NOT_SHOWN` shows that a model, in a few attempts, could not break the code through its public API.
- **The reproducer is only as good as the claim and the writer.** A vague claim, a weak local model or an API it has to guess from one file gives `NOT_SHOWN` for real bugs. The writer sees one file and one test file, not the whole project.
- **The suite check is as strong as the suite.** `HARMFUL` needs the existing tests to cover the code the suggestion touches; a suggestion that breaks untested behaviour is not caught.
- **Normalisation is syntactic.** Reordered independent statements, `x != 0` versus `0 != x`, or an equivalent rewrite are reported as changes. Token-stream comparison for non-Python code treats comment syntax by file extension.
- **Reachability is a heuristic** (see above), and only Python and C get a static check.
- **Cost.** The suite runs `2 x --runs` times and the reproducer up to `--runs` times per side; a slow or non-incremental build makes that expensive.
- **No evaluation yet.** Unlike `check` there is no labelled corpus behind these verdicts; the end-to-end tests use tiny fixture repos with a mocked model.

## Checking a fix is load-bearing (`check`)

`check` is the same engine pointed the other way: it takes a bug-fix patch, a repository and a test command, and gives an evidence-backed verdict on whether the patch is *load-bearing*: does the test fail without the fix, pass with it, execute the fixed lines, and notice when those lines are broken?

```
$ patchproof check --repo ./base --patch fix.patch --test-cmd "pytest -x -q"
╭───────────── PROVEN  PATCH IS LOAD-BEARING ─────────────╮
│ test fails without the fix, passes with it              │
│                                                         │
│ runs      base: 0/3 passed   head: 3/3 passed           │
│ coverage  1/1 changed code hunks executed on head       │
│           base failure passes through calc.py:2         │
│ mutation  5 killed, 0 survived (threshold 50% survival) │
╰───────────────────────── 3.0s ──────────────────────────╯
```

### Why `check` exists

An AI-generated review suggestion on a systemd PR looked right on inspection: the diff was tidy, the explanation was plausible, the new code read as correct. It was a no-op. The changed code never changed behaviour, and only CI caught it. Maintainers are now drowning in AI-generated PRs, and "looks right to me" does not scale. They need evidence, not vibes: *show me the test failing before, passing after, and show me the fix is what flipped it.*

### Prior art

patchproof is a practical packaging of ideas that are well established in research:

- **Automated program repair (APR) and patch correctness assessment.** The APR literature has studied *overfitting patches* for years: patches that make the given tests pass without fixing the bug. Techniques include test generation for differential checking, and plausibility versus correctness analysis.
- **Known SWE-bench problems.** Several studies found SWE-bench "resolved" patches that pass the benchmark tests while being semantically wrong or incomplete, because the tests are weak.
- **Mutation testing and delta debugging.** Mutation score as a test-strength signal, and ddmin (Zeller and Hildebrandt) for minimising failure-inducing or, here, fix-inducing change sets.

What patchproof adds is a CLI (and, later, a GitHub Action) that a reviewer can run on a single PR, with a small fixed vocabulary of verdicts, each backed by recorded evidence, and hunk-level ablation that says which parts of a patch the test actually depends on.

### Pipeline

```
 repo (BASE) + patch + test-cmd
        |
        v
 [1] split patch: test-file hunks | code hunks         (path heuristics)
        |
        v
 [2] BASE = base + test hunks      HEAD = base + full patch      (temp copies)
        |
        v
 [3] run test N times on BASE and on HEAD
        |-- BASE passes ............................... NO_OP
        |-- BASE fails for a trivial reason ........... CANNOT_REPRODUCE
        |      (SyntaxError, ImportError, collection error, NameError on
        |       a symbol the patch adds, compile error, timeout)
        |-- HEAD fails ................................ CANNOT_REPRODUCE
        |-- results differ between runs ............... FLAKY
        v
 [4] coverage (Python): does the failing test reach the patched files/functions
     on BASE? are the patch's added/removed lines executed on HEAD?
        |-- no changed line ever runs ................. NO_OP
        v
 [5] per-test outcomes (pytest junit XML): which tests flip fail -> pass?
     AST assertion-strength analysis of exactly those tests
        |-- every flipping test asserts only weak things ... WEAK_TEST
        v
 [6] hunk ablation (ddmin): smallest set of code hunks that makes the test pass
        |-- a strict subset is enough ................. PARTIAL
        v
 [7] mutation testing on the changed lines of the needed hunks
        |-- too many mutants survive (>= --min-mutants valid) ... WEAK_TEST
        |-- fewer than --min-mutants valid mutants ........ "inconclusive" (no verdict change)
        v
 [8] optional --second-opinion: LLM tests written from the issue only
        |-- a confirmed independent test fails on head ..... WEAK_TEST
        v
      PROVEN
```

Priority when several findings apply: `ERROR > CANNOT_REPRODUCE > FLAKY > NO_OP > WEAK_TEST > PARTIAL > PROVEN`. An inconclusive mutation result is not a finding at all.

### Check verdicts

| Verdict | Meaning |
| --- | --- |
| `PROVEN` | The test fails on base (for a behavioural reason), passes with the patch on every run, the changed lines execute, every code hunk is needed, and the test notices mutations of those lines. |
| `WEAK_TEST` | The test flips, but it does not pin the fix down. Any of: (a) every test that flips fail -> pass makes only weak assertions (`evidence.assertion_strength`); (b) at least `--min-mutants` valid mutants of the changed lines were run and more than `--mutation-threshold` (default 0.5) survived (survivors listed with file, line, description); (c) with `--second-opinion`, an independent LLM-written test that the issue implies fails on head (`evidence.second_opinion`). |
| `NO_OP` | Either the test passes without the fix, or the patch's changed lines are never executed by the test (or are comments only). |
| `PARTIAL` | A strict subset of the code hunks is enough to make the test pass. The others are reported as `not_exercised`. This is not "useless": they may be defensive, or covered by a test you did not run. |
| `CANNOT_REPRODUCE` | The test fails on base only for a wrong reason (import/syntax/collection/compile error, a symbol only the patch adds), or still fails with the patch applied. |
| `FLAKY` | Repeated runs of base or head disagree. |
| `ERROR` | patchproof could not run: bad repo, patch does not apply, patch touches only tests, internal error. |

#### Assertion-strength analysis

patchproof runs the test command once more on base and on head with `--junitxml` (only when the command is a plain `pytest ...` / `python -m pytest ...`; `-x`/`--maxfail` are dropped for that run), and computes the *discriminating* tests: those that fail or error on base and pass on head. Only their assertions matter. Each is parsed with `ast` and graded:

- `STRONG`: equality/inequality against an expected value (`== 3`, `== []`, `is None`, `is True`, `== f(y)`), `pytest.raises` with a specific exception type (or any type plus `match=`), a specific member (`"k" in r`) or length (`len(r) == 3`), a bound against a concrete value, a mock call assertion, a call to a predicate (`assert is_valid(x)`), or a helper assertion function. unittest-style `self.assertX` calls are mapped onto the same rules and helper functions defined in the same test module are inlined.
- `WEAK`: no assertion at all; only `isinstance`/`type(x) == T`/`callable`/`hasattr`; `is not None`/`!= None`/`!= 0`/`!= ""`; bare truthiness of a name (`assert result`); `len(x) >= 0` and other tautologies; a value compared with itself (`assert r == r`) or two constants; `or True` disjunctions and `x is None or isinstance(x, T)` (a disjunction is only as strong as its weakest branch); `pytest.raises(Exception)` with no `match`; a `try`/`except` that swallows or merely `pytest.fail`s on a raise.
- `UNKNOWN`: the test function could not be located or parsed. Never counts as weak.

A test is `STRONG` if any of its checks is strong (an `and` of a weak and a strong check is strong). The verdict is `WEAK_TEST` only when every discriminating test is `WEAK`:

```json
"assertion_strength": {
  "tests/test_kvparse.py::test_bare_key_does_not_crash":
    {"class": "WEAK", "reasons": ["checks only the type of the result"]}
}
```

The grading is syntactic, and conservative on purpose: when in doubt a check counts as strong, because wrongly demoting a good test costs more than missing a weak one. Known blind spots: `assert parse(x)` (truthiness of a call) is treated as a predicate and counted strong; fixtures and helpers in other modules are not followed; expected values read from data files look "concrete" even if they are trivial. If the command is not plain pytest, the analysis is unavailable (`evidence.assertion_strength_unavailable`) and never demotes. Disable it with `--no-assertion-analysis`.

#### Mutation-evidence floor

Mutating a tiny patch yields a handful of mutants, and 1 of 2 surviving says nothing. With fewer than `--min-mutants` (default 5) valid mutants (killed + survived; invalid ones do not count) the mutation result is `status: "inconclusive"`. Decision for verdict priority: inconclusive mutation **never changes the verdict**. It cannot demote to `WEAK_TEST` (too little data) and it is not counted as support for `PROVEN`: the verdict then rests on the other stages (fail -> pass, coverage, ablation, assertion strength), and a `PROVEN` card says `(mutation inconclusive: N valid mutants)` in its summary and on the mutation line. `--no-mutation` still reports `skipped`, not inconclusive.

#### Second-opinion tests (`--second-opinion`, LLM, opt-in)

```
patchproof check --repo DIR --patch FILE --test-cmd "pytest -q" --second-opinion --issue issue.md
```

The environment switch `PATCHPROOF_SECOND_OPINION=1` enables it too (then a missing `--issue` just skips it instead of being a usage error). Configure the endpoint with `PATCHPROOF_LLM_BASE_URL`, `PATCHPROOF_LLM_MODEL`, `PATCHPROOF_LLM_API_KEY` (any OpenAI-compatible server; `<think>` blocks are stripped from replies).

1. **Independent generation.** The model receives *only* the issue text and the signatures and docstrings (extracted with `ast` from the HEAD code) of the functions the patch touches. It never receives the patch, the diff, or any function body. Private helpers that the patch adds are omitted so their names do not leak the fix's structure. It is asked for edge-case and boundary tests with specific expected values.
2. **Run on head.** The generated file runs against HEAD. A collection error gets one repair round (the error output, nothing about the patch). Tests that error, or fail with anything other than an assertion (or "DID NOT RAISE"), or whose failure `classify.py` calls non-behavioural, are dropped and listed in `evidence.second_opinion.dropped`.
3. **Judge.** For each remaining failing test the model is asked 3 times (majority vote) whether the test's expectation is *consistent with the issue text* (strict: the issue must state or entail the value). Only tests judged consistent are confirmed.
4. **Verdict.** At least `--min-confirmations` (default 1) confirmed failures give `WEAK_TEST`, summary `fix appears wrong: independent test X fails`, with the test source and failure in the evidence. The card states that this is an LLM's opinion, not proof.

Why independence matters: a fix and the test that ships with it usually come from the same mental model of the bug. If that model is wrong (semver that ignores pre-release tags, a UTF-8 truncation that emits U+FFFD, a `Retry-After` HTTP-date parsed as 0) then the fix passes its own test and mutation, ablation and coverage all agree it is "load-bearing". Only a test written from the *requirements* can disagree, and it can only do so if it was never shown the fix.

Limits: it needs an issue that states the intended behaviour, and the model must guess a matching import path and API from signatures alone. A weak or small local model may produce tests that are wrong, the judge can be fooled (it is the same kind of model), and a confirmed failure may come from an ambiguous issue. Misses are expected (nothing is flagged if the model does not think of the case). The feature can only demote `PROVEN`, never promote, and a flagged good patch is a false positive to be read by a human. If the LLM is unreachable the stage is skipped (with the reason in the evidence) and the verdict is unchanged. See "Evaluation".

Exit code: `0` for `PROVEN`, `1` for any other verdict, `2` for `ERROR` and usage errors.

### Usage

```
uv sync                      # or: pip install .
patchproof check --repo DIR --patch FILE --test-cmd "CMD" \
                 [--runs N] [--json] [--no-mutation] [--timeout SECONDS] \
                 [--mutation-threshold 0.5] [--max-mutants 30] [--min-mutants 5] \
                 [--no-assertion-analysis] \
                 [--persistent-workdir DIR] [--exclude PATTERN ...] \
                 [--second-opinion --issue FILE [--min-confirmations 1]] [-v]
patchproof check --repo DIR --patch FILE --gen-test [--issue FILE]    # LLM reproducer
patchproof gen-test --repo DIR --patch FILE [--issue FILE] [--out FILE]
```

- `--repo` is the **base** (unpatched) tree. It need not be a git repo and is never modified: it is copied into temp workdirs (without `.git`, `.venv`, caches, `node_modules`).
- `--patch` is a unified/git diff, applied with `git apply` (fallback `patch -p1`).
- `--test-cmd` runs in the workdir through the shell; exit code 0 means pass. The test can already be in the base, or be added by the patch: test-file hunks (`test_*.py`, `*_test.go`, `tests/`, `*.test.*`, ...) are applied to the BASE run as well, so "base" means *base code + the patch's test changes*.
- `--json` prints exactly one JSON object to stdout: `{"verdict", "summary", "evidence", "hunks", "timings"}`.

Python coverage wraps commands that start with `pytest` or `python` (no shell operators); other commands still get run, ablation and mutation, but not coverage.

#### Compiled projects: `--persistent-workdir DIR`

By default every run (base, head, each ablation subset, each mutant) happens in a fresh temp copy of the repo. For a C project that means a full rebuild each time. With `--persistent-workdir DIR` patchproof keeps ONE working copy in `DIR/tree` and reuses it for every run:

- `DIR` is created and seeded from `--repo` on first use (the usual ignores plus any `--exclude PATTERN`, an fnmatch pattern, repeatable). On later uses the tree is reused as is, so the build directory your test command creates (for example `build/` with meson and ninja) stays warm across stages and even across `patchproof check` invocations on the same base.
- Between runs only the files the patch touches are rewritten: put back to the base content, then the requested hunks (or the mutant) are applied. A file whose content already matches is not rewritten. Every other file keeps its mtime, so ninja or make recompiles only what actually changed.
- Before touching anything patchproof saves the original bytes of the touched files in `DIR/backup`. At the end of a check (also after an error) they are restored. If a run is killed, the next use of the same `DIR` restores them first. `--repo` itself is never modified.
- Make the test command self-sufficient: configure on first use, then build incrementally and run, for example `[ -f build/build.ninja ] || meson setup build ... && ninja -C build test-foo && build/test-foo`.

Trade-offs, read these: runs are strictly sequential; every run shares the tree and whatever the test command leaves in it (a stale build product, a test that writes files), so a test command that is not incremental-build safe can give wrong answers that a fresh copy would not; a hung test (for example an infinite loop in an ablation subset) costs a full `--timeout`, so set `--timeout` low; `DIR` must be empty or created by patchproof (anything else is refused); Python line coverage and `--second-opinion` need several trees at once and are skipped in this mode. Excluding paths from the copy is optional here: the copy happens once, so exclusion only saves that single copy.

C failures are classified too: an AddressSanitizer/UBSan/LeakSanitizer report, an `Assertion '...' failed` message, or a SIGABRT/SIGSEGV/SIGBUS/SIGFPE exit is a behavioural failure; compiler and linker errors (and ninja's `FAILED:` lines on their own) are not, so a patch that merely breaks the build is `CANNOT_REPRODUCE`, never a pass, and a mutant that does not build is `invalid`, never `killed`.

#### LLM reproducers (optional)

If the patch ships no test, `--gen-test` asks an OpenAI-compatible chat endpoint (for example a local llama.cpp server) for a pytest reproducer, validates it (must fail behaviourally on base, pass on head) and feeds failing output back to the model for up to 3 attempts.

```
export PATCHPROOF_LLM_BASE_URL=http://localhost:8080/v1
export PATCHPROOF_LLM_MODEL=local
export PATCHPROOF_LLM_API_KEY=...        # optional
```

### Headline demo: systemd strv

systemd issue #43052 is an out-of-bounds read in `strv_rebreak_lines()` (`src/basic/strv.c`): on invalid UTF-8 the loop kept stepping with `utf8_next_char()` past the terminating NUL. The first version of the fix looked right on inspection and shipped as a no-op. The loop clamps `cw = 1` on invalid UTF-8, so the new `p = cw < 0 ? p + 1 : utf8_next_char(p)` had a dead `cw < 0` branch and still over-read. Only CI's ASAN `test-strv` caught it. The real fix captures `bool invalid = cw < 0;` before the clamp and revalidates after the line-break reset repoints `p`.

Both versions are checked against the same base (`c925e405f3`, a systemd clone at that commit) with the test the fix ships (`src/test/test-strv.c`), built with clang and AddressSanitizer:

```
demo/systemd-strv/run.sh [WORKDIR]        # SYSTEMD_SRC=<local clone> to avoid a network clone
```

The script clones systemd into `WORKDIR` (a mktemp directory by default), checks out the base, and runs `patchproof check --persistent-workdir ... --max-mutants 12 --timeout 90` for `v1-noop.patch` and then `real-fix.patch`. The test command configures the meson build (`CC=clang`, `-Dmode=developer -Dtests=true -Db_sanitize=address -Db_lundef=false`) the first time and otherwise runs `ninja -C build test-strv && build/test-strv`. Needs meson, ninja, clang with the ASAN runtime and systemd's build dependencies. gcc cannot stand in: without libasan it cannot link, and a gcc build without ASAN cannot see the over-read at all (the `STRV_MAKE` literals sit in `.rodata`, so it neither crashes nor gets flagged).

The verdict cards below are the verbatim output, also committed as `demo/systemd-strv/*.verdict.txt`.

The no-op version (`v1-noop.patch`):

```
╭──────────────────── CANNOT_REPRODUCE  BUG NOT REPRODUCED ────────────────────╮
│ test still fails with the patch applied: the fix does not fix it             │
│                                                                              │
│ runs      base: 0/3 passed   head: 0/3 passed                                │
│                                                                              │
│ hunk                  status                                                 │
│ src/basic/strv.c#1    not_evaluated                                          │
│ src/basic/strv.c#2    not_evaluated                                          │
│ src/test/test-strv.c  test                                                   │
│                                                                              │
│ failure output (tail):                                                       │
│ ==100848==ERROR: AddressSanitizer: global-buffer-overflow on address         │
│ 0x00000054f1c7 at pc 0x7f82e25b2f6f bp 0x7fffd399ff10 sp 0x7fffd399ff08      │
│ READ of size 1 at 0x00000054f1c7 thread T0                                   │
│     #0 0x7f82e25b2f6e                                                        │
│ (<repo>/build/src/shared/libsystemd-shared-261.so+0x644f6e) (BuildId:        │
│ dad132441dfeba36a15e6a4e5f6188e17e81925d)                                    │
│     #1 0x000000528d39  (<repo>/build/test-strv+0x528d39) (BuildId:           │
│ 843100c6bbb88461f2f3e698d9670dcd0c75ea6d)                                    │
│     #2 0x7f82e243d549                                                        │
│ (<repo>/build/src/shared/libsystemd-shared-261.so+0x4cf549) (BuildId:        │
│ dad132441dfeba36a15e6a4e5f6188e17e81925d)                                    │
│     #3 0x00000052b9f1  (<repo>/build/test-strv+0x52b9f1) (BuildId:           │
│ 843100c6bbb88461f2f3e698d9670dcd0c75ea6d)                                    │
│     #4 0x7f82e1bfc680  (/lib64/libc.so.6+0x3680) (BuildId:                   │
│ 5bd941be836f538fe5e10eff508f7f5dd94905a6)                                    │
│     #5 0x7f82e1bfc797  (/lib64/libc.so.6+0x3797) (BuildId:                   │
│ 5bd941be836f538fe5e10eff508f7f5dd94905a6)                                    │
│     #6 0x00000042c6c4  (<repo>/build/test-strv+0x42c6c4) (BuildId:           │
│ 843100c6bbb88461f2f3e698d9670dcd0c75ea6d)                                    │
│                                                                              │
│ 0x00000054f1c7 is located 25 bytes before global variable '.str.686' defined │
│ in '../src/test/test-strv.c' (0x00000054f1e0) of size 54                     │
│   '.str.686' is ascii string 'strv_rebreak_lines(STRV_MAKE("foo\xF0"), 10,   │
│ &l) >= 0'                                                                    │
╰─────────────────────────────────── 13.7s ────────────────────────────────────╯
```

The real fix (`real-fix.patch`):

```
╭─────────────────────── PROVEN  PATCH IS LOAD-BEARING ────────────────────────╮
│ test fails without the fix, passes with it (mutation inconclusive: 4 valid   │
│ mutants)                                                                     │
│                                                                              │
│ runs      base: 0/3 passed   head: 3/3 passed                                │
│ coverage  unavailable (no Python files in the patch)                         │
│ ablation  3/3 hunks needed (6 test runs)                                     │
│ mutation  inconclusive (4 mutants, need 5): not counted as evidence          │
│                                                                              │
│ hunk                  status                                                 │
│ src/basic/strv.c#1    required                                               │
│ src/basic/strv.c#2    required                                               │
│ src/basic/strv.c#3    required                                               │
│ src/test/test-strv.c  test                                                   │
│                                                                              │
│ surviving mutants (test did not notice):                                     │
│   src/basic/strv.c:1252  `<` -> `<=`                                         │
│   src/basic/strv.c:1277  `<` -> `<=`                                         │
╰─────────────────────────────────── 195.5s ───────────────────────────────────╯
```

Runtime on this machine (16 cores, `ninja -j6`): the v1 check took 14 s including the cold clang+ASAN build of `test-strv` (the persistent tree keeps that build), and the real-fix check took 195 s. Per-stage timings were not captured, but the likely cost is hung runs: removing `utf8_next_char()` from the `for` header without the new advance step makes some ablation subsets loop forever, and each of those costs the full `--timeout 90` (two of them alone would account for about 180 s). Total about 3.5 minutes. The wall clock the script prints for the second check is not trustworthy if the machine suspends; the number on the card is patchproof's own clock.

What the results say:

- v1 is not `PROVEN`, but the verdict is `CANNOT_REPRODUCE`, not `NO_OP`. The vocabulary has no better fit: the ordering rule puts "test still fails with the patch applied" first, and that is exactly the situation. The card quotes the ASAN report on head: a `global-buffer-overflow` read at the first new test line (`"foo\xF0"`), i.e. the bug is still there. That is the CI failure, caught in 14 seconds on this machine, with the evidence on the card.
- The real fix is `PROVEN`: the test fails on base under ASAN (a behavioural failure by the C classification), passes on head 3/3, and ablation shows all three code hunks are needed (6 test runs).
- Mutation is `inconclusive`, not decisive: the regex mutator found only 4 mutants of the changed lines (fewer than `--min-mutants` 5), so the PROVEN is carried by the base/head/ablation evidence. Of the 4, two were killed and two survived: `<` -> `<=` in `bool invalid = cw < 0;` and in the revalidation after the line-break reset. They survive because the test never feeds a zero-width character (`cw == 0`), so this is a genuine, small gap in the shipped test (or two harmless equivalents on real input; patchproof cannot tell which).

What this does and does not prove. It shows the tool turns "looks right" into evidence on a real bug: the no-op version is rejected for exactly the reason CI rejected it, and the fix that works earns `PROVEN` with every hunk accounted for. It does not show the fix is complete (that is what `--second-opinion` or a fuzzer is for), and one bug is a demonstration, not a benchmark. The C side is weaker than the Python side: there is no line-coverage stage (see the roadmap), so "the changed lines execute" is not checked, and the mutation operators are regexes over source text.

### Evaluation

`eval/` holds a synthetic corpus of 33 labelled cases (run: `uv run --python 3.14 --with pytest python eval/run_eval.py --jobs 4`). The honest finding that drove the second half of this tool: **mutating the changed lines cannot catch two whole classes of bad evidence.**

- A *no-crash test* (`assert isinstance(result, dict)` after a crash fix): mutating a crash fix reintroduces the crash, so even a test that asserts nothing kills the mutant. Mutation measures whether the test notices the code changing, not whether it checks the right thing. Tiny patches also yield only 1-6 mutants.
- A *wrong fix with a passing test* (semver ignoring pre-release tags, truncation emitting U+FFFD, a `Retry-After` date returning 0): the fix lines are mutated, the test kills those mutants, and the fix is still wrong for inputs the test never tries. Mutation of the fix cannot see what the fix omits.

| | before | after (no LLM) |
| --- | --- | --- |
| accuracy | 26/33 (78.8%) | 30/33 (90.9%) |
| `WEAK_TEST` recall | 0/7 | 4/7 |
| bad patches wrongly `PROVEN` | 7/23 (30.4%) | 3/23 (13.0%) |
| "flag bad patch" precision / recall | 1.00 / 0.70 | 1.00 / 0.87 |
| good patches flagged | 0/10 | 0/10 |

The 4 no-crash cases are now caught by assertion-strength analysis. The 3 wrong-fix cases are, by construction, out of reach without an independent source of requirements, which is what `--second-opinion` is for.

#### With `--second-opinion`, and on a held-out set

`eval/heldout/` holds 16 more cases written after the tool was tuned, by an author who never saw the tool's code, and never read by the agent that wrote the assertion analysis. 16 of the 49 cases carry an `issue.md` (all 8 wrong fixes and 8 real fixes), so only those reach the second opinion. The model was Claude Sonnet, as two separate blind agents: a test writer and a judge, each served through the file-exchange backend (below) and allowed to read nothing but the prompt files.

| | main corpus (33) | held-out (16) |
| --- | --- | --- |
| no LLM | 30/33 | 11/16 |
| `--second-opinion` | 32/33 | 16/16 |
| wrong fixes caught | 3/3 | 5/5 |
| good patches flagged | 0/10 | 0/3 |

Read these numbers with their caveats:

- **The judge did no filtering in this run.** On all 8 real fixes with an issue, every test the writer produced passed, so there was nothing to judge. All 21 judged tests came from wrong fixes, and the judge accepted every one. "0 false positives" here comes from the writer, not from the judge. The judge is measured separately below.
- **Small, synthetic, single-author corpus.** 8 wrong fixes is a demo, not a benchmark.
- **The one remaining main-corpus miss** is `flaky_dedupe_set_order`. Its flakiness depends on the hash seed, so 3 runs sometimes agree and it reads as `PROVEN` or `CANNOT_REPRODUCE`.

#### The judge on its own

The end-to-end run cannot show whether the judge would stop a test that over-reaches, so `eval/judge/` holds 80 labelled tests written from the 16 issues: 32 whose expectation the issue states or entails, and 48 plausible over-reaches (an input the issue is silent on, an exception type it never names, an expectation that contradicts it, over-precise formatting, or a feature beyond the fix). The judge sees exactly the prompt patchproof sends, never the label. Run it with `python eval/judge_eval.py emit|score --dataset eval/judge/dataset.json --exchange DIR`.

| | Sonnet judge |
| --- | --- |
| over-reaching tests rejected | 46/48 (96%) |
| valid tests accepted | 32/32 (100%) |

Both misses are `silent_input` items, and one of them is arguably a labelling error. A test that a cache miss does not change LRU recency asserts values that ordinary LRU behaviour fixes anyway. The other is a real miss: the judge accepted "a page past the end returns `[]`" for an issue that never says what out-of-range pages should return. The dataset was written by one model from the issue texts alone and spot-checked by hand, so treat it as a sanity check rather than a benchmark.

#### File-exchange backend (any agent as the model)

With `PATCHPROOF_LLM_EXCHANGE_DIR=DIR`, every prompt without an answer is written to `DIR/<hash>.request.json` and that call fails as "LLM unavailable". Whoever answers (a person, a coding agent, a batch job) writes `DIR/<hash>.reply.md`, and the run is repeated. File names are content hashes, so the answerer sees the prompt and nothing else: no case names, no patch. Identical prompts share one reply, so repeated judge samples collapse to one vote. A run takes three passes: generation prompts, then judge prompts, then the verdicts.

### Limitations (read these)

- **Verdicts are evidence, not proof.** `PROVEN` means "this test, run this way, behaves as a good regression test for this patch should". It does not mean the patch is correct or complete.
- **Mutation cannot detect wrong fixes or no-crash tests** (see "Evaluation"); assertion-strength analysis catches the second, only an independent test catches the first.
- **Assertion grading is syntactic.** See "Assertion-strength analysis" for blind spots.
- **Mutants can be equivalent.** A surviving mutant may be semantically identical to the original. The default threshold tolerates some, but `WEAK_TEST` can be a false alarm on tiny patches.
- **Reproducers can overfit.** An LLM-written test may encode the patch's behaviour rather than the intended behaviour. `gen-test` output needs a human read.
- **Build cost.** Every ablation subset and every mutant is a full test-command run. In the default mode that is a fresh copy and, for compiled projects, a full rebuild; use `--persistent-workdir` for incremental builds (sequential only, shared state, see above) and cap work with `--max-mutants` and `--no-mutation`.
- **Hunks are git hunks.** Adjacent changes that git merged into one hunk are one ablation unit. Ablation also assumes failures are deterministic; a flaky subset can mislead ddmin.
- **Coverage is Python only**, and only for `pytest`/`python` commands. A `def` line running does not count as the fix running; only the body lines do. For C (the systemd demo) there is no line coverage: the executed-lines check is not decisive, so a fix whose lines never run is caught only because the test passes on base or fails on head.
- **Mutation for non-Python code is regex-based** (comparison operators, `&&`/`||`, `+1`/`-1`), ignores comments and strings, and treats mutants that fail to build as invalid, not killed.
- Docs-only (`.md`/`.rst`/`.txt`) companion files are tolerated in the executed-lines check; other non-Python changes make that check non-decisive.
- Tests that depend on an environment inside the repo (a `.venv`, built artifacts) will not see it in the temp copy.

## Roadmap

- C/meson line coverage: `--persistent-workdir` and C failure classification are in. Not done: `gcov` line coverage for the executed-lines stage. It needs a second, instrumented build tree (`-Db_coverage=true`; not tried together with ASAN, and untested here), `.gcda` collection between runs and a gcov parser in the pipeline; judged too costly for the first cut.
- A GitHub Action that runs patchproof on a PR and comments the verdict card (`review` on each bot comment, `check` on each fix).
- An evaluation corpus for `review`: labelled real and scope-creep review comments, to measure `NECESSARY` / `NOT_SHOWN` accuracy and how often the reachability rule rejects a legitimate reproducer.
- `review` reproducers for more languages than Python and C, and a reachability check that follows the call graph from the public entry points.
- More C demos and a mutation operator set that yields enough valid mutants on small C patches (the systemd demo gets 4, below the evidence floor of 5).
- Sub-hunk splitting, more mutation operators, parallel mutant execution.

## Development

```
uv sync
uv run pytest
uv run ruff check && uv run ruff format --check
```
