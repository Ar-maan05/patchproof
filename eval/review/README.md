# Review-comment corpus for `patchproof review`

This directory holds 35 real GitHub pull-request review comments that claim a defect or ask for a code change, each with a ground-truth outcome and a judgement of whether the claim was a real defect. It is data only; the tool lives elsewhere.

## Layout

- `comments/<owner>__<repo>__<pr>__<comment_id>.json`: the raw review-comment object as returned by `gh api repos/O/R/pulls/comments/ID` (the input contract for `patchproof review --comment-json`). Non-ASCII characters are written as JSON escapes, so the content is identical but the file is pure ASCII.
- `labels.json`: one record per comment with `file`, `url`, `reviewer`, `is_bot`, `has_suggestion_block`, `outcome`, `claim_class`, `justification`, `head_sha_at_comment`, `fix_sha`, `language`, `test_hint`, `e2e_candidate`, plus `repo`, `pr`, `comment_id` and `comment_commit_id` for convenience.

## Sources

All comments come from pull requests authored by the corpus maintainer (GitHub user Ar-maan05): 30 comments on 10 systemd/systemd PRs (43078, 43079, 43588, 43872, 43952, 43953, 43954, 43663, 43653, 43589) and 5 on lance-format/lance (6975, 7845). 33 are from bots (the Claude review GitHub Action on systemd, the lance-gatekeeper bot, CodeRabbit), 2 from human maintainers (yuwata, a mix of requests). Other PRs were scanned and had no suitable comments (util-linux, sqlx, lancedb, afterburn and most small systemd PRs have none), or only design requests I could not check (lightpanda-io/browser 2722).

## How it was labelled

- Comments were fetched with `gh api repos/O/R/pulls/N/comments --paginate` (GET only). Pure praise, questions, repository-process complaints (for example the README notice rule) and most wording/style nits were skipped.
- `head_sha_at_comment` is the comment's `original_commit_id`, i.e. the PR head the reviewer actually looked at. The comment's `commit_id` field (which GitHub moves forward for outdated comments) is kept as `comment_commit_id`. Many PRs were force-pushed, so intermediate SHAs are not on the PR any more; for systemd they are all readable with `git -C systemd show <sha>:<path>` in the local clone.
- `outcome`: `applied` (a later PR version made essentially that change or fixed the claimed problem; checked by diffing the force-pushed versions, and by the author's reply), `declined_with_reason` (the author explained why not; quoted or paraphrased in the justification), `superseded` (folded into another thread) and `unresolved` (no reply or change by the time of labelling, 2026-10-02). `fix_sha` is the first commit I could identify that contains the change, and null when none.
- `claim_class` is my own judgement of whether the claim was a real defect: `real_bug`, `defensive_only` (real but unreachable or hardening/test quality), `style`, `wrong` or `unknown`. Where I only read the code I say so in the justification. No comment was labelled `style` because style nits were filtered out up front.
- `e2e_candidate` is true for small, self-contained claims testable through an existing test binary, with a known head commit. `test_hint` says how to build and run the relevant test; the commands were taken from the repository layout and were not all executed.

## Counts

By outcome: applied 21, declined_with_reason 7, unresolved 6, superseded 1.

By claim_class: real_bug 24, defensive_only 6, wrong 4, unknown 1, style 0.

Cross-tab: applied/real_bug 19, applied/defensive_only 2, declined/wrong 4, declined/defensive_only 1, declined/real_bug 1, declined/unknown 1, unresolved/real_bug 3, unresolved/defensive_only 3, superseded/real_bug 1.

By language: C 28, Rust 5, shell 2 (counted by the file the comment is on).

End-to-end candidates (6): 3610928091 and 3610971331 (strv_rebreak_lines out-of-bounds), 3611094483 (suggestion that would regress test-fstab-generator, a negative case), 3903228287 (mDNS maintenance schedule), 4145779056 (journal hash chain cycle in hole punching) and 3933593047 (ellipsize_mem emits a UTF-8 ellipsis for ASCII+tab in a non-UTF-8 locale).

## Caveats

- Selection bias: these are the author's own PRs, in a small number of repositories (mostly systemd). The bot comments on a PR the author was actively iterating are mostly right, which is why `real_bug` dominates; expect a real-world precision lower than this corpus suggests.
- The labels are one person's judgement, partly derived from the author's own replies (so an `applied` label means the author agreed, not that an independent test confirmed the claim). I only read code; I did not rebuild or reproduce any claim except where a justification quotes the author doing so.
- `outcome` for PRs still open (43588, 43872, 43952, 43953, 43954) reflects the state at 2026-10-02 and can change; `unresolved` there often means not yet addressed.
- Some `fix_sha` values (for example be22d9fb27, 4b9379d19b) belong to force-pushed history that no longer appears in the PR commit list; they exist in the local systemd clone but may be unreachable on GitHub. The final PR heads sometimes restructure the fix.
- Only one comment per root thread was kept in most cases, but some threads (systemd 43588, lance 6975) have several related claims, so the records are not independent.
- Lance and Rust comments need a heavy build (protoc, cmake); none are marked e2e.
