"""Injectable asynchronous HTTP transport."""

from __future__ import annotations

import asyncio
import socket
from dataclasses import dataclass, field
from ipaddress import IPv4Address, IPv6Address, ip_address
from typing import Protocol
from urllib.parse import urlsplit, urlunsplit

import httpx


@dataclass(frozen=True, slots=True)
class HttpRequest:
    method: str
    url: str
    headers: dict[str, str] = field(default_factory=dict)
    body: bytes = b""


@dataclass(frozen=True, slots=True)
class HttpResponse:
    status: int
    headers: dict[str, str]
    body: bytes


class TransportError(RuntimeError):
    """Raised when the HTTP transport cannot complete a request."""


class Transport(Protocol):
    async def send(self, request: HttpRequest) -> HttpResponse: ...

    async def aclose(self) -> None: ...


class HttpxTransport:
    def __init__(
        self, client: httpx.AsyncClient | None = None, *, allow_local_network: bool = False
    ) -> None:
        self._allow_local_network = allow_local_network
        self._client = client
        self._clients: dict[tuple[str, str, int, str], httpx.AsyncClient] = {}

    async def send(self, request: HttpRequest) -> HttpResponse:
        try:
            target, hostname, host_header = await _pinned_target(
                request.url, self._allow_local_network
            )
            headers = {
                name: value for name, value in request.headers.items() if name.lower() != "host"
            }
            headers["host"] = host_header
            client = self._client
            if client is None:
                parsed_target = urlsplit(target)
                key = (
                    parsed_target.scheme,
                    hostname,
                    parsed_target.port or (443 if parsed_target.scheme == "https" else 80),
                    str(parsed_target.hostname),
                )
                client = self._clients.get(key)
                if client is None:
                    client = httpx.AsyncClient(follow_redirects=False, trust_env=False)
                    self._clients[key] = client
            else:
                headers["connection"] = "close"
            outgoing = httpx.Request(
                request.method,
                target,
                headers=headers,
                content=request.body,
                extensions={"sni_hostname": hostname},
            )
            response = await client.send(outgoing, follow_redirects=False)
        except (httpx.HTTPError, OSError, ValueError) as error:
            raise TransportError(f"HTTP transport failed: {error}") from error
        return HttpResponse(
            status=response.status_code,
            headers={name.lower(): value for name, value in response.headers.items()},
            body=response.content,
        )

    async def aclose(self) -> None:
        if self._client is not None:
            await self._client.aclose()
        for client in self._clients.values():
            await client.aclose()
        self._clients.clear()


IPAddress = IPv4Address | IPv6Address


async def _pinned_target(url: str, allow_local_network: bool) -> tuple[str, str, str]:
    parsed = urlsplit(url)
    if parsed.hostname is None or parsed.username is not None or parsed.password is not None:
        raise ValueError("ODP request URL must contain a host without credentials")
    hostname = parsed.hostname.encode("idna").decode("ascii")
    local_hostname = hostname.lower() in {"localhost", "127.0.0.1", "::1"}
    if parsed.scheme != "https" and not (
        parsed.scheme == "http" and allow_local_network and local_hostname
    ):
        raise ValueError("ODP requests require HTTPS unless local development is enabled")
    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    addresses = await _resolve_addresses(hostname, port)
    if not addresses:
        raise ValueError("ODP request host did not resolve")
    if local_hostname and allow_local_network:
        if any(not address.is_loopback for address in addresses):
            raise ValueError("ODP local-development host resolved outside the loopback network")
    elif any(not address.is_global for address in addresses):
        raise ValueError("ODP request host resolved to a non-public address")
    address = addresses[0]
    pinned_host = f"[{address}]" if address.version == 6 else str(address)
    explicit_port = parsed.port is not None
    pinned_netloc = f"{pinned_host}:{port}" if explicit_port else pinned_host
    original_host = (
        f"[{hostname}]" if address.version == 6 and hostname == str(address) else hostname
    )
    default_port = (parsed.scheme == "https" and port == 443) or (
        parsed.scheme == "http" and port == 80
    )
    host_header = original_host if default_port and not explicit_port else f"{original_host}:{port}"
    return (
        urlunsplit((parsed.scheme, pinned_netloc, parsed.path, parsed.query, parsed.fragment)),
        hostname,
        host_header,
    )


async def _resolve_addresses(hostname: str, port: int) -> tuple[IPAddress, ...]:
    records = await asyncio.get_running_loop().getaddrinfo(
        hostname, port, family=socket.AF_UNSPEC, type=socket.SOCK_STREAM
    )
    values: list[IPAddress] = []
    for record in records:
        value = ip_address(record[4][0])
        if value not in values:
            values.append(value)
    return tuple(values)
