"""Fetch a review comment with ``gh api`` (read-only; nothing is ever posted)."""

from __future__ import annotations

import json
import re
import subprocess


class GithubError(RuntimeError):
    pass


def run_gh(endpoint: str) -> str:
    """GET ``endpoint`` through the gh CLI. The only place patchproof talks to GitHub."""
    try:
        r = subprocess.run(
            ["gh", "api", endpoint],
            capture_output=True,
            text=True,
            timeout=60,
            stdin=subprocess.DEVNULL,
        )
    except FileNotFoundError as e:
        raise GithubError("the gh CLI is not installed") from e
    except subprocess.TimeoutExpired as e:
        raise GithubError("gh api timed out") from e
    if r.returncode != 0:
        raise GithubError(f"gh api {endpoint} failed: {r.stderr.strip() or r.stdout.strip()}")
    return r.stdout


_SPEC = re.compile(r"^([\w.-]+)/([\w.-]+)#(\d+)$")


def parse_spec(spec: str) -> tuple[str, str, int]:
    m = _SPEC.match(spec.strip())
    if not m:
        raise GithubError(f"--github must look like OWNER/REPO#PR, got {spec!r}")
    return m.group(1), m.group(2), int(m.group(3))


def fetch_comment(spec: str, comment_id: int) -> dict:
    """The review comment (``pulls/comments``), else the issue-level comment, as a dict."""
    owner, repo, pr = parse_spec(spec)
    errors = []
    for kind in ("pulls", "issues"):
        endpoint = f"repos/{owner}/{repo}/{kind}/comments/{comment_id}"
        try:
            data = json.loads(run_gh(endpoint))
        except GithubError as e:
            errors.append(str(e))
            continue
        except ValueError as e:
            raise GithubError(f"gh api {endpoint} did not return JSON: {e}") from e
        if not isinstance(data, dict):
            raise GithubError(f"gh api {endpoint} did not return a comment object")
        url = str(data.get("pull_request_url") or data.get("issue_url") or "")
        if url and not url.rstrip("/").endswith(f"/{pr}"):
            raise GithubError(f"comment {comment_id} belongs to {url}, not to PR #{pr}")
        return data
    raise GithubError("; ".join(errors))
