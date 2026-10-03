"""Filesystem helpers for the static-file handler (POSIX paths)."""
import os


def safe_join(root: str, user_path: str) -> str:
    """Join `user_path` under `root`, refusing anything that escapes it."""
    root = os.path.abspath(root)
    target = os.path.abspath(os.path.join(root, user_path))
    if not target.startswith(root):
        raise ValueError(f"path escapes root: {user_path!r}")
    return target
