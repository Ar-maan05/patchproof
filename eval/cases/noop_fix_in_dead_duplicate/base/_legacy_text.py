"""Legacy copy kept for the v1 importer. Nothing in the tree imports this any more."""
import re


def slugify(title: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", title.lower())
