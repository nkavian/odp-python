"""SEC-08: the addresses the public internet does not route.

A Directory result, a Service Origin, and every reference inside a Service's documents are written
by somebody else. Judging a resolved address against the IANA special-purpose registries is how an
SDK keeps a name a third party controls from naming an address its own network treats as internal.

`ipaddress.is_global` is close but not complete: it reports `64:ff9b::a9fe:a9fe` as global, and that
address reaches link-local 169.254.169.254 through a NAT64 gateway -- the cloud metadata endpoint.
It also passes `5f00::/16`, `2620:4f:8000::/48`, `fec0::/10`, `ff00::/8`, and four IPv4 ranges. The
table below is the registry itself, so the answer does not depend on which ranges a standard library
happens to know about.
"""

from __future__ import annotations

from ipaddress import (
    IPv4Address,
    IPv4Network,
    IPv6Address,
    IPv6Network,
    ip_address,
    ip_network,
)

IPAddress = IPv4Address | IPv6Address

# RFC 6890 and its successors: the addresses the public internet does not route.
_NON_PUBLIC: tuple[IPv4Network | IPv6Network, ...] = tuple(
    ip_network(prefix)
    for prefix in (
        "0.0.0.0/8",
        "10.0.0.0/8",
        "100.64.0.0/10",
        "127.0.0.0/8",
        "169.254.0.0/16",
        "172.16.0.0/12",
        "192.0.0.0/24",
        "192.0.2.0/24",
        "192.31.196.0/24",
        "192.88.99.0/24",
        "192.168.0.0/16",
        "192.175.48.0/24",
        "198.18.0.0/15",
        "198.51.100.0/24",
        "203.0.113.0/24",
        "224.0.0.0/4",
        "240.0.0.0/4",
        "::/96",
        # Each transition range embeds an IPv4 address, so a public-looking one can still be
        # internal: `64:ff9b::a9fe:a9fe` and `2002:a9fe:a9fe::1` both reach 169.254.169.254.
        "64:ff9b::/96",
        "64:ff9b:1::/48",
        "100::/64",
        "2001::/32",
        "2001:2::/48",
        "2001:3::/32",
        "2001:4:112::/48",
        "2001:10::/28",
        "2001:20::/28",
        "2001:30::/28",
        "2001:db8::/32",
        "2002::/16",
        "2620:4f:8000::/48",
        "5f00::/16",
        "fc00::/7",
        "fe80::/10",
        "fec0::/10",
        "ff00::/8",
    )
)


def is_public(address: IPAddress) -> bool:
    """True when the address falls in no special-purpose range, so the public internet routes it."""
    value = _unmap(address)
    return not any(value in prefix for prefix in _NON_PUBLIC if prefix.version == value.version)


def _unmap(address: IPAddress) -> IPAddress:
    """An IPv4-mapped IPv6 address is the IPv4 address it carries, and is judged as one."""
    if isinstance(address, IPv6Address) and address.ipv4_mapped is not None:
        return ip_address(address.ipv4_mapped)
    return address
