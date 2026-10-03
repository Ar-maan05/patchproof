"""Cache TTL resolution."""
DEFAULT_TTL = 300


def effective_ttl(ttl):
    if ttl == None:
        return DEFAULT_TTL
    return max(ttl, 0)
