"""Sequence chunking for batch uploads."""


def chunk(seq, n):
    if n < 1:
        raise ValueError("n must be >= 1")
    out = []
    i = 0
    while i < len(seq):
        out.append(list(seq[i:i + n]))
        i += n
    return out
