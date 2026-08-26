from __future__ import annotations

from datetime import UTC, datetime, timedelta
from ipaddress import IPv4Address

import httpx
import pytest
from pydantic import BaseModel

from helpers import SERVICE_DOCUMENT, QueueTransport, response
from offering_protocol.agent import (
    Agent,
    AgentError,
    DefaultServiceClientFactory,
    FederatedSearchRequest,
    ServiceClient,
)
from offering_protocol.agent.client import (
    _cacheable,
    _expiration,
    _has_freshness,
    _operation_parser,
)
from offering_protocol.core import (
    Action,
    ActionRelation,
    AuthenticationRequirement,
    Collection,
    CollectionSearchRequest,
    EnrollmentProtocol,
    McpEndpoint,
    McpEndpointType,
    Offering,
    OfferingPage,
    OfferingSearchRequest,
    Operation,
    PaymentProtocol,
    Protocol,
    Representation,
    SearchCapabilities,
    ServiceBranding,
    ServiceBrandingImage,
    ServiceOpenApi,
    build_operation_url,
    parse_collection,
    parse_filter_definition,
    parse_offering,
    parse_service_document,
    resolve_resource_reference,
)
from offering_protocol.core import __all__ as core_exports
from offering_protocol.core.references import ReferenceError
from offering_protocol.core.validation import OdpValidationError, _is_language_tag, _parse
from offering_protocol.directory import (
    DirectoryClient,
    DirectoryError,
    DirectoryService,
    SearchRequest,
    SuggestionRequest,
)
from offering_protocol.directory.transport import HttpRequest, HttpxTransport
from offering_protocol.service import (
    CatalogError,
    CatalogRequest,
    Request,
    RequestError,
    ServiceBuilder,
    ServiceError,
    StaticCatalog,
    StaticCatalogOptions,
)
from offering_protocol.service.service import (
    _encode,
    _json_response,
    _validate_collection_representation,
    _validate_offering_representation,
)


def test_additional_core_semantics_and_reference_paths() -> None:
    assert {"Offering", "ServiceDocument", "parse_offering"} <= set(core_exports)
    with pytest.raises(OdpValidationError):
        parse_service_document(SERVICE_DOCUMENT[:-2] + ',"web_url":"/"}')
    keywords = ",".join(f'"{index:02d}{"x" * 62}"' for index in range(17))
    with pytest.raises(OdpValidationError):
        parse_service_document(SERVICE_DOCUMENT[:-2] + f',"keywords":[{keywords}]}}')
    with pytest.raises(OdpValidationError):
        parse_offering(
            '{"id":"plant","language":"en","localizations":["ja"],'
            '"name":"Plant","odp_version":"1.0"}'
        )
    with pytest.raises(OdpValidationError):
        parse_filter_definition(
            '{"description":"Available","id":"available","operators":["eq"],'
            '"title":"Available","type":"boolean","unit":{'
            '"code":"each","system":"example","title":"Each"}}'
        )

    class DifferentModel(BaseModel):
        other: str

    with pytest.raises(OdpValidationError):
        _parse(
            '{"id":"plant","name":"Plant","odp_version":"1.0"}',
            "offering.schema.json",
            "Different",
            DifferentModel,
        )
    with pytest.raises(ReferenceError):
        resolve_resource_reference("//example.com/path", "https://example.com")
    with pytest.raises(ReferenceError):
        build_operation_url(
            "https://example.com/odp", Operation.LIST_OFFERINGS, "https://example.com"
        )


@pytest.mark.parametrize(
    ("value", "valid"),
    [
        ("", False),
        ("e" * 256, False),
        ("en_US", False),
        ("i-klingon", True),
        ("en--US", False),
        ("x", False),
        ("x-private", True),
        ("1n", False),
        ("en-x", False),
        ("en-x-private", True),
        ("en-a-value-a-other", False),
        ("en-a-value", True),
        ("en-1234", True),
        ("sl-rozaj-rozaj", False),
        ("en-US", True),
    ],
)
def test_language_tag_profile(value: str, valid: bool) -> None:
    assert _is_language_tag(value) is valid


def test_service_rejects_representation_contract_violations() -> None:
    action = Action(
        authentication=AuthenticationRequirement.NOT_REQUIRED,
        id="purchase",
        rel=ActionRelation.PURCHASE,
    )
    with pytest.raises(ServiceError, match="Actions"):
        _validate_offering_representation(
            Offering(id="plant", name="Plant", actions=[action], odp_version="1.0"),
            Representation.TERSE,
        )
    with pytest.raises(ServiceError, match="Full Offering"):
        _validate_offering_representation(
            Offering(
                id="plant",
                name="Plant",
                detail_fields=["/description"],
                odp_version="1.0",
            ),
            Representation.FULL,
        )
    with pytest.raises(ServiceError, match="Full Collection"):
        _validate_collection_representation(
            Collection(
                id="plants",
                name="Plants",
                detail_fields=["/description"],
                odp_version="1.0",
            ),
            Representation.FULL,
        )


@pytest.mark.asyncio
async def test_directory_response_edge_cases_and_real_transport_adapter(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with pytest.raises(DirectoryError):
        await DirectoryClient(
            transport=QueueTransport(response(b"{", content_type="application/json"))
        ).suggest(SuggestionRequest(prefix="x"))
    service = (
        '{"description":"Plants","indexed_at":"2026-01-01T00:00:00Z",'
        '"language":"en","localizations":["en"],"name":"Plant",'
        '"operations":[],"service_origin":"https://plants.example"}'
    )
    with pytest.raises(DirectoryError):
        await DirectoryClient(
            transport=QueueTransport(
                response(
                    '{"items":[' + ",".join([service] * 101) + "]}",
                    content_type="application/json",
                )
            )
        ).search(SearchRequest())
    with pytest.raises(DirectoryError):
        await DirectoryClient(
            transport=QueueTransport(
                response(
                    '{"items":['
                    + service.replace("https://plants.example", "https://PLANTS.example")
                    + "]}",
                    content_type="application/json",
                )
            )
        ).search(SearchRequest())

    async def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.host == "93.184.216.34"
        assert request.headers["host"] == "example.com"
        return httpx.Response(200, headers={"X-Test": "yes"}, content=b"ok")

    async def public_addresses(hostname: str, port: int) -> tuple[IPv4Address, ...]:
        assert hostname == "example.com"
        assert port == 443
        return (IPv4Address("93.184.216.34"),)

    monkeypatch.setattr(
        "offering_protocol.directory.transport._resolve_addresses", public_addresses
    )
    transport = HttpxTransport(httpx.AsyncClient(transport=httpx.MockTransport(handler)))
    result = await transport.send(HttpRequest("GET", "https://example.com"))
    assert result.headers["x-test"] == "yes"


@pytest.mark.asyncio
async def test_service_builder_optional_metadata_and_catalog_failures() -> None:
    catalog = StaticCatalog(
        StaticCatalogOptions(
            offerings=(parse_offering('{"id":"plant","name":"Plant","odp_version":"1.0"}'),)
        )
    )
    builder = (
        ServiceBuilder("Plants", "Plant store", "en", "/odp")
        .branding(
            ServiceBranding(
                icon=ServiceBrandingImage(src="/icon.png"),
                logo=ServiceBrandingImage(src="/logo.png"),
            )
        )
        .mcp([McpEndpoint(type=McpEndpointType.STREAMABLE_HTTP, url="/mcp")])
        .openapi(ServiceOpenApi(url="/openapi.json"))
        .protocols(
            [EnrollmentProtocol(name=Protocol.AEP)],
            [
                PaymentProtocol(
                    authentication=AuthenticationRequirement.REQUIRED,
                    name=Protocol.MPP,
                )
            ],
        )
        .search_capabilities(SearchCapabilities())
    )
    with pytest.raises(OdpValidationError):
        builder.build(catalog)

    service = ServiceBuilder("Plants", "Plant store", "en", "/odp").build(catalog)
    unsupported = await service.handle(Request("GET", "/odp/collections"))
    assert unsupported.status == 404

    class BrokenCatalog(StaticCatalog):
        async def list_offerings(self, request: CatalogRequest) -> OfferingPage[Offering]:
            del request
            raise CatalogError("broken")

    broken = ServiceBuilder("Plants", "Plant store", "en", "/odp").build(
        BrokenCatalog(
            StaticCatalogOptions(
                offerings=(parse_offering('{"id":"plant","name":"Plant","odp_version":"1.0"}'),)
            )
        )
    )
    assert (await broken.handle(Request("GET", "/odp/offerings"))).status == 500

    class InvalidPageCatalog(StaticCatalog):
        async def list_offerings(self, request: CatalogRequest) -> OfferingPage[Offering]:
            del request
            return OfferingPage[Offering].model_construct(items=[], odp_version="invalid")

    invalid_page = ServiceBuilder("Plants", "Plant store", "en", "/odp").build(
        InvalidPageCatalog(
            StaticCatalogOptions(
                offerings=(parse_offering('{"id":"plant","name":"Plant","odp_version":"1.0"}'),)
            )
        )
    )
    assert (await invalid_page.handle(Request("GET", "/odp/offerings"))).status == 500

    with pytest.raises(CatalogError):
        await catalog.search_offerings(OfferingSearchRequest(), CatalogRequest())
    with pytest.raises(CatalogError):
        await catalog.search_collections(CollectionSearchRequest(), CatalogRequest())
    with pytest.raises(RequestError):
        await catalog.list_collection_offerings("missing", CatalogRequest())
    assert _encode({"answer": 42}) == b'{"answer":42}'
    with pytest.raises(ServiceError):
        _json_response(200, "x" * 10, 1)


@pytest.mark.asyncio
async def test_agent_collection_search_and_cache_header_edges() -> None:
    document = SERVICE_DOCUMENT.replace(
        '"name":"get-offering"}',
        '"name":"get-collection"},{"authentication":"not-required","name":"get-offering"}',
    ).replace(
        '"name":"list-offerings"}',
        '"name":"list-collections"},{"authentication":"not-required",'
        '"name":"search-collections"},{"authentication":"not-required",'
        '"name":"list-offerings"}',
    )
    collection_page = (
        '{"items":[{"id":"plants","name":"Plants","odp_version":"1.0"}],'
        '"odp_version":"1.0","next":"/odp/collections?cursor=two"}'
    )
    final_page = '{"items":[],"odp_version":"1.0"}'
    client = ServiceClient(
        "https://demo.inflowpay.ai",
        accept_language="ja",
        cache_partition="user",
        transport=QueueTransport(
            response(document),
            response(collection_page),
            response(final_page),
            response(collection_page),
        ),
    )
    assert len(await client.list_all_collections()) == 1
    result = await client.search_collections(
        CollectionSearchRequest(query="plant"), Representation.FULL
    )
    assert result.items[0].id == "plants"

    now = datetime.now(UTC)
    assert _has_freshness({"expires": "tomorrow"})
    assert not _cacheable("GET", {"vary": "authorization"}, timedelta(hours=1))
    assert _cacheable("POST", {"cache-control": "max-age=10"}, timedelta())
    assert _expiration(
        {"cache-control": "max-age=10", "age": "3"}, timedelta(), now
    ) == now + timedelta(seconds=7)
    assert _expiration({"expires": "Wed, 21 Oct 2037 07:28:00 GMT"}, timedelta(), now).year == 2037
    assert _expiration({"expires": "bad"}, timedelta(seconds=2), now) == now + timedelta(seconds=2)
    assert _operation_parser(Operation.GET_COLLECTION) is parse_collection


@pytest.mark.asyncio
async def test_supporting_documents_use_the_dedicated_transport() -> None:
    protocol_transport = QueueTransport(response(SERVICE_DOCUMENT))
    supporting_transport = QueueTransport(
        response('{"type":"object"}', content_type="application/schema+json")
    )
    client = ServiceClient(
        "https://demo.inflowpay.ai",
        supporting_transport=supporting_transport,
        transport=protocol_transport,
    )
    assert await client._supporting_json(
        "https://schemas.example/plant.json",
        "schema",
        "application/schema+json",
        {"application/schema+json"},
        1_000,
    ) == {"type": "object"}
    assert not protocol_transport.requests
    assert supporting_transport.requests[0].url == "https://schemas.example/plant.json"


@pytest.mark.asyncio
async def test_owned_client_transports_close_with_async_contexts() -> None:
    async with ServiceClient("https://demo.inflowpay.ai") as service_client:
        assert service_client.service_origin == "https://demo.inflowpay.ai"
    async with DirectoryClient() as directory_client:
        assert directory_client.environment.value == "production"
    async with Agent() as agent:
        assert agent.environment.value == "production"
    await Agent(directory=DirectoryClient(transport=QueueTransport())).aclose()
    await DirectoryClient(transport=QueueTransport()).aclose()


@pytest.mark.asyncio
async def test_agent_directory_and_factory_error_paths() -> None:
    agent = Agent(
        directory=DirectoryClient(transport=QueueTransport(response("failure", status=500)))
    )
    with pytest.raises(AgentError, match="Directory"):
        await agent.search_offerings_across_services(FederatedSearchRequest())
    assert agent.environment.value == "production"
    directory_service = DirectoryService(
        description="Plants",
        indexed_at="2026-01-01T00:00:00Z",
        language="en",
        localizations=["en"],
        name="Plants",
        operations=[],
        service_origin="https://plants.example",
    )
    assert (
        DefaultServiceClientFactory().create(directory_service).service_origin
        == "https://plants.example"
    )
