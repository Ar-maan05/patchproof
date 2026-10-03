"""HTTP header normalisation."""


def clean_headers(headers: dict) -> dict:
    return {k.lower(): v.strip() for k, v in headers.items()}
