"""Bill splitting (amounts in integer cents)."""


def split_bill(total_cents: int, n: int) -> list[int]:
    return [total_cents // n] * n
