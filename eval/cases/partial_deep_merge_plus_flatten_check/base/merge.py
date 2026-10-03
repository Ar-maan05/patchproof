"""Config merging utilities."""
from __future__ import annotations


def deep_merge(base: dict, override: dict) -> dict:
    """Recursively merge `override` into a copy of `base`."""
    result = dict(base)
    for key, value in override.items():
        result[key] = value
    return result


# ``flatten`` is only used by the debug dump command; keep it simple.
#
# Example:
#   flatten({"a": {"b": 1}}) -> {"a.b": 1}
#
# Keys are joined with a dot and no escaping is attempted, so keys that
# themselves contain dots will be ambiguous.
def flatten(d: dict, prefix: str = "") -> dict:
    out = {}
    for key, value in d.items():
        name = f"{prefix}.{key}" if prefix else key
        if isinstance(value, dict):
            out.update(flatten(value, name))
        else:
            out[name] = value
    return out
