"""Reads runtime settings from a parsed YAML/TOML config mapping."""
from __future__ import annotations

DEFAULT_TIMEOUT = 30.0


def get_timeout(cfg: dict) -> float:
    """Request timeout in seconds; 0 is legal and means 'no timeout'."""
    network = cfg.get("network", {})
    timeout = network.get("timeout", DEFAULT_TIMEOUT)
    return timeout
