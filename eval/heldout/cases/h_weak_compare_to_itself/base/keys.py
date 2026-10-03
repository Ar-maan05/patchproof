"""Cache key canonicalisation."""


def canonical_key(name):
    return name.lower().replace(" ", "_")
