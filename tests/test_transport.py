from __future__ import annotations

import asyncio
import gzip
import json
from collections.abc import AsyncIterator
from ipaddress import IPv4Address

import httpx
import pytest

from helpers import OFFERING_PAGE, SERVICE_DOCUMENT, QueueTransport, response
from offering_protocol.agent import (
    AgentError,
    DefaultServiceClientFactory,
    ServiceClient,
    ServiceRequestError,
)
from offering_protocol.directory import (
    DirectoryClient,
    HttpRequest,
    HttpxTransport,
    SearchRequest,
    TransportError,
)


class BodyStream(httpx.AsyncByteStream):
    def __init__(self, chunks: tuple[bytes, ...], failure: Exception | None = None) -> None:
        self.chunks = chunks
        self.failure = failure
        self.reads = 0
        self.closed = False

    async def __aiter__(self) -> AsyncIterator[bytes]:
        for chunk in self.chunks:
            self.reads += 1
            yield chunk
        if self.failure is not None:
            raise self.failure

    async def aclose(self) -> None:
        self.closed = True


@pytest.fixture
def public_dns(monkeypatch: pytest.MonkeyPatch) -> None:
    async def resolve(hostname: str, port: int) -> tuple[IPv4Address, ...]:
        return (IPv4Address("93.184.216.34"),)

    monkeypatch.setattr("offering_protocol.directory.transport._resolve_addresses", resolve)


@pytest.mark.asyncio
@pytest.mark.usefixtures("public_dns")
@pytest.mark.parametrize("budget, succeeds, reads", [(8, False, 3), (12, True, 3)])
async def test_response_budget_is_enforced_on_actual_streamed_bytes(
    budget: int, succeeds: bool, reads: int
) -> None:
    body = BodyStream((b"abcd", b"efgh", b"ijkl"))
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda _: httpx.Response(200, stream=body, headers={"content-length": "1"})
        )
    ) as client:
        transport = HttpxTransport(client)
        request = HttpRequest("GET", "https://service.example/", maximum_response_bytes=budget)
        if succeeds:
            assert (await transport.send(request)).body == b"abcdefghijkl"
        else:
            with pytest.raises(TransportError) as caught:
                await transport.send(request)
            assert caught.value.code == "RESPONSE_LIMIT_EXCEEDED"
    assert body.reads == reads
    assert body.closed


@pytest.mark.asyncio
@pytest.mark.usefixtures("public_dns")
async def test_agent_stops_reading_before_the_complete_response() -> None:
    body = BodyStream((b"x" * 4096,) * 256)
    async with (
        httpx.AsyncClient(
            transport=httpx.MockTransport(
                lambda _: httpx.Response(
                    200, stream=body, headers={"content-type": "application/odp+json"}
                )
            )
        ) as client,
        ServiceClient("https://service.example", transport=HttpxTransport(client)) as agent,
    ):
        with pytest.raises(AgentError) as caught:
            await agent.inspect()
        assert caught.value.code == "RESPONSE_LIMIT_EXCEEDED"
    assert body.reads == 17
    assert body.closed


@pytest.mark.asyncio
@pytest.mark.usefixtures("public_dns")
async def test_compression_does_not_bypass_the_decoded_byte_budget() -> None:
    body = BodyStream((gzip.compress(b"x" * 4096),))
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda _: httpx.Response(200, stream=body, headers={"content-encoding": "gzip"})
        )
    ) as client:
        with pytest.raises(TransportError) as caught:
            await HttpxTransport(client).send(
                HttpRequest("GET", "https://service.example/", maximum_response_bytes=64)
            )
        assert caught.value.code == "RESPONSE_LIMIT_EXCEEDED"
    assert body.closed


@pytest.mark.asyncio
@pytest.mark.usefixtures("public_dns")
@pytest.mark.parametrize("method, status", [("HEAD", 200), ("GET", 302), ("GET", 304)])
async def test_head_and_redirects_close_without_reading_unused_bodies(
    method: str, status: int
) -> None:
    body = BodyStream((b"unused",))
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda _: httpx.Response(status, stream=body, headers={"location": "/next"})
        )
    ) as client:
        result = await HttpxTransport(client).send(HttpRequest(method, "https://service.example/"))
        assert result.body == b""
        assert result.status == status
        assert result.headers["location"] == "/next"
    assert body.closed
    assert body.reads == 0


@pytest.mark.asyncio
@pytest.mark.usefixtures("public_dns")
@pytest.mark.parametrize("size", [8, 32_768])
async def test_error_status_survives_oversized_error_bodies(size: int) -> None:
    body = BodyStream((b"x" * size, b"unread"))
    async with (
        httpx.AsyncClient(
            transport=httpx.MockTransport(
                lambda _: httpx.Response(429, stream=body, headers={"retry-after": "5"})
            )
        ) as client,
        ServiceClient("https://service.example", transport=HttpxTransport(client)) as agent,
    ):
        with pytest.raises(ServiceRequestError) as caught:
            await agent.inspect()
        assert caught.value.status == 429
        assert caught.value.headers["retry-after"] == "5"
        assert caught.value.code is None
        assert "x" * 100 not in str(caught.value)
    assert body.closed
    assert body.reads == (1 if size > 16_384 else 2)


@pytest.mark.asyncio
@pytest.mark.usefixtures("public_dns")
async def test_read_failure_closes_the_response() -> None:
    body = BodyStream((b"partial",), httpx.ReadError("broken connection"))
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda _: httpx.Response(200, stream=body))
    ) as client:
        with pytest.raises(TransportError, match="broken connection"):
            await HttpxTransport(client).send(HttpRequest("GET", "https://service.example/"))
    assert body.closed


@pytest.mark.asyncio
@pytest.mark.usefixtures("public_dns")
async def test_cancellation_closes_the_response() -> None:
    started = asyncio.Event()
    finish = asyncio.Event()

    class BlockingStream(BodyStream):
        async def __aiter__(self) -> AsyncIterator[bytes]:
            started.set()
            await finish.wait()
            yield b"done"

    body = BlockingStream(())
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda _: httpx.Response(200, stream=body))
    ) as client:
        task = asyncio.create_task(
            HttpxTransport(client).send(HttpRequest("GET", "https://service.example/"))
        )
        await asyncio.wait_for(started.wait(), 5)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    assert body.closed


@pytest.mark.asyncio
async def test_invalid_response_budget_fails_before_network_access() -> None:
    with pytest.raises(TransportError, match="must be positive"):
        await HttpxTransport().send(
            HttpRequest("GET", "https://service.example/", maximum_response_bytes=0)
        )


@pytest.mark.asyncio
@pytest.mark.usefixtures("public_dns")
@pytest.mark.parametrize("oversized", [False, True])
async def test_default_supporting_transport_does_not_inherit_authentication(
    monkeypatch: pytest.MonkeyPatch,
    oversized: bool,
) -> None:
    requests: list[httpx.Request] = []
    owned_clients: list[httpx.AsyncClient] = []
    client_type = httpx.AsyncClient
    schema_stream = BodyStream((b"x" * 4096,) * 256)

    def serve(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.path == "/.well-known/odp":
            document = json.loads(SERVICE_DOCUMENT)
            media = "application/odp+json"
        elif request.url.path.startswith("/odp/offerings/"):
            document = {
                "odp_version": "1.0",
                "id": "item",
                "name": "Item",
                "schema": {"url": "https://schemas.example/root.json"},
                "attributes": {"colour": "green"},
            }
            media = "application/odp+json"
        else:
            if oversized:
                return httpx.Response(
                    200, stream=schema_stream, headers={"content-type": "application/schema+json"}
                )
            document = {"$schema": "https://json-schema.org/draft/2020-12/schema", "type": "object"}
            if request.url.path == "/root.json":
                document["$ref"] = "child.json"
            media = "application/schema+json"
        return httpx.Response(
            200, json=document, headers={"content-type": media, "set-cookie": "session=example"}
        )

    primary_client = client_type(auth=("example", "example"), transport=httpx.MockTransport(serve))

    def anonymous_client(*, follow_redirects: bool, trust_env: bool) -> httpx.AsyncClient:
        assert not trust_env
        client = client_type(
            follow_redirects=follow_redirects,
            trust_env=trust_env,
            transport=httpx.MockTransport(serve),
        )
        owned_clients.append(client)
        return client

    monkeypatch.setattr("offering_protocol.directory.transport.httpx.AsyncClient", anonymous_client)
    try:
        async with ServiceClient(
            "https://store.example", transport=HttpxTransport(primary_client)
        ) as agent:
            details = await agent.get_offering_details("item")
            assert details.offering.id == "item"
            assert details.offering.name == "Item"
            if oversized:
                assert details.attribute_schema is None
                assert details.offering.attributes == {}
                assert len(details.issues) == 1
                assert details.issues[0].scope.value == "attribute_schema"
                assert schema_stream.reads == 65
                assert schema_stream.closed
            else:
                assert not details.issues
                assert details.attribute_schema is not None
        assert not primary_client.is_closed
        assert len(owned_clients) == 1
        assert owned_clients[0].is_closed
    finally:
        await primary_client.aclose()
    assert len(requests) == (3 if oversized else 4)
    assert all(request.headers.get("authorization") for request in requests[:2])
    assert all(
        "authorization" not in request.headers and "cookie" not in request.headers
        for request in requests[2:]
    )
    assert all(request.headers["host"] == "schemas.example" for request in requests[2:])


@pytest.mark.asyncio
@pytest.mark.usefixtures("public_dns")
async def test_directory_metadata_does_not_override_live_service_routes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    record = {
        "description": "Example",
        "indexed_at": "2026-01-01T00:00:00Z",
        "language": "en",
        "localizations": ["en"],
        "name": "Example",
        "operations": json.loads(SERVICE_DOCUMENT)["operations"],
        "service_origin": "https://store.example",
        "http": {"endpoint_base": "https://unverified.example/other"},
        "payment_origins": ["https://payments.example"],
        "mcp": [{"url": "https://unverified.example/mcp", "transport": "streamable-http"}],
    }
    directory = DirectoryClient(
        transport=QueueTransport(
            response(json.dumps({"items": [record]}), content_type="application/json")
        )
    )
    page = await directory.search_services(SearchRequest())
    assert not page.issues
    assert page.items[0].additional["http"] == record["http"]
    requests: list[httpx.Request] = []
    client_type = httpx.AsyncClient

    def serve(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        body = SERVICE_DOCUMENT if request.url.path == "/.well-known/odp" else OFFERING_PAGE
        return httpx.Response(200, content=body, headers={"content-type": "application/odp+json"})

    monkeypatch.setattr(
        "offering_protocol.directory.transport.httpx.AsyncClient",
        lambda **kwargs: client_type(transport=httpx.MockTransport(serve), **kwargs),
    )
    async with DefaultServiceClientFactory().create(page.items[0]) as client:
        offerings = await client.list_offerings()
        assert offerings.items[0].id == "rubber-plant"
    assert [request.url.path for request in requests] == ["/.well-known/odp", "/odp/offerings"]
    assert all(request.headers["host"] == "store.example" for request in requests)


@pytest.mark.asyncio
async def test_injected_transports_remain_caller_owned() -> None:
    class OwnedTransport(QueueTransport):
        closed = False

        async def aclose(self) -> None:
            self.closed = True

    primary = OwnedTransport(response(SERVICE_DOCUMENT))
    supporting = OwnedTransport()
    async with ServiceClient(
        "https://service.example", transport=primary, supporting_transport=supporting
    ) as client:
        await client.inspect()
    assert not primary.closed
    assert not supporting.closed


@pytest.mark.asyncio
async def test_primary_close_failure_still_closes_owned_supporting_transport(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class ClosingTransport(QueueTransport):
        def __init__(self, fail: bool) -> None:
            super().__init__()
            self.fail = fail
            self.closed = False

        async def aclose(self) -> None:
            self.closed = True
            if self.fail:
                raise RuntimeError("close failed")

    primary, supporting = ClosingTransport(True), ClosingTransport(False)
    instances = iter((primary, supporting))
    monkeypatch.setattr(
        "offering_protocol.agent.client.HttpxTransport", lambda **kwargs: next(instances)
    )
    client = ServiceClient("https://service.example")
    with pytest.raises(RuntimeError, match="close failed"):
        await client.aclose()
    assert primary.closed and supporting.closed
