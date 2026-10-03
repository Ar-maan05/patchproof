# End-to-end cases for `patchproof review`

One directory per case, each with `case.json` (SHA, build targets, suite commands, reference test, reproducer path, ground truth, expected verdict) and `suggestion.patch` (the suggestion, with a note in `case.json` on how it was derived). `python3 eval/review/run_e2e.py` runs them all sequentially against a scratch clone of systemd, with the LLM served through the file-exchange backend (`PATCHPROOF_LLM_EXCHANGE_DIR`, see the top-level README). Answer each `<hash>.request.json` with a `<hash>.reply.md` and run the script again; builds are kept warm in `<scratch>/workdirs`.

`eval/review/build_repro.sh <tree> <reference-test> <file.c>` compiles a standalone C reproducer with the compile command of an existing meson test, links it with that test's link command, and runs it (a hang counts as a failed assertion). It is what `--reproducer-cmd` calls, so no `meson.build` is edited.

The suggestion patches are not what the writer sees: the writer gets the review comment's claim and the code, never the patch.
