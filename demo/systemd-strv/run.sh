#!/usr/bin/env bash
# Headline demo: patchproof on the systemd strv_rebreak_lines() out-of-bounds read (issue #43052).
#
#   demo/systemd-strv/run.sh [WORKDIR]
#
# Environment:
#   SYSTEMD_SRC   a systemd git clone to clone from (default: a fresh clone of upstream)
#   PATCHPROOF    command to run patchproof (default: the venv script from "uv sync")
#   MAX_MUTANTS   cap on mutants per patch (default: 12)
#   JOBS          ninja parallelism (default: 6)
#
# Needs: git, meson, ninja, clang with AddressSanitizer runtime (gcc without libasan is not
# enough), and the usual systemd build dependencies. Nothing outside WORKDIR is modified; if
# WORKDIR is omitted a fresh mktemp directory is used and printed.
set -euo pipefail

HERE=$(cd "$(dirname "$0")" && pwd)
ROOT=$(cd "$HERE/../.." && pwd)
WORK=${1:-$(mktemp -d -t patchproof-systemd-XXXXXX)}
SYSTEMD_SRC=${SYSTEMD_SRC:-https://github.com/systemd/systemd.git}
if [ -z "${PATCHPROOF:-}" ]; then
    # Call the venv script directly: "uv run" would put the venv first on PATH, and the test
    # command (meson) would then pick up its python instead of the system one.
    uv sync --project "$ROOT" -q
    PATCHPROOF=$ROOT/.venv/bin/patchproof
fi
MAX_MUTANTS=${MAX_MUTANTS:-12}
JOBS=${JOBS:-6}
BASE=c925e405f3   # parent of the fix: "pcrextend: skip measurement gracefully when the TPM can't be used"

mkdir -p "$WORK"
echo "[demo] workdir: $WORK"

# 1. A scratch clone at the base commit. --no-checkout + checkout keeps it cheap with a local source.
if [ ! -d "$WORK/systemd/.git" ]; then
    git clone --no-checkout "$SYSTEMD_SRC" "$WORK/systemd"
fi
git -C "$WORK/systemd" checkout -q "$BASE"

# 2. The test command. It configures the (ASAN, clang) build the first time, then every call is
#    an incremental ninja build of just test-strv followed by running it. patchproof's persistent
#    workdir keeps ./build between runs, so only the files a patch or mutant touches recompile.
TEST_CMD="[ -f build/build.ninja ] || CC=clang CXX=clang++ meson setup build -Dmode=developer \
-Dtests=true -Dman=false -Dtranslations=false -Db_sanitize=address -Db_lundef=false >/dev/null \
&& ninja -C build -j$JOBS test-strv && build/test-strv"

check() {  # check NAME PATCH
    local name=$1 patch=$2 start end
    start=$(date +%s)
    # $PATCHPROOF is intentionally unquoted: it is a command line.
    # shellcheck disable=SC2086
    $PATCHPROOF check \
        --repo "$WORK/systemd" \
        --patch "$patch" \
        --persistent-workdir "$WORK/patchproof-workdir" \
        --test-cmd "$TEST_CMD" \
        --max-mutants "$MAX_MUTANTS" \
        --timeout 90 \
        -v 2>"$WORK/$name.log" | tee "$HERE/$name.verdict.txt" || true
    end=$(date +%s)
    echo "[demo] $name: $((end - start))s wall clock (progress log: $WORK/$name.log)"
}

# 3. Check the two versions of the fix against the same base. The first call pays for the
#    initial full build; the second reuses it.
check v1-noop "$HERE/v1-noop.patch"
check real-fix "$HERE/real-fix.patch"
