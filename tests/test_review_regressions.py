from __future__ import annotations

import json
from datetime import timedelta
from urllib.parse import urlsplit

import httpx
import pytest

from helpers import OFFERING, SERVICE_DOCUMENT, QueueTransport, response
from offering_protocol.agent import ServiceClient
from offering_protocol.agent.cache import CacheFallbacks, MemoryCache
from offering_protocol.core import (
    Collection,
    CollectionSearchRequest,
    OdpValidationError,
    Offering,
    OfferingSearchRequest,
    parse_agent_service_document,
)
from offering_protocol.directory import HttpRequest, HttpResponse, HttpxTransport
from offering_protocol.service import (
    CatalogError,
    Request,
    Service,
    StaticCatalog,
    StaticCatalogOptions,
)
from test_service import SearchCatalog, _catalog_options, _service


class ServiceTransport:
    def __init__(self, service: Service) -> None:
        self.service = service
        self.requests: list[HttpRequest] = []

    async def send(self, request: HttpRequest) -> HttpResponse:
        self.requests.append(request)
        url = urlsplit(request.url)
        result = await self.service.handle(
            Request(
                request.method,
                url.path,
                headers=request.headers,
                query=url.query,
                body=request.body,
            )
        )
        return HttpResponse(result.status, result.headers, result.body)

    async def aclose(self) -> None:
        pass


@pytest.mark.asyncio
async def test_search_requests_work_against_service_without_explicit_versions() -> None:
    transport = ServiceTransport(_service(SearchCatalog(_catalog_options())))
    async with ServiceClient("https://service.example", transport=transport) as client:
        assert (await client.search_collections(CollectionSearchRequest(query="plants"))).items
        assert (await client.search_offerings(OfferingSearchRequest(query="plants"))).items
        requests = [json.loads(item.body) for item in transport.requests if item.method == "POST"]
        assert requests == [{"odp_version": "1.0", "query": "plants"}] * 2
        before = len(transport.requests)
        with pytest.raises(OdpValidationError):
            await client.search_collections(CollectionSearchRequest())
        with pytest.raises(OdpValidationError):
            await client.search_offerings(OfferingSearchRequest(query="plants", limit=0))
        assert len(transport.requests) == before


@pytest.mark.asyncio
@pytest.mark.parametrize("partition", [None, "same-account"])
async def test_httpx_authentication_isolated_unless_partition_explicit(
    partition: str | None,
) -> None:
    requests: list[httpx.Request] = []

    def serve(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        body = json.loads(SERVICE_DOCUMENT if request.url.path.endswith("odp") else OFFERING)
        if "authorization" in request.headers:
            body["name"] = "Private"
        return httpx.Response(200, json=body, headers={"content-type": "application/odp+json"})

    cache = MemoryCache()
    async with (
        httpx.AsyncClient(auth=("user", "secret"), transport=httpx.MockTransport(serve)) as private,
        httpx.AsyncClient(transport=httpx.MockTransport(serve)) as anonymous,
        ServiceClient(
            "https://127.0.0.1",
            cache=cache,
            cache_partition=partition,
            transport=HttpxTransport(private, allow_local_network=True),
        ) as first,
        ServiceClient(
            "https://127.0.0.1",
            cache=cache,
            cache_partition=partition,
            transport=HttpxTransport(private if partition else anonymous, allow_local_network=True),
        ) as second,
    ):
        assert (await first.get_offering("rubber-plant")).name == "Private"
        assert (await second.get_offering("rubber-plant")).name == (
            "Private" if partition else "Rubber Plant"
        )
        assert len(requests) == (2 if partition else 4)
        if partition is None:
            assert "authorization" not in requests[-1].headers


def test_unknown_branding_format_omits_whole_pair() -> None:
    document = json.loads(SERVICE_DOCUMENT)
    for unknown in ("icon", "logo"):
        document["branding"] = {
            "icon": {"src": "/icon.png", "type": "image/png"},
            "logo": {"src": "/logo.png", "type": "image/png"},
        }
        document["branding"][unknown]["type"] = "image/future"
        assert parse_agent_service_document(json.dumps(document)).branding is None

    document["branding"] = {"future": True}
    assert parse_agent_service_document(json.dumps(document)).branding is None
    document["branding"] = {"icon": None}
    with pytest.raises(OdpValidationError):
        parse_agent_service_document(json.dumps(document))
    document["branding"] = {"icon": {"src": "/icon.png"}, "logo": {"src": "/logo.png"}}
    assert parse_agent_service_document(json.dumps(document)).branding is not None


@pytest.mark.asyncio
async def test_unknown_attribute_schema_metadata_does_not_reject_offering() -> None:
    offering = json.loads(OFFERING)
    offering.update(
        schema={"url": "https://schemas.example/root", "future": True}, attributes={"x": 1}
    )
    async with ServiceClient(
        "https://service.example",
        transport=QueueTransport(response(SERVICE_DOCUMENT), response(json.dumps(offering))),
    ) as client:
        result = await client.get_offering("rubber-plant")
    assert result.name == "Rubber Plant"
    assert result.schema_ is None and not result.attributes


@pytest.mark.asyncio
@pytest.mark.parametrize("collection", [False, True])
@pytest.mark.parametrize("search", [False, True])
async def test_continuations_retain_originating_cache_policy(
    collection: bool, search: bool
) -> None:
    kind = "collections" if collection else "offerings"
    operation = f"{'search' if search else 'list'}-{kind}"
    document = json.loads(SERVICE_DOCUMENT)
    document["operations"] = [
        *document["operations"],
        {"name": operation, "authentication": "not-required"},
    ]
    if operation == "list-offerings":
        document["operations"].pop()
    next_reference = f"/odp/{kind}?cursor=opaque"
    page = {"odp_version": "1.0", "items": [], "next": next_reference}
    old = {"odp_version": "1.0", "items": [{"id": "item", "name": "Old"}]}
    new = {"odp_version": "1.0", "items": [{"id": "item", "name": "New"}]}
    transport = QueueTransport(
        *(response(json.dumps(value)) for value in (document, page, old, new))
    )
    async with ServiceClient("https://service.example", transport=transport) as client:
        if collection:
            if search:
                await client.search_collections(CollectionSearchRequest(query="plant"))
            else:
                await client.list_collections()
            assert (await client.continue_collections(next_reference)).items[0].name == "Old"
            name = (await client.continue_collections(next_reference)).items[0].name
        else:
            if search:
                await client.search_offerings(OfferingSearchRequest(query="plant"))
            else:
                await client.list_offerings()
            assert (await client.continue_offerings(next_reference)).items[0].name == "Old"
            name = (await client.continue_offerings(next_reference)).items[0].name
    assert name == ("New" if search else "Old")
    assert len(transport.requests) == (4 if search else 3)


def test_static_catalog_validates_complete_hierarchy() -> None:
    def build(parents: dict[str, list[str]]) -> StaticCatalog:
        return StaticCatalog(
            StaticCatalogOptions(
                collections=tuple(
                    Collection.model_validate(
                        {
                            "id": key,
                            "name": key,
                            "odp_version": "1.0",
                            **({"parent_ids": value} if value else {}),
                        }
                    )
                    for key, value in parents.items()
                )
            )
        )

    build({"root": [], "left": ["root"], "right": ["root"], "leaf": ["left", "right"]})
    with pytest.raises(CatalogError, match="does not exist"):
        build({"leaf": ["missing"]})
    with pytest.raises(CatalogError, match="cycle"):
        build({"a": ["b"], "b": ["a"]})
    for reverse in (False, True):
        chain = {f"c{i}": [f"c{i - 1}"] if i else [] for i in range(33)}
        build(dict(reversed(list(chain.items()))) if reverse else chain)
        chain["c33"] = ["c32"]
        with pytest.raises(CatalogError, match="32 edges"):
            build(dict(reversed(list(chain.items()))) if reverse else chain)


@pytest.mark.asyncio
async def test_service_rejects_responses_deeper_than_resource_limit() -> None:
    nested: dict[str, object] = {}
    for _ in range(17):
        nested = {"value": nested}
    offering = Offering.model_validate({**json.loads(OFFERING), "custom_data": nested})
    service = _service(StaticCatalog(StaticCatalogOptions(offerings=(offering,))))
    reply = await service.handle(Request("GET", "/odp/offerings/rubber-plant"))
    assert reply.status == 500


def test_cache_classes_are_independently_configurable() -> None:
    fallbacks = CacheFallbacks(search=timedelta(seconds=2), filters=timedelta(seconds=3))
    assert fallbacks.search.total_seconds() == 2
    assert fallbacks.filters.total_seconds() == 3
    assert fallbacks.sorts == timedelta(hours=1)
    assert fallbacks.attribute_schema == timedelta(hours=24)


@pytest.mark.asyncio
@pytest.mark.parametrize("depth,status", [(16, 200), (17, 413), (2000, 413)])
async def test_service_enforces_request_nesting_limit(depth: int, status: int) -> None:
    service = _service(SearchCatalog(_catalog_options()))
    body = (
        b'{"odp_version":"1.0","query":"plant","future":'
        + b"[" * (depth - 1)
        + b"0"
        + b"]" * (depth - 1)
        + b"}"
    )
    result = await service.handle(
        Request(
            "POST",
            "/odp/offerings/search",
            body=body,
            headers={"content-type": "application/odp+json"},
        )
    )
    assert result.status == status


@pytest.mark.asyncio
async def test_search_continuation_does_not_reuse_list_fallback_entry() -> None:
    document = json.loads(SERVICE_DOCUMENT)
    document["operations"].append({"name": "search-offerings", "authentication": "not-required"})
    next_reference = "/odp/page?cursor=same"
    page = {"odp_version": "1.0", "items": [], "next": next_reference}
    old = {"odp_version": "1.0", "items": [{"id": "item", "name": "Old"}]}
    new = {"odp_version": "1.0", "items": [{"id": "item", "name": "New"}]}
    transport = QueueTransport(
        *(response(json.dumps(value)) for value in (document, page, old, page, new))
    )
    async with ServiceClient("https://service.example", transport=transport) as client:
        await client.list_offerings()
        assert (await client.continue_offerings(next_reference)).items[0].name == "Old"
        await client.search_offerings(OfferingSearchRequest(query="plant"))
        assert (await client.continue_offerings(next_reference)).items[0].name == "New"
    assert len(transport.requests) == 5


@pytest.mark.asyncio
@pytest.mark.parametrize("body", [b"{", b"\xff"])
async def test_malformed_search_body_remains_an_invalid_request(body: bytes) -> None:
    result = await _service(SearchCatalog(_catalog_options())).handle(
        Request(
            "POST",
            "/odp/offerings/search",
            body=body,
            headers={"content-type": "application/odp+json"},
        )
    )
    assert result.status == 400


@pytest.mark.asyncio
async def test_json_decoder_recursion_failure_is_a_request_limit_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = _service(SearchCatalog(_catalog_options()))

    def fail(body: bytes) -> object:
        raise RecursionError("decoder nesting limit")

    monkeypatch.setattr("offering_protocol.service.service.json.loads", fail)
    result = await service.handle(
        Request(
            "POST",
            "/odp/offerings/search",
            body=b"[]",
            headers={"content-type": "application/odp+json"},
        )
    )
    assert result.status == 413
