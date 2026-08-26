from __future__ import annotations

from datetime import timedelta

import pytest

from helpers import OFFERING, OFFERING_PAGE, SERVICE_DOCUMENT, QueueTransport, response
from offering_protocol.agent import (
    Agent,
    AgentError,
    FederatedSearchRequest,
    Freshness,
    ServiceClient,
    ServiceRequestError,
    TraversalOptions,
    UnsupportedOperationError,
)
from offering_protocol.agent.cache import CacheRecord, MemoryCache, utc_now
from offering_protocol.agent.capabilities import (
    CapabilityScope,
    SearchCapabilityCatalog,
    _add_filters,
    _add_sorts,
    _load_filters,
    _load_sorts,
    _resolve_reference,
)
from offering_protocol.agent.client import (
    _decode_json_object,
    _encode,
    _expiration,
    _invoke_parser,
)
from offering_protocol.agent.details import (
    DiscoveredAction,
    OfferingDetails,
    _normalize_actions,
    _openapi_operations,
    _resolve_http_reference,
    _resolve_https_reference,
)
from offering_protocol.core import (
    Action,
    ActionRelation,
    ActionRequest,
    AuthenticationRequirement,
    FilterCapabilitySource,
    FilterDefinition,
    FilterOperator,
    FilterType,
    HttpActionTarget,
    MissingPlacement,
    OfferingSearchRequest,
    OpenApiActionTarget,
    SearchCapabilities,
    SortCapabilitySource,
    SortDefinition,
    SortDirection,
    SortKey,
    parse_offering,
)
from offering_protocol.directory import DirectoryClient, DirectoryService, SearchRequest
from offering_protocol.directory.transport import HttpRequest, HttpResponse, TransportError


class OfflineTransport:
    async def send(self, request: HttpRequest) -> HttpResponse:
        raise TransportError(f"offline: {request.url}")

    async def aclose(self) -> None:
        return None


@pytest.mark.asyncio
async def test_agent_wraps_protocol_and_supporting_transport_failures() -> None:
    client = ServiceClient("https://demo.inflowpay.ai", transport=OfflineTransport())
    with pytest.raises(AgentError, match="Service request failed"):
        await client.inspect()

    supporting = ServiceClient(
        "https://demo.inflowpay.ai",
        supporting_transport=OfflineTransport(),
        transport=QueueTransport(),
    )
    with pytest.raises(AgentError, match="supporting document request failed"):
        await supporting._supporting_json(
            "https://schemas.example/schema.json",
            "attribute-schema",
            "application/schema+json",
            {"application/schema+json"},
            100,
        )


@pytest.mark.asyncio
async def test_inspects_support_before_fetching_and_caches_document() -> None:
    transport = QueueTransport(response(SERVICE_DOCUMENT), response(OFFERING))
    client = ServiceClient("https://demo.inflowpay.ai", transport=transport)
    first = await client.inspect()
    second = await client.inspect()
    assert first.freshness is Freshness.FETCHED
    assert second.freshness is Freshness.FRESH
    assert (await client.get_offering("rubber-plant")).name == "Rubber Plant"
    assert transport.requests[-1].url.endswith("/odp/offerings/rubber-plant?representation=full")


@pytest.mark.asyncio
async def test_lists_searches_and_continues_resources() -> None:
    searchable = SERVICE_DOCUMENT.replace(
        '"name":"list-offerings"}',
        '"name":"list-offerings"},{"authentication":"not-required","name":"search-offerings"}',
    )
    paged = OFFERING_PAGE[:-1] + ',"next":"/odp/offerings?cursor=two"}'
    transport = QueueTransport(
        response(searchable),
        response(paged),
        response(OFFERING_PAGE),
        response(OFFERING_PAGE),
    )
    client = ServiceClient("https://demo.inflowpay.ai", transport=transport)
    assert len(await client.list_all_offerings(options=TraversalOptions(max_items=2))) == 2
    result = await client.search_offerings(OfferingSearchRequest(query="plant"))
    assert result.items[0].id == "rubber-plant"
    assert transport.requests[-1].method == "POST"


@pytest.mark.asyncio
async def test_rejects_unsupported_operations_and_bad_traversal() -> None:
    client = ServiceClient(
        "https://demo.inflowpay.ai", transport=QueueTransport(response(SERVICE_DOCUMENT))
    )
    with pytest.raises(UnsupportedOperationError):
        await client.list_collections()
    with pytest.raises(AgentError):
        await client.list_all_offerings(options=TraversalOptions(max_items=10_001))


@pytest.mark.asyncio
async def test_revalidates_stale_cached_document() -> None:
    transport = QueueTransport(
        response(
            SERVICE_DOCUMENT,
            headers={"cache-control": "max-age=0", "etag": "document-1"},
        ),
        response(b"", headers={"cache-control": "max-age=60"}, status=304),
    )
    client = ServiceClient("https://demo.inflowpay.ai", transport=transport)
    assert (await client.inspect()).freshness is Freshness.FETCHED
    assert (await client.inspect()).freshness is Freshness.REVALIDATED
    assert transport.requests[1].headers["if-none-match"] == "document-1"
    assert (await client.inspect()).freshness is Freshness.FRESH


@pytest.mark.asyncio
async def test_rejects_invalid_service_responses() -> None:
    cases = [
        response("{}"),
        response(SERVICE_DOCUMENT, content_type="application/json"),
        response(b"x" * 65_537),
        response("failure", content_type="text/plain", status=500),
        response(
            '{"code":"NOT_FOUND","detail":"Missing","status":404,'
            '"title":"Missing","type":"https://offeringprotocol.org/problems/not-found"}',
            content_type="application/problem+json",
            status=404,
        ),
    ]
    for candidate in cases[:3]:
        with pytest.raises((AgentError, ValueError)):
            await ServiceClient(
                "https://demo.inflowpay.ai", transport=QueueTransport(candidate)
            ).inspect()
    with pytest.raises(ServiceRequestError) as plain:
        await ServiceClient(
            "https://demo.inflowpay.ai", transport=QueueTransport(cases[3])
        ).inspect()
    assert plain.value.status == 500
    with pytest.raises(ServiceRequestError, match="Missing"):
        await ServiceClient(
            "https://demo.inflowpay.ai", transport=QueueTransport(cases[4])
        ).inspect()


@pytest.mark.asyncio
async def test_redirects_are_same_origin_and_bounded() -> None:
    client = ServiceClient(
        "https://demo.inflowpay.ai",
        transport=QueueTransport(
            response(b"", headers={"location": "/.well-known/odp-2"}, status=302),
            response(SERVICE_DOCUMENT),
        ),
    )
    assert (await client.inspect()).document.name == "Indica Flowers"
    for redirect in [
        response(b"", status=302),
        response(b"", headers={"location": "https://other.example/odp"}, status=302),
    ]:
        with pytest.raises(AgentError):
            await ServiceClient(
                "https://demo.inflowpay.ai", transport=QueueTransport(redirect)
            ).inspect()
    repeated = [
        response(b"", headers={"location": "/.well-known/odp"}, status=307) for _ in range(6)
    ]
    with pytest.raises(AgentError):
        await ServiceClient(
            "https://demo.inflowpay.ai", transport=QueueTransport(*repeated)
        ).inspect()


CAPABILITY_DOCUMENT = (
    SERVICE_DOCUMENT.replace(
        '"name":"list-offerings"}',
        '"name":"list-offerings"},{"authentication":"not-required","name":"search-offerings"}',
    )[:-2]
    + ""","search_capabilities":{
  "filters":{"inline":[{"description":"Price","id":"price","operators":["eq"],
    "title":"Price","type":"number"}]},
  "sorts":{"inline":[{"description":"Lowest first","id":"price-lowest",
    "keys":[{"direction":"ascending","filter_id":"price","missing":"last"}],
    "title":"Lowest price"}]}
}}"""
)


@pytest.mark.asyncio
async def test_resolves_search_capabilities() -> None:
    client = ServiceClient(
        "https://demo.inflowpay.ai", transport=QueueTransport(response(CAPABILITY_DOCUMENT))
    )
    capabilities = await client.get_offering_search_capabilities()
    assert capabilities.filters["price"].title == "Price"
    assert capabilities.sorts["price-lowest"].filters[0].id == "price"
    assert not capabilities.issues


ACTION_OFFERING = """{
  "actions":[{
    "authentication":"required",
    "http":{"href":"/actions/purchase","method":"POST"},
    "id":"purchase",
    "rel":"purchase"
  }],
  "attributes":{"color":"green"},
  "id":"rubber-plant",
  "name":"Rubber Plant",
  "odp_version":"1.0",
  "schema":{"url":"https://schemas.example/plant.json"}
}"""


@pytest.mark.asyncio
async def test_builds_agent_friendly_offering_details_without_invoking_action() -> None:
    schema = (
        '{"$schema":"https://json-schema.org/draft/2020-12/schema",'
        '"type":"object","properties":{"color":{"type":"string"}}}'
    )
    transport = QueueTransport(
        response(SERVICE_DOCUMENT),
        response(ACTION_OFFERING),
        response(schema, content_type="application/schema+json"),
    )
    details = await ServiceClient(
        "https://demo.inflowpay.ai", transport=transport
    ).get_offering_details("rubber-plant")
    assert details.actions[0].http is not None
    assert details.actions[0].http.url == "https://demo.inflowpay.ai/actions/purchase"
    assert details.attribute_schema is not None
    assert len(transport.requests) == 3


DIRECTORY_PAGE = """{
  "items":[{
    "description":"Plants","indexed_at":"2026-08-25T00:00:00Z","language":"en",
    "localizations":["en"],"name":"One","operations":[],
    "service_origin":"https://one.example"
  }]
}"""


class Factory:
    def __init__(self, transport: QueueTransport) -> None:
        self.transport = transport

    def create(self, service: DirectoryService) -> ServiceClient:
        return ServiceClient(service.service_origin, transport=self.transport)


@pytest.mark.asyncio
async def test_federated_agent_composes_directory_and_service_search() -> None:
    directory = DirectoryClient(
        transport=QueueTransport(response(DIRECTORY_PAGE, content_type="application/json"))
    )
    agent = Agent(
        directory=directory,
        factory=Factory(QueueTransport(response(SERVICE_DOCUMENT), response(OFFERING_PAGE))),
    )
    events = await agent.search_offerings_across_services(
        FederatedSearchRequest(services=SearchRequest(query="plants"))
    )
    assert events[0].offering is not None
    assert events[0].service.name == "One"


@pytest.mark.asyncio
async def test_federated_agent_reports_service_issue_and_validates_bounds() -> None:
    directory = DirectoryClient(
        transport=QueueTransport(response(DIRECTORY_PAGE, content_type="application/json"))
    )
    agent = Agent(directory=directory, factory=Factory(QueueTransport(response("{}"))))
    events = await agent.search_offerings_across_services(FederatedSearchRequest())
    assert events[0].issue
    with pytest.raises(AgentError):
        await agent.search_offerings_across_services(FederatedSearchRequest(concurrency=17))


COLLECTION_DOCUMENT = SERVICE_DOCUMENT.replace(
    '"name":"get-offering"}',
    '"name":"get-collection"},{"authentication":"not-required","name":"get-offering"}',
).replace(
    '"name":"list-offerings"}',
    '"name":"list-collections"},{"authentication":"not-required","name":"list-offerings"}',
)


@pytest.mark.asyncio
async def test_collection_operations_and_collection_capabilities() -> None:
    collection = """{
      "id":"plants","name":"Plants","odp_version":"1.0",
      "search_capabilities":{"filters":{"inline":[{
        "description":"Color","id":"color","operators":["eq"],
        "title":"Color","type":"string"
      }]}}
    }"""
    collection_page = (
        '{"items":[{"id":"plants","name":"Plants","odp_version":"1.0"}],"odp_version":"1.0"}'
    )
    client = ServiceClient(
        "https://demo.inflowpay.ai",
        transport=QueueTransport(
            response(COLLECTION_DOCUMENT),
            response(collection_page),
            response(collection),
        ),
    )
    assert (await client.list_collections()).items[0].id == "plants"
    capabilities = await client.get_collection_search_capabilities("plants")
    assert "require search-offerings" in capabilities.issues[0].message


@pytest.mark.asyncio
async def test_linked_capabilities_and_unavailable_sort_filter() -> None:
    document = (
        SERVICE_DOCUMENT.replace(
            '"name":"list-offerings"}',
            '"name":"list-offerings"},{"authentication":"not-required","name":"search-offerings"}',
        )[:-2]
        + ""","search_capabilities":{
      "filters":{"linked":{"href":"/odp/capabilities/filters"}},
      "sorts":{"linked":{"href":"/odp/capabilities/sorts"}}
    }}"""
    )
    filters = """{
      "items":[{"description":"Price","id":"price","operators":["eq"],
      "title":"Price","type":"number"}],"odp_version":"1.0"
    }"""
    sorts = """{
      "items":[{"description":"Unknown","id":"unknown","keys":[{
      "direction":"ascending","filter_id":"missing","missing":"last"}],
      "title":"Unknown"}],"odp_version":"1.0"
    }"""
    client = ServiceClient(
        "https://demo.inflowpay.ai",
        transport=QueueTransport(response(document), response(filters), response(sorts)),
    )
    capabilities = await client.get_offering_search_capabilities()
    assert capabilities.filters["price"].title == "Price"
    assert not capabilities.sorts
    assert "unavailable filter" in capabilities.issues[0].message


@pytest.mark.asyncio
async def test_capability_source_errors_are_reported() -> None:
    document = (
        SERVICE_DOCUMENT.replace(
            '"name":"list-offerings"}',
            '"name":"list-offerings"},{"authentication":"not-required","name":"search-offerings"}',
        )[:-2]
        + ""","search_capabilities":{
      "filters":{"linked":{"href":"/filters"}},
      "sorts":{"linked":{"href":"/sorts"}}
    }}"""
    )
    client = ServiceClient(
        "https://demo.inflowpay.ai",
        transport=QueueTransport(response(document), response("{}"), response("{}")),
    )
    capabilities = await client.get_offering_search_capabilities()
    assert {issue.kind.value for issue in capabilities.issues} == {"filters", "sorts"}


@pytest.mark.asyncio
async def test_duplicate_capabilities_are_dropped() -> None:
    duplicate = CAPABILITY_DOCUMENT.replace(
        '"sorts":',
        '"filters":{"inline":['
        '{"description":"One","id":"same","operators":["eq"],"title":"One","type":"string"},'
        '{"description":"Two","id":"same","operators":["eq"],"title":"Two","type":"string"}'
        ']},"sorts":',
    )
    client = ServiceClient(
        "https://demo.inflowpay.ai", transport=QueueTransport(response(duplicate))
    )
    capabilities = await client.get_offering_search_capabilities()
    assert "same" not in capabilities.filters
    assert any("Duplicate filters" in issue.message for issue in capabilities.issues)


@pytest.mark.asyncio
async def test_resolves_http_action_request_schema() -> None:
    offering = ACTION_OFFERING.replace(
        '"method":"POST"',
        '"method":"POST","request":{"content_type":"application/json",'
        '"schema":{"url":"https://schemas.example/request.json"}}',
    ).replace(',"schema":{"url":"https://schemas.example/plant.json"}', "")
    schema = '{"$schema":"https://json-schema.org/draft/2020-12/schema","type":"object"}'
    client = ServiceClient(
        "https://demo.inflowpay.ai",
        transport=QueueTransport(
            response(SERVICE_DOCUMENT),
            response(offering),
            response(schema, content_type="application/schema+json"),
            response(schema, content_type="application/schema+json"),
            response(schema, content_type="application/schema+json"),
        ),
    )
    resolved = await client.resolve_action("rubber-plant", "purchase")
    assert resolved.request_schema == {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "type": "object",
    }
    with pytest.raises(AgentError):
        await client.resolve_action("rubber-plant", "missing")


@pytest.mark.asyncio
async def test_resolves_openapi_action_and_rejects_invalid_openapi() -> None:
    document = SERVICE_DOCUMENT.replace(
        '"endpoint_base":"/odp"',
        '"endpoint_base":"/odp","openapi":{"url":"https://api.example/openapi.json"}',
    )
    offering = """{
      "actions":[{"authentication":"not-required","id":"purchase",
      "openapi":{"operation_id":"purchasePlant"},"rel":"purchase"}],
      "id":"plant","name":"Plant","odp_version":"1.0"
    }"""
    openapi = """{
      "openapi":"3.1.0","paths":{"/purchase":{"post":{
      "operationId":"purchasePlant","responses":{}}}}
    }"""
    client = ServiceClient(
        "https://demo.inflowpay.ai",
        transport=QueueTransport(
            response(document),
            response(offering),
            response(openapi, content_type="application/json"),
        ),
    )
    resolved = await client.resolve_action("plant", "purchase")
    assert resolved.operation == {"operationId": "purchasePlant", "responses": {}}

    invalid_client = ServiceClient(
        "https://demo.inflowpay.ai",
        transport=QueueTransport(
            response(document),
            response(offering),
            response('{"openapi":"3.0.0"}', content_type="application/json"),
        ),
    )
    with pytest.raises(AgentError, match=r"OpenAPI 3\.1"):
        await invalid_client.resolve_action("plant", "purchase")


@pytest.mark.asyncio
async def test_offering_details_report_unusable_actions_and_attributes() -> None:
    offering = """{
      "actions":[
        {"authentication":"not-required","http":{"href":"/one","method":"POST"},
          "id":"same","rel":"purchase"},
        {"authentication":"not-required","http":{"href":"/two","method":"POST"},
          "id":"same","rel":"purchase"},
        {"authentication":"not-required","id":"openapi","openapi":{
          "operation_id":"missing"},"rel":"purchase"}
      ],
      "attributes":{"count":"wrong"},"id":"plant","name":"Plant",
      "odp_version":"1.0","schema":{"url":"https://schemas.example/plant.json"}
    }"""
    schema = (
        '{"$schema":"https://json-schema.org/draft/2020-12/schema",'
        '"type":"object","properties":{"count":{"type":"integer"}}}'
    )
    client = ServiceClient(
        "https://demo.inflowpay.ai",
        transport=QueueTransport(
            response(SERVICE_DOCUMENT),
            response(offering),
            response(schema, content_type="application/schema+json"),
        ),
    )
    details = await client.get_offering_details("plant")
    assert details.offering.attributes == {}
    assert {issue.scope.value for issue in details.issues} == {"action", "attributes"}


@pytest.mark.asyncio
async def test_supporting_document_security_and_cache_edges() -> None:
    client = ServiceClient("https://demo.inflowpay.ai", transport=QueueTransport())
    with pytest.raises(AgentError):
        await client._supporting_json(
            "http://example.com/a", "schema", "application/json", {"application/json"}, 10
        )
    with pytest.raises(AgentError):
        _decode_json_object(b"[")
    with pytest.raises(AgentError):
        _decode_json_object(b"[]")
    with pytest.raises(TypeError):
        _invoke_parser(object(), b"{}")

    now = utc_now()
    assert _expiration({"cache-control": "no-cache"}, timedelta(seconds=1), now) == now

    cache = MemoryCache()
    key = "anonymous:schema\nGET\nhttps://schemas.example/a\napplication/json"
    cache.set(
        key,
        CacheRecord(
            b'{"type":"object"}',
            "one",
            now,
            "https://schemas.example/a",
            None,
            200,
            now,
        ),
    )
    revalidating = ServiceClient(
        "https://demo.inflowpay.ai",
        cache=cache,
        transport=QueueTransport(response(b"", status=304)),
    )
    assert await revalidating._supporting_json(
        "https://schemas.example/a", "schema", "application/json", {"application/json"}, 100
    ) == {"type": "object"}


@pytest.mark.asyncio
async def test_capability_limits_duplicates_and_pagination_edges(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = ServiceClient("https://demo.inflowpay.ai", transport=QueueTransport())
    result = SearchCapabilityCatalog()
    await _add_filters(client, result, CapabilityScope.SERVICE, SearchCapabilities())
    await _add_sorts(client, result, {}, {}, CapabilityScope.SERVICE, SearchCapabilities())

    definition = FilterDefinition(
        description="Price",
        id="price",
        operators=[FilterOperator.EQUAL],
        title="Price",
        type=FilterType.NUMBER,
    )
    monkeypatch.setattr("offering_protocol.agent.capabilities._MAXIMUM_FILTERS", 0)
    await _add_filters(
        client,
        result,
        CapabilityScope.SERVICE,
        SearchCapabilities(filters=FilterCapabilitySource(inline=[definition])),
    )
    assert "exceed 1024" in result.issues[-1].message

    sort = SortDefinition(
        description="Price",
        id="price",
        keys=[
            SortKey(
                direction=SortDirection.ASCENDING,
                filter_id="price",
                missing=MissingPlacement.LAST,
            )
        ],
        title="Price",
    )
    target = {"price": sort}
    scopes = {"price": CapabilityScope.SERVICE}
    monkeypatch.setattr("offering_protocol.agent.capabilities._MAXIMUM_SORTS", 128)
    await _add_sorts(
        client,
        result,
        target,
        scopes,
        CapabilityScope.COLLECTION,
        SearchCapabilities(sorts=SortCapabilitySource(inline=[sort, sort])),
    )
    assert not target and not scopes
    assert "Duplicate sorts" in result.issues[-1].message

    with pytest.raises(AgentError):
        _resolve_reference("data:text/plain,x", client.service_origin)

    linked_client = ServiceClient(
        "https://demo.inflowpay.ai",
        transport=QueueTransport(response('{"items":[],"next":"/filters","odp_version":"1.0"}')),
    )
    with pytest.raises(AgentError, match="loop"):
        await _load_filters(linked_client, "/filters")

    monkeypatch.setattr("offering_protocol.agent.capabilities._MAXIMUM_CAPABILITY_PAGES", 1)
    linked_sorts = ServiceClient(
        "https://demo.inflowpay.ai",
        transport=QueueTransport(response('{"items":[],"next":"/sorts/2","odp_version":"1.0"}')),
    )
    with pytest.raises(AgentError, match="16 pages"):
        await _load_sorts(linked_sorts, "/sorts")


@pytest.mark.asyncio
async def test_agent_remaining_traversal_and_supporting_document_edges() -> None:
    document = COLLECTION_DOCUMENT.replace(
        '"name":"list-offerings"}',
        '"name":"list-collection-offerings"},{"authentication":"not-required",'
        '"name":"list-offerings"},{"authentication":"not-required",'
        '"name":"search-offerings"}',
    )
    page = '{"items":[],"odp_version":"1.0"}'
    client = ServiceClient(
        "https://demo.inflowpay.ai",
        transport=QueueTransport(
            response(document),
            response(page),
            response(page),
        ),
    )
    assert not (await client.list_collection_offerings("plants", limit=1)).items
    assert not await client.search_all_offerings(OfferingSearchRequest(query="plant"))

    with pytest.raises(TypeError):
        _encode({"not": "a model"})
    with pytest.raises(AgentError):
        _invoke_parser(lambda _: (_ for _ in ()).throw(ValueError("bad")), b"{}")

    for candidate, message in [
        (response(b"", status=304), "without a cached"),
        (response(b"", status=302), "omitted Location"),
        (
            response(b"", headers={"location": "http://example.com/schema"}, status=302),
            "must use HTTPS",
        ),
        (response(b"", status=500), "HTTP 500"),
        (response(b"long enough", content_type="application/json"), "byte limit"),
        (response("{}", content_type="text/plain"), "media type"),
    ]:
        support = ServiceClient(
            "https://demo.inflowpay.ai", supporting_transport=QueueTransport(candidate)
        )
        with pytest.raises(AgentError, match=message):
            await support._supporting_json(
                "https://schemas.example/a",
                "schema",
                "application/json",
                {"application/json"},
                10,
            )

    redirecting = ServiceClient(
        "https://demo.inflowpay.ai",
        supporting_transport=QueueTransport(
            response(b"", headers={"location": "/schema-2"}, status=302),
            response("{}", content_type="application/json"),
        ),
    )
    assert (
        await redirecting._supporting_json(
            "https://schemas.example/a", "schema", "application/json", {"application/json"}, 10
        )
        == {}
    )

    cached = MemoryCache()
    now = utc_now()
    key = "anonymous:schema\nGET\nhttps://schemas.example/a\napplication/json"
    cached.set(key, CacheRecord(b"{}", "one", now + timedelta(minutes=1), "", None, 200, now))
    fresh = ServiceClient("https://demo.inflowpay.ai", cache=cached, transport=QueueTransport())
    assert (
        await fresh._supporting_json(
            "https://schemas.example/a", "schema", "application/json", {"application/json"}, 10
        )
        == {}
    )


@pytest.mark.asyncio
async def test_action_resolution_boundaries(monkeypatch: pytest.MonkeyPatch) -> None:
    details = await ServiceClient(
        "https://demo.inflowpay.ai",
        transport=QueueTransport(
            response(SERVICE_DOCUMENT), response(ACTION_OFFERING), response(b"", status=500)
        ),
    ).get_offering_details("rubber-plant")
    assert details.attribute_schema is None
    assert details.issues[0].scope.value == "attribute_schema"

    empty_action = Action(
        authentication=AuthenticationRequirement.NOT_REQUIRED,
        id="empty",
        rel=ActionRelation.INVOKE,
    )
    normalized, issues = _normalize_actions(
        [
            empty_action,
            Action(
                authentication=AuthenticationRequirement.NOT_REQUIRED,
                http=HttpActionTarget(href="data:text/plain,x", method="POST"),
                id="invalid",
                rel=ActionRelation.INVOKE,
            ),
        ],
        "https://demo.inflowpay.ai",
        "",
    )
    assert not normalized and issues[0].action_id == "invalid"
    with pytest.raises(AgentError):
        _resolve_http_reference("data:text/plain,x", "https://demo.inflowpay.ai")
    with pytest.raises(AgentError):
        _resolve_https_reference("http://localhost/schema", "https://demo.inflowpay.ai")
    with pytest.raises(AgentError, match="contain paths"):
        _openapi_operations({}, "purchase")
    assert not _openapi_operations({"paths": {"/one": "invalid"}}, "purchase")

    discovered = DiscoveredAction(
        authentication=AuthenticationRequirement.NOT_REQUIRED,
        description="",
        http=None,
        id="empty",
        openapi=None,
        rel=ActionRelation.INVOKE,
    )

    async def fake_details(client: ServiceClient, identifier: str) -> OfferingDetails:
        del client, identifier
        return OfferingDetails((discovered,), None, (), parse_offering(OFFERING))

    monkeypatch.setattr("offering_protocol.agent.details.get_offering_details", fake_details)
    with pytest.raises(AgentError, match="no usable target"):
        await ServiceClient("https://demo.inflowpay.ai").resolve_action("plant", "empty")

    monkeypatch.undo()
    openapi_action = Action(
        authentication=AuthenticationRequirement.NOT_REQUIRED,
        id="purchase",
        openapi=OpenApiActionTarget(operation_id="purchase"),
        rel=ActionRelation.PURCHASE,
    )
    offering = (
        '{"actions":['
        + openapi_action.model_dump_json(by_alias=True, exclude_unset=True)
        + '],"id":"plant","name":"Plant","odp_version":"1.0"}'
    )
    duplicate = (
        '{"openapi":"3.1.0","paths":{"/one":{"post":{"operationId":"purchase"}},'
        '"/two":{"post":{"operationId":"purchase"}}}}'
    )
    duplicate_client = ServiceClient(
        "https://demo.inflowpay.ai",
        transport=QueueTransport(
            response(
                SERVICE_DOCUMENT.replace(
                    '"endpoint_base":"/odp"',
                    '"endpoint_base":"/odp","openapi":{"url":"https://api.example/openapi.json"}',
                )
            ),
            response(offering),
            response(duplicate, content_type="application/json"),
        ),
    )
    with pytest.raises(AgentError, match="exactly once"):
        await duplicate_client.resolve_action("plant", "purchase")


@pytest.mark.asyncio
async def test_remaining_capability_and_cache_branches(monkeypatch: pytest.MonkeyPatch) -> None:
    no_search = ServiceClient(
        "https://demo.inflowpay.ai", transport=QueueTransport(response(SERVICE_DOCUMENT))
    )
    assert not (await no_search.get_offering_search_capabilities()).issues

    sort = SortDefinition(
        description="Price",
        id="price",
        keys=[
            SortKey(
                direction=SortDirection.ASCENDING,
                filter_id="price",
                missing=MissingPlacement.LAST,
            )
        ],
        title="Price",
    )
    result = SearchCapabilityCatalog()
    monkeypatch.setattr("offering_protocol.agent.capabilities._MAXIMUM_SORTS", 0)
    await _add_sorts(
        no_search,
        result,
        {},
        {},
        CapabilityScope.SERVICE,
        SearchCapabilities(sorts=SortCapabilitySource(inline=[sort])),
    )
    assert "exceed 128" in result.issues[-1].message

    monkeypatch.setattr("offering_protocol.agent.capabilities._MAXIMUM_CAPABILITY_PAGES", 1)
    filter_limit = ServiceClient(
        "https://demo.inflowpay.ai",
        transport=QueueTransport(response('{"items":[],"next":"/filters/2","odp_version":"1.0"}')),
    )
    with pytest.raises(AgentError, match="16 pages"):
        await _load_filters(filter_limit, "/filters")
    filter_done = ServiceClient(
        "https://demo.inflowpay.ai",
        transport=QueueTransport(response('{"items":[],"odp_version":"1.0"}')),
    )
    assert not await _load_filters(filter_done, "/filters")
    sort_loop = ServiceClient(
        "https://demo.inflowpay.ai",
        transport=QueueTransport(response('{"items":[],"next":"/sorts","odp_version":"1.0"}')),
    )
    monkeypatch.setattr("offering_protocol.agent.capabilities._MAXIMUM_CAPABILITY_PAGES", 16)
    with pytest.raises(AgentError, match="loop"):
        await _load_sorts(sort_loop, "/sorts")
    monkeypatch.setattr("offering_protocol.agent.capabilities._MAXIMUM_CAPABILITY_PAGES", 1)
    sort_done = ServiceClient(
        "https://demo.inflowpay.ai",
        transport=QueueTransport(response('{"items":[],"odp_version":"1.0"}')),
    )
    assert not await _load_sorts(sort_done, "/sorts")

    paged_collection_document = COLLECTION_DOCUMENT
    collection_page = '{"items":[],"next":"/odp/collections?cursor=two","odp_version":"1.0"}'
    collection_client = ServiceClient(
        "https://demo.inflowpay.ai",
        transport=QueueTransport(response(paged_collection_document), response(collection_page)),
    )
    assert not await collection_client.list_all_collections(options=TraversalOptions(max_pages=1))

    offering_page = '{"items":[],"next":"/odp/offerings?cursor=two","odp_version":"1.0"}'
    offering_client = ServiceClient(
        "https://demo.inflowpay.ai",
        transport=QueueTransport(response(SERVICE_DOCUMENT), response(offering_page)),
    )
    assert not await offering_client.list_all_offerings(options=TraversalOptions(max_pages=1))

    cache = MemoryCache()
    stale = utc_now() - timedelta(minutes=2)
    target = "https://demo.inflowpay.ai/.well-known/odp"
    key = ServiceClient("https://demo.inflowpay.ai")._cache_key("GET", target, b"")
    cache.set(
        key,
        CacheRecord(
            SERVICE_DOCUMENT.encode(),
            None,
            stale,
            "https://other.example/document",
            "yesterday",
            200,
            stale - timedelta(minutes=1),
        ),
    )
    conditional_transport = QueueTransport(
        response(b"", headers={"cache-control": "no-store"}, status=304)
    )
    conditional = ServiceClient(
        "https://demo.inflowpay.ai",
        cache=cache,
        transport=conditional_transport,
    )
    assert (await conditional.inspect()).freshness is Freshness.REVALIDATED
    assert conditional_transport.requests[0].headers["if-modified-since"] == "yesterday"

    uncached = ServiceClient(
        "https://demo.inflowpay.ai", transport=QueueTransport(response(b"", status=304))
    )
    with pytest.raises(AgentError, match="without a cached"):
        await uncached.inspect()

    posted = ServiceClient(
        "https://demo.inflowpay.ai",
        transport=QueueTransport(
            response(
                SERVICE_DOCUMENT.replace(
                    '"name":"list-offerings"}',
                    '"name":"list-offerings"},{"authentication":"not-required",'
                    '"name":"search-offerings"}',
                )
            ),
            response(b"", headers={"location": "/odp/offerings"}, status=302),
            response('{"items":[],"odp_version":"1.0"}'),
        ),
    )
    assert not (await posted.search_offerings(OfferingSearchRequest(query="x"))).items

    support_cache = MemoryCache()
    support_key = "anonymous:schema\nGET\nhttps://schemas.example/a\napplication/json"
    support_cache.set(
        support_key,
        CacheRecord(b"{}", None, stale, "", "yesterday", 200, stale),
    )
    supporting = ServiceClient(
        "https://demo.inflowpay.ai",
        cache=support_cache,
        supporting_transport=QueueTransport(
            response(b"", headers={"cache-control": "no-store"}, status=304)
        ),
    )
    assert (
        await supporting._supporting_json(
            "https://schemas.example/a", "schema", "application/json", {"application/json"}, 10
        )
        == {}
    )

    cacheable_support = ServiceClient(
        "https://demo.inflowpay.ai",
        supporting_transport=QueueTransport(
            response("{}", content_type="application/json", headers={"cache-control": "max-age=60"})
        ),
    )
    assert (
        await cacheable_support._supporting_json(
            "https://schemas.example/a", "schema", "application/json", {"application/json"}, 10
        )
        == {}
    )

    monkeypatch.setattr("offering_protocol.agent.client._MAXIMUM_REDIRECTS", 0)
    bounded_support = ServiceClient(
        "https://demo.inflowpay.ai",
        supporting_transport=QueueTransport(
            response(b"", headers={"location": "/again"}, status=302)
        ),
    )
    with pytest.raises(AgentError, match="five redirects"):
        await bounded_support._supporting_json(
            "https://schemas.example/a", "schema", "application/json", {"application/json"}, 10
        )

    request_action = Action(
        authentication=AuthenticationRequirement.REQUIRED,
        http=HttpActionTarget(
            href="/actions/purchase",
            method="POST",
            request=ActionRequest(content_type="application/json"),
        ),
        id="purchase",
        rel=ActionRelation.PURCHASE,
    )
    request_without_schema = (
        '{"actions":['
        + request_action.model_dump_json(by_alias=True, exclude_unset=True)
        + '],"id":"rubber-plant","name":"Rubber Plant","odp_version":"1.0"}'
    )
    request_client = ServiceClient(
        "https://demo.inflowpay.ai",
        transport=QueueTransport(response(SERVICE_DOCUMENT), response(request_without_schema)),
    )
    assert (await request_client.resolve_action("rubber-plant", "purchase")).request_schema is None


@pytest.mark.asyncio
async def test_federated_agent_uses_service_search_for_offering_queries() -> None:
    searchable = SERVICE_DOCUMENT.replace(
        '"name":"list-offerings"}',
        '"name":"list-offerings"},{"authentication":"not-required","name":"search-offerings"}',
    )
    agent = Agent(
        directory=DirectoryClient(
            transport=QueueTransport(response(DIRECTORY_PAGE, content_type="application/json"))
        ),
        factory=Factory(QueueTransport(response(searchable), response(OFFERING_PAGE))),
    )
    events = await agent.search_offerings_across_services(
        FederatedSearchRequest(offerings=OfferingSearchRequest(query="plant"))
    )
    assert events[0].offering is not None
