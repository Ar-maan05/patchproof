#!/usr/bin/env bash
# Build and run a standalone C reproducer against an existing meson build, without touching
# meson.build.
#
#   build_repro.sh <tree> <reference-test-name> <file.c>
#
# <tree> is a configured source tree (it must contain build/, the meson build directory).
# <reference-test-name> is an existing test target of that build, for example test-strv. The
# reproducer is compiled with the exact compile command of that test (include paths, defines,
# sanitizer flags, taken from build/compile_commands.json) and linked with the exact link command
# of that test (same libsystemd-shared, same rpaths), with only the object file and the output
# name swapped. The result is run from <tree>. Environment: REPRO_TIMEOUT (seconds, default 60),
# JOBS (ninja parallelism for the reference target, default 6), BUILD_DIR (default build).
#
# A hang is reported as a failed assertion (exit 134): to a reproducer, a call that never returns
# is a behavioural failure, not a harness error.
set -euo pipefail

if [ "$#" -ne 3 ]; then
    echo "usage: $0 <tree> <reference-test-name> <file.c>" >&2
    exit 64
fi
tree=$1 ref=$2 file=$3
B=${BUILD_DIR:-build}
JOBS=${JOBS:-6}
REPRO_TIMEOUT=${REPRO_TIMEOUT:-60}

cd "$tree"
[ -f "$file" ] || { echo "error: reproducer $file not found in $tree" >&2; exit 64; }

# Makes sure libsystemd-shared and whatever else the reference test links are up to date.
ninja -C "$B" -j"$JOBS" "$ref" >&2

out="$B/patchproof-review-repro"
obj="$B/patchproof-review-repro.o"
rm -f "$out" "$obj"

python3 - "$B" "$ref" "$file" "$obj" "$out" <<'PY'
import json, os, shlex, subprocess, sys

build, ref, src, obj, out = sys.argv[1:6]
build_abs = os.path.abspath(build)

# 1. The compile command of the reference test.
db = json.load(open(os.path.join(build, "compile_commands.json")))
# The reference test's own object lives in <ref>.p/ (meson names it <ref>.p/<dir>_<file>.c.o).
entry = next((e for e in db if e.get("output", "").startswith(f"{ref}.p/")
              and e["output"].endswith(".c.o")), None)
if entry is None:
    sys.exit(f"error: no compile_commands.json entry for an object of {ref}")
argv = shlex.split(entry["command"])
new, i = [], 0
while i < len(argv):
    a = argv[i]
    if a in ("-o", "-MF", "-MQ", "-MT"):
        i += 2
        continue
    if a == "-MD":
        i += 1
        continue
    if a == "-c":
        i += 2  # drops the source operand too
        continue
    new.append(a)
    i += 1
new += ["-o", os.path.relpath(os.path.abspath(obj), entry["directory"]),
        "-c", os.path.abspath(src)]
print("[build_repro] compile:", shlex.join(new), file=sys.stderr)
r = subprocess.run(new, cwd=entry["directory"])
if r.returncode:
    sys.exit(r.returncode)

# 2. The link command of the reference test, with our object and output swapped in.
cmds = subprocess.run(["ninja", "-C", build, "-t", "commands", ref], check=True,
                      capture_output=True, text=True).stdout.strip().splitlines()
link = cmds[-1]
ref_obj = entry["output"]
if ref_obj not in link or f" -o {ref} " not in link + " ":
    sys.exit(f"error: cannot find {ref_obj} / '-o {ref}' in the link command: {link}")
link = link.replace(ref_obj, os.path.relpath(os.path.abspath(obj), build_abs), 1)
link = (link + " ").replace(f" -o {ref} ", f" -o {os.path.relpath(os.path.abspath(out), build_abs)} ", 1)
print("[build_repro] link:", link, file=sys.stderr)
r = subprocess.run(["bash", "-c", link], cwd=build_abs)
sys.exit(r.returncode)
PY

echo "[build_repro] running $out" >&2
set +e
timeout -k 5 "$REPRO_TIMEOUT" "$out"
rc=$?
set -e
if [ "$rc" -eq 124 ]; then
    echo "Assertion 'reproducer returned within ${REPRO_TIMEOUT}s' failed (hang)." >&2
    exit 134
fi
exit "$rc"
