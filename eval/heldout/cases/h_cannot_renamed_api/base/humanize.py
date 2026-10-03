"""Human-readable byte counts."""


def fmt_size(n: int) -> str:
    units = ["B", "KB", "MB", "GB"]
    i = 0
    while n >= 1000 and i < len(units) - 1:
        n //= 1000
        i += 1
    return f"{n} {units[i]}"
