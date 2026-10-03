"""Run shell commands with a hard timeout and captured output."""

from __future__ import annotations

import contextlib
import os
import re
import signal
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path

MAX_OUTPUT = 400_000


@dataclass
class RunResult:
    returncode: int
    output: str
    duration: float
    timed_out: bool = False

    @property
    def passed(self) -> bool:
        return self.returncode == 0 and not self.timed_out

    def summary(self) -> dict:
        return {
            "returncode": self.returncode,
            "passed": self.passed,
            "timed_out": self.timed_out,
            "duration": round(self.duration, 3),
        }


def clip(text: str, limit: int = MAX_OUTPUT) -> str:
    if len(text) <= limit:
        return text
    half = limit // 2
    return text[:half] + "\n...[output truncated]...\n" + text[-half:]


_SANITIZER = re.compile(r"^.*ERROR: \w*Sanitizer.*$", re.M)


def tail(text: str, n: int = 2500) -> str:
    """The last ``n`` chars, except that a sanitizer report is shown from its ERROR line on
    (its trailing shadow-memory legend says nothing)."""
    if len(text) <= n:
        return text
    m = _SANITIZER.search(text)
    if m:
        return text[m.start() : m.start() + n] + ("..." if m.start() + n < len(text) else "")
    return "..." + text[-n:]


def run_env() -> dict[str, str]:
    env = dict(os.environ)
    # Stale bytecode would otherwise make same-size, same-second mutants invisible.
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    env.pop("PYTEST_CURRENT_TEST", None)
    return env


def run_command(cmd: str, cwd: Path, timeout: float) -> RunResult:
    start = time.monotonic()
    proc = subprocess.Popen(
        cmd,
        shell=True,
        cwd=cwd,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        stdin=subprocess.DEVNULL,
        env=run_env(),
        start_new_session=True,
    )
    timed_out = False
    try:
        out, _ = proc.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        timed_out = True
        with contextlib.suppress(ProcessLookupError):
            os.killpg(proc.pid, signal.SIGKILL)
        out, _ = proc.communicate()
    text = (out or b"").decode("utf-8", errors="replace")
    text = text.replace(str(cwd), "<repo>")
    return RunResult(
        returncode=proc.returncode if proc.returncode is not None else -1,
        output=clip(text),
        duration=time.monotonic() - start,
        timed_out=timed_out,
    )
