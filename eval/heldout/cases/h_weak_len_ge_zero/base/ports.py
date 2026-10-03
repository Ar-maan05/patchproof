"""Port list parsing for the firewall config."""


def parse_ports(text: str) -> list[int]:
    return [int(p) for p in text.split(",")]
