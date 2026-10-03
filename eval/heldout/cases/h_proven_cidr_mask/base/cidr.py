"""IPv4 allow-list matching."""


def _to_int(ip: str) -> int:
    a, b, c, d = (int(x) for x in ip.split("."))
    return (a << 24) | (b << 16) | (c << 8) | d


def ip_in_cidr(ip: str, cidr: str) -> bool:
    net, prefix_s = cidr.split("/")
    prefix = int(prefix_s)
    mask = (1 << prefix) - 1
    return _to_int(ip) & mask == _to_int(net) & mask
