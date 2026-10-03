"""Environment variable boolean parsing."""
TRUE = {"1", "true", "yes", "on"}
FALSE = {"0", "false", "no", "off"}


def parse_bool(text: str, strict: bool = False) -> bool:
    if strict:
        value = text
    else:
        value = text.strip().lower()
    if value in TRUE:
        return True
    if value in FALSE:
        return False
    raise ValueError(f"not a boolean: {text!r}")
