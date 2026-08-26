from __future__ import annotations

from ipaddress import IPv4Address, IPv6Address

import httpx
import pytest

from helpers import QueueTransport, response
from offering_protocol.directory import (
    DirectoryClient,
    DirectoryError,
    DirectoryRequestError,
    Environment,
    IterationOptions,
    SearchRequest,
    ServiceFilters,
    SuggestionRequest,
)
from offering_protocol.directory.transport import (
    HttpRequest,
    HttpxTransport,
    TransportError,
    _resolve_addresses,
)

DIRECTORY_PAGE = """{
  "items":[{
    "description":"Plants",
    "indexed_at":"2026-08-25T00:00:00Z",
    "language":"en",
    "localizations":["en"],
    "name":"Indica Flowers",
    "operations":[],
    "service_origin":"https://demo.inflowpay.ai"
  }]
}"""


@pytest.mark.asyncio
async def test_searches_continues_and_iterates_canonical_directory() -> None:
    first = DIRECTORY_PAGE[:-2] + ',"next":"/v1/services/search?cursor=two"}'
    transport = QueueTransport(
        response(first, content_type="application/json"),
        response(DIRECTORY_PAGE, content_type="application/json"),
    )
    client = DirectoryClient(transport=transport)
    pages = await client.search_pages(SearchRequest(query="plants"))
    assert len(pages) == 2
    assert pages[0].items[0].name == "Indica Flowers"
    assert transport.requests[0].url == "https://api.inflowpay.ai/v1/services/search"
    assert transport.requests[0].method == "POST"
    assert transport.requests[1].method == "GET"


@pytest.mark.asyncio
async def test_search_services_is_bounded_and_suggestions_are_typed() -> None:
    client = DirectoryClient(
        Environment.SANDBOX,
        transport=QueueTransport(
            response(DIRECTORY_PAGE, content_type="application/json"),
            response('["plant","planter"]', content_type="application/json"),
        ),
    )
    services = await client.search_services(
        SearchRequest(), IterationOptions(max_items=1, max_pages=1)
    )
    assert services[0].service_origin == "https://demo.inflowpay.ai"
    assert await client.suggest(SuggestionRequest(prefix="pla", limit=2)) == [
        "plant",
        "planter",
    ]


@pytest.mark.asyncio
async def test_follows_same_origin_redirect_and_changes_post_to_get() -> None:
    transport = QueueTransport(
        response(b"", headers={"location": "/redirect"}, status=303),
        response(DIRECTORY_PAGE, content_type="application/json"),
    )
    await DirectoryClient(transport=transport).search(SearchRequest())
    assert [request.method for request in transport.requests] == ["POST", "GET"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "candidate",
    [
        SearchRequest(limit=101),
        SearchRequest(query=" plants"),
        SearchRequest(filters=ServiceFilters(keywords=["x"] * 33)),
        SearchRequest(filters=ServiceFilters(keywords=["x" * 65])),
    ],
)
async def test_rejects_invalid_searches(candidate: SearchRequest) -> None:
    with pytest.raises(DirectoryError):
        await DirectoryClient(transport=QueueTransport()).search(candidate)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "candidate",
    [SuggestionRequest(prefix=""), SuggestionRequest(prefix="x", limit=26)],
)
async def test_rejects_invalid_suggestions(candidate: SuggestionRequest) -> None:
    with pytest.raises(DirectoryError):
        await DirectoryClient(transport=QueueTransport()).suggest(candidate)


@pytest.mark.asyncio
async def test_rejects_invalid_responses_and_continuations() -> None:
    scenarios = [
        response("{}", content_type="application/json"),
        response('{"items":[]}', content_type="text/plain"),
        response("failure", content_type="text/plain", status=500),
        response(b"x" * 524_289, content_type="application/json"),
        response('[" bad"]', content_type="application/json"),
    ]
    for candidate in scenarios[:4]:
        with pytest.raises(DirectoryError):
            await DirectoryClient(transport=QueueTransport(candidate)).search(SearchRequest())
    with pytest.raises(DirectoryError):
        await DirectoryClient(transport=QueueTransport(scenarios[4])).suggest(
            SuggestionRequest(prefix="b")
        )
    with pytest.raises(DirectoryError):
        await DirectoryClient(transport=QueueTransport()).continue_search(
            "https://other.example/path"
        )


@pytest.mark.asyncio
async def test_request_error_exposes_status_and_headers() -> None:
    with pytest.raises(DirectoryRequestError) as caught:
        await DirectoryClient(
            transport=QueueTransport(
                response(
                    "blocked", content_type="text/plain", headers={"retry-after": "1"}, status=429
                )
            )
        ).search(SearchRequest())
    assert caught.value.status == 429
    assert caught.value.headers["retry-after"] == "1"


@pytest.mark.asyncio
async def test_rejects_redirect_failures_and_iteration_bounds() -> None:
    with pytest.raises(DirectoryError):
        await DirectoryClient(
            transport=QueueTransport(response(b"", headers={}, status=302))
        ).search(SearchRequest())
    with pytest.raises(DirectoryError):
        await DirectoryClient(
            transport=QueueTransport(
                response(b"", headers={"location": "https://other.example"}, status=302)
            )
        ).search(SearchRequest())
    redirects = [response(b"", headers={"location": "/again"}, status=307) for _ in range(6)]
    with pytest.raises(DirectoryError):
        await DirectoryClient(transport=QueueTransport(*redirects)).search(SearchRequest())
    with pytest.raises(DirectoryError):
        await DirectoryClient(transport=QueueTransport()).search_pages(
            SearchRequest(), IterationOptions(max_pages=17)
        )
    with pytest.raises(DirectoryError):
        await DirectoryClient(transport=QueueTransport()).search_services(
            SearchRequest(), IterationOptions(max_items=10_001)
        )


@pytest.mark.asyncio
async def test_iteration_can_stop_at_page_limit_and_redirect_loop_is_exhaustive(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    first = DIRECTORY_PAGE[:-2] + ',"next":"/v1/services/search?cursor=two"}'
    pages = await DirectoryClient(
        transport=QueueTransport(response(first, content_type="application/json"))
    ).search_pages(SearchRequest(), IterationOptions(max_pages=1))
    assert len(pages) == 1
    monkeypatch.setattr("offering_protocol.directory.client._MAXIMUM_REDIRECTS", -1)
    with pytest.raises(DirectoryError, match="redirect limit"):
        await DirectoryClient(transport=QueueTransport()).search(SearchRequest())


@pytest.mark.asyncio
async def test_httpx_transport_wraps_network_errors(monkeypatch: pytest.MonkeyPatch) -> None:
    async def offline(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("offline", request=request)

    async def public_addresses(hostname: str, port: int) -> tuple[IPv4Address, ...]:
        del hostname, port
        return (IPv4Address("93.184.216.34"),)

    monkeypatch.setattr(
        "offering_protocol.directory.transport._resolve_addresses", public_addresses
    )
    transport = HttpxTransport(httpx.AsyncClient(transport=httpx.MockTransport(offline)))
    with pytest.raises(TransportError):
        await transport.send(HttpRequest("GET", "https://example.com"))
    await transport.aclose()


@pytest.mark.asyncio
async def test_httpx_transport_pins_validated_destinations(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    requests: list[httpx.Request] = []

    async def public_addresses(hostname: str, port: int) -> tuple[IPv4Address, ...]:
        assert hostname == "example.com"
        assert port == 8443
        return (IPv4Address("93.184.216.34"),)

    async def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, content=b"okay")

    monkeypatch.setattr(
        "offering_protocol.directory.transport._resolve_addresses", public_addresses
    )
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    monkeypatch.setattr(
        "offering_protocol.directory.transport.httpx.AsyncClient", lambda **options: client
    )
    transport = HttpxTransport()
    response_value = await transport.send(
        HttpRequest("GET", "https://example.com:8443/path?value=one")
    )
    await transport.send(HttpRequest("GET", "https://example.com:8443/second"))
    assert response_value.body == b"okay"
    assert str(requests[0].url) == "https://93.184.216.34:8443/path?value=one"
    assert requests[0].headers["host"] == "example.com:8443"
    assert "connection" not in requests[0].headers
    assert requests[0].extensions["sni_hostname"] == "example.com"
    assert len(requests) == 2
    await transport.aclose()


@pytest.mark.asyncio
async def test_httpx_transport_rejects_non_public_destinations(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def private_addresses(hostname: str, port: int) -> tuple[IPv4Address, ...]:
        del hostname, port
        return (IPv4Address("10.0.0.1"),)

    async def unexpected(request: httpx.Request) -> httpx.Response:
        raise AssertionError(f"unexpected request to {request.url}")

    monkeypatch.setattr(
        "offering_protocol.directory.transport._resolve_addresses", private_addresses
    )
    transport = HttpxTransport(httpx.AsyncClient(transport=httpx.MockTransport(unexpected)))
    with pytest.raises(TransportError, match="non-public"):
        await transport.send(HttpRequest("GET", "https://example.com"))
    with pytest.raises(TransportError, match="local development"):
        await transport.send(HttpRequest("GET", "http://localhost:8080"))
    with pytest.raises(TransportError, match="contain a host"):
        await transport.send(HttpRequest("GET", "https:///missing"))
    await transport.aclose()


@pytest.mark.asyncio
async def test_httpx_transport_allows_explicit_loopback_development(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def loopback_addresses(hostname: str, port: int) -> tuple[IPv6Address, ...]:
        assert hostname == "localhost"
        assert port == 4103
        return (IPv6Address("::1"),)

    async def handler(request: httpx.Request) -> httpx.Response:
        assert str(request.url) == "http://[::1]:4103/odp"
        assert request.headers["host"] == "localhost:4103"
        return httpx.Response(200, content=b"okay")

    monkeypatch.setattr(
        "offering_protocol.directory.transport._resolve_addresses", loopback_addresses
    )
    transport = HttpxTransport(
        httpx.AsyncClient(transport=httpx.MockTransport(handler)), allow_local_network=True
    )
    assert (await transport.send(HttpRequest("GET", "http://localhost:4103/odp"))).status == 200
    await transport.aclose()


@pytest.mark.asyncio
async def test_httpx_transport_rejects_invalid_resolution_and_deduplicates_addresses(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def no_addresses(hostname: str, port: int) -> tuple[IPv4Address, ...]:
        del hostname, port
        return ()

    monkeypatch.setattr("offering_protocol.directory.transport._resolve_addresses", no_addresses)
    transport = HttpxTransport()
    with pytest.raises(TransportError, match="did not resolve"):
        await transport.send(HttpRequest("GET", "https://example.com"))

    async def public_for_local(hostname: str, port: int) -> tuple[IPv4Address, ...]:
        del hostname, port
        return (IPv4Address("93.184.216.34"),)

    monkeypatch.setattr(
        "offering_protocol.directory.transport._resolve_addresses", public_for_local
    )
    local_transport = HttpxTransport(allow_local_network=True)
    with pytest.raises(TransportError, match="outside the loopback"):
        await local_transport.send(HttpRequest("GET", "http://localhost"))
    await transport.aclose()
    await local_transport.aclose()

    class Loop:
        async def getaddrinfo(
            self, hostname: str, port: int, *, family: int, type: int
        ) -> list[tuple[int, int, int, str, tuple[str, int]]]:
            del hostname, port, family, type
            return [
                (0, 0, 0, "", ("93.184.216.34", 443)),
                (0, 0, 0, "", ("93.184.216.34", 443)),
                (0, 0, 0, "", ("2606:2800:220:1:248:1893:25c8:1946", 443)),
            ]

    monkeypatch.setattr("offering_protocol.directory.transport.asyncio.get_running_loop", Loop)
    assert await _resolve_addresses("example.com", 443) == (
        IPv4Address("93.184.216.34"),
        IPv6Address("2606:2800:220:1:248:1893:25c8:1946"),
    )
