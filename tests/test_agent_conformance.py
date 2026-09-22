"""Agent conformance: the rules an ODP Agent applies to what somebody else wrote.

Every document reaching the Agent -- a Service Document, a capability page, a Problem Details
response, an OpenAPI document -- is written by the Service, so each test here states one rule the
Agent enforces on that input and shows the Agent refusing the input that breaks it.
"""

from __future__ import annotations

import json
from datetime import timedelta

import pytest

from helpers import SERVICE_DOCUMENT, QueueTransport, response
from offering_protocol.agent import AgentError, ServiceClient, ServiceRequestError
from offering_protocol.agent.cache import utc_now
from offering_protocol.agent.capabilities import (
    CapabilityKind,
    CapabilityScope,
    SearchCapabilityCatalog,
    _add_filters,
    _add_sorts,
    _load_filters,
    _load_sorts,
)
from offering_protocol.agent.client import (
    _MAXIMUM_DEPTH,
    _MAXIMUM_DOCUMENT_DEPTH,
    _consume,
    _expiration,
    _nesting_depth,
)
from offering_protocol.core import (
    CapabilityLink,
    CollectionSearchRequest,
    FilterCapabilitySource,
    FilterDefinition,
    FilterOperator,
    FilterType,
    MissingPlacement,
    SearchCapabilities,
    SortCapabilitySource,
    SortDefinition,
    SortDirection,
    SortKey,
)
from offering_protocol.directory.transport import HttpResponse

ORIGIN = "https://plants.example"


def _client(*replies: HttpResponse) -> ServiceClient:
    return ServiceClient(ORIGIN, transport=QueueTransport(*replies))


def _filter(identifier: str) -> FilterDefinition:
    return FilterDefinition(
        description="How heavy the plant is.",
        id=identifier,
        operators=[FilterOperator.EQUAL],
        title="Weight",
        type=FilterType.NUMBER,
    )


def _sort(identifier: str, filter_id: str = "weight") -> SortDefinition:
    return SortDefinition(
        description="Orders plants by weight.",
        id=identifier,
        keys=[
            SortKey(
                direction=SortDirection.ASCENDING,
                filter_id=filter_id,
                missing=MissingPlacement.LAST,
            )
        ],
        title="Lightest first",
    )


def _filter_page(identifiers: list[str], next_reference: str = "") -> str:
    page: dict[str, object] = {
        "odp_version": "1.0",
        "items": [
            json.loads(_filter(identifier).model_dump_json(by_alias=True, exclude_unset=True))
            for identifier in identifiers
        ],
    }
    if next_reference:
        page["next"] = next_reference
    return json.dumps(page)


async def _merge_filters(
    result: SearchCapabilityCatalog,
    scope: CapabilityScope,
    source: FilterCapabilitySource,
    client: ServiceClient | None = None,
) -> None:
    await _add_filters(
        client,  # type: ignore[arg-type]
        result,
        scope,
        SearchCapabilities(filters=source),
    )


# -- linked capability sources stay on the Service origin ------------------------------------------


@pytest.mark.asyncio
async def test_refuses_a_cross_origin_linked_source() -> None:
    """FLT-52: `linked.href` is a same-origin Resource Reference.

    The Service Document that names it is written by the Service, so a cross-origin `href` would
    let a Service point this Agent's ODP requests at a host of its choosing.
    """
    client = _client(response(_filter_page([])))
    with pytest.raises(AgentError, match="Service origin"):
        await _load_filters(client, "https://elsewhere.example/filters")
    assert not client._transport.requests  # type: ignore[attr-defined]


@pytest.mark.asyncio
async def test_refuses_a_cross_origin_continuation() -> None:
    """FLT-53: a linked page's `next` obeys the common continuation contract.

    That contract keeps a continuation on the Service origin, so a page can no more redirect this
    Agent off the Service than the advertisement that named the source could.
    """
    client = _client(response(_filter_page(["weight"], "https://elsewhere.example/page-2")))
    with pytest.raises(AgentError, match="Service origin"):
        await _load_filters(client, "/odp/filters")
    assert [request.url for request in client._transport.requests] == [  # type: ignore[attr-defined]
        f"{ORIGIN}/odp/filters"
    ]


@pytest.mark.asyncio
async def test_follows_a_same_origin_source_and_its_continuations() -> None:
    client = _client(
        response(_filter_page(["weight"], "/odp/filters?page=2")),
        response(_filter_page(["height"])),
    )
    values = await _load_filters(client, "/odp/filters")

    assert [value.id for value in values] == ["weight", "height"]


@pytest.mark.asyncio
async def test_refuses_a_reference_that_is_not_an_odp_reference() -> None:
    for reference in ("data:text/plain,x", "//elsewhere.example/filters", "filters"):
        with pytest.raises(AgentError):
            await _load_filters(_client(), reference)


# -- a source is atomic ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_repeat_within_one_source_discards_that_source() -> None:
    """FLT-55: a source enforces its own uniqueness before any of it is exposed.

    Two definitions under one identifier in one source give the Agent no basis for choosing between
    them, so the source is unusable rather than partly usable.
    """
    result = SearchCapabilityCatalog()
    await _merge_filters(
        result,
        CapabilityScope.SERVICE,
        FilterCapabilitySource(inline=[_filter("weight"), _filter("weight"), _filter("height")]),
    )

    assert not result.filters
    assert len(result.issues) == 1
    assert "within one source" in result.issues[0].message
    assert result.issues[0].kind is CapabilityKind.FILTERS


@pytest.mark.asyncio
async def test_a_repeat_within_one_source_leaves_earlier_sources_alone() -> None:
    result = SearchCapabilityCatalog()
    await _merge_filters(
        result, CapabilityScope.SERVICE, FilterCapabilitySource(inline=[_filter("weight")])
    )
    await _merge_filters(
        result,
        CapabilityScope.COLLECTION,
        FilterCapabilitySource(inline=[_filter("height"), _filter("height")]),
    )

    assert sorted(result.filters) == ["weight"]


@pytest.mark.asyncio
async def test_an_identifier_two_sources_publish_is_quarantined() -> None:
    """One identifier published by two effective sources is quarantined -- neither copy wins.

    That removes the identifier and nothing else, which is what tells this rule apart from the
    within-source repeat above.
    """
    result = SearchCapabilityCatalog()
    await _merge_filters(
        result,
        CapabilityScope.SERVICE,
        FilterCapabilitySource(inline=[_filter("weight"), _filter("service-only")]),
    )
    await _merge_filters(
        result,
        CapabilityScope.COLLECTION,
        FilterCapabilitySource(inline=[_filter("weight"), _filter("collection-only")]),
    )

    assert sorted(result.filters) == ["collection-only", "service-only"]
    assert result.issues[-1].message == "Duplicate filters: weight"
    assert result.issues[-1].scope is CapabilityScope.COLLECTION


# -- effective-catalog bounds ---------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_source_that_overflows_the_bound_leaves_earlier_sources_intact() -> None:
    """FLT-62: exceeding the bound invalidates the source that caused it, and only that source."""
    result = SearchCapabilityCatalog()
    await _merge_filters(
        result, CapabilityScope.SERVICE, FilterCapabilitySource(inline=[_filter("weight")])
    )
    # 1025 new definitions beside the one already merged cannot fit, even after the identifier
    # both sources publish is quarantined.
    overflowing = [_filter(f"f{index}") for index in range(1025)] + [_filter("weight")]
    await _merge_filters(
        result, CapabilityScope.COLLECTION, FilterCapabilitySource(inline=overflowing)
    )

    assert sorted(result.filters) == ["weight"]
    assert "Effective filters exceed their limit" in result.issues[-1].message


@pytest.mark.asyncio
async def test_a_source_that_exactly_fills_the_bound_is_accepted() -> None:
    result = SearchCapabilityCatalog()
    await _merge_filters(
        result, CapabilityScope.SERVICE, FilterCapabilitySource(inline=[_filter("weight")])
    )
    exact = [_filter(f"f{index}") for index in range(1023)]
    await _merge_filters(result, CapabilityScope.COLLECTION, FilterCapabilitySource(inline=exact))

    assert len(result.filters) == 1024
    assert not result.issues


@pytest.mark.asyncio
async def test_paging_stops_at_the_page_that_overflows_the_bound() -> None:
    """FLT-58: a source that cannot fit costs one page rather than sixteen."""
    page = _filter_page([f"f{index}" for index in range(100)], "/odp/filters?page=next")
    client = _client(*[response(page) for _ in range(16)])

    with pytest.raises(AgentError, match="exceed their limit"):
        await _load_filters(client, "/odp/filters", budget=50)

    assert len(client._transport.requests) == 1  # type: ignore[attr-defined]


@pytest.mark.asyncio
async def test_a_source_whose_last_page_still_offers_another_is_discarded() -> None:
    """FLT-59: page 16 carrying `next` means page 17 is never retrieved."""
    pages = [
        response(_filter_page([f"p{index}"], f"/odp/filters?page={index + 1}"))
        for index in range(16)
    ]
    client = _client(*pages)

    with pytest.raises(AgentError, match="16 pages"):
        await _load_filters(client, "/odp/filters")

    assert len(client._transport.requests) == 16  # type: ignore[attr-defined]


@pytest.mark.asyncio
async def test_sorts_follow_the_same_source_rules() -> None:
    result = SearchCapabilityCatalog()
    target: dict[str, SortDefinition] = {}
    scopes: dict[str, CapabilityScope] = {}
    await _add_sorts(
        _client(),
        result,
        target,
        scopes,
        CapabilityScope.SERVICE,
        SearchCapabilities(sorts=SortCapabilitySource(inline=[_sort("light"), _sort("light")])),
    )

    assert not target and not scopes
    assert "within one source" in result.issues[-1].message


@pytest.mark.asyncio
async def test_a_linked_sort_source_stays_on_the_service_origin() -> None:
    result = SearchCapabilityCatalog()
    await _add_sorts(
        _client(),
        result,
        {},
        {},
        CapabilityScope.SERVICE,
        SearchCapabilities(
            sorts=SortCapabilitySource(linked=CapabilityLink(href="https://elsewhere.example/s"))
        ),
    )

    assert "Service origin" in result.issues[-1].message
    assert result.issues[-1].kind is CapabilityKind.SORTS


@pytest.mark.asyncio
async def test_reads_a_linked_source_into_the_catalog() -> None:
    result = SearchCapabilityCatalog()
    client = _client(response(_filter_page(["weight", "height"])))
    await _merge_filters(
        result,
        CapabilityScope.SERVICE,
        FilterCapabilitySource(linked=CapabilityLink(href="/odp/filters")),
        client,
    )

    assert sorted(result.filters) == ["height", "weight"]
    assert not result.issues


@pytest.mark.asyncio
async def test_loading_sorts_returns_what_a_complete_source_advertised() -> None:
    sorts = json.dumps(
        {
            "odp_version": "1.0",
            "items": [
                json.loads(_sort("light").model_dump_json(by_alias=True, exclude_unset=True))
            ],
        }
    )
    values = await _load_sorts(_client(response(sorts)), "/odp/sorts")

    assert [value.id for value in values] == ["light"]


# -- nesting depth --------------------------------------------------------------------------------


def test_measures_nesting_from_the_top_level_value() -> None:
    """ERR-18: a scalar is a value a container holds, not a level of its own."""
    assert _nesting_depth(3) == 0
    assert _nesting_depth({}) == 1
    assert _nesting_depth({"a": 1}) == 1
    assert _nesting_depth({"a": {"b": 1}}) == 2
    assert _nesting_depth([[{"a": 1}]]) == 3
    assert _nesting_depth({"shallow": 1, "deep": {"a": {"b": 1}}}) == 3


def _nested(levels: int) -> bytes:
    value: dict[str, object] = {}
    cursor = value
    for _ in range(levels - 1):
        child: dict[str, object] = {}
        cursor["a"] = child
        cursor = child
    return json.dumps(value).encode()


def _odp(body: bytes, status: int = 200) -> HttpResponse:
    return HttpResponse(status, {"content-type": "application/odp+json"}, body)


def test_refuses_a_response_nested_deeper_than_odp_allows() -> None:
    """ERR-21: every ODP document except the Service Document nests no deeper than 16."""
    assert _consume(_odp(_nested(16)), 524_288, _MAXIMUM_DEPTH).status == 200
    with pytest.raises(AgentError, match="nesting-depth"):
        _consume(_odp(_nested(17)), 524_288, _MAXIMUM_DEPTH)


def test_refuses_a_service_document_nested_deeper_than_its_own_limit() -> None:
    """ERR-21: the Service Document has the tighter allowance of 8."""
    assert _consume(_odp(_nested(8)), 65_536, _MAXIMUM_DOCUMENT_DEPTH).status == 200
    with pytest.raises(AgentError, match="nesting-depth"):
        _consume(_odp(_nested(9)), 65_536, _MAXIMUM_DOCUMENT_DEPTH)


@pytest.mark.asyncio
async def test_refuses_a_deeply_nested_service_document_over_http() -> None:
    document = json.loads(SERVICE_DOCUMENT)
    cursor = document.setdefault("branding", {})
    for _ in range(12):
        cursor["a"] = {}
        cursor = cursor["a"]
    client = _client(response(json.dumps(document)))

    with pytest.raises(AgentError, match="nesting-depth"):
        await client.inspect()


def test_leaves_a_malformed_body_to_the_document_parser() -> None:
    """A body that is not JSON is reported by the parser, which can say what is wrong with it."""
    assert _consume(_odp(b"not json"), 524_288, _MAXIMUM_DEPTH).body == b"not json"


# -- refused requests -----------------------------------------------------------------------------


def _problem(detail: str) -> bytes:
    return json.dumps(
        {
            "type": "https://offeringprotocol.org/problems/not-found",
            "title": "Not found",
            "status": 404,
            "code": "NOT_FOUND",
            "detail": detail,
        }
    ).encode()


def test_reports_the_detail_a_problem_document_gave() -> None:
    with pytest.raises(ServiceRequestError) as raised:
        _consume(_odp(_problem("No such Offering."), 404), 524_288, _MAXIMUM_DEPTH)

    assert raised.value.status == 404
    assert "No such Offering." in str(raised.value)


def test_reads_an_error_body_only_within_the_problem_details_limit() -> None:
    """ERR-21 budgets a Problem Details response at 16,384 bytes.

    A larger body is not a Problem Details document this Agent will read, so the status still
    describes the failure rather than the size becoming the failure.
    """
    oversized = b" " * 20_000 + _problem("No such Offering.")
    with pytest.raises(ServiceRequestError) as raised:
        _consume(_odp(oversized, 404), 524_288, _MAXIMUM_DEPTH)

    assert raised.value.status == 404
    assert "No such Offering." not in str(raised.value)


def test_reports_an_error_body_that_is_not_a_problem_document() -> None:
    with pytest.raises(ServiceRequestError, match="upstream exploded"):
        _consume(_odp(b"upstream exploded", 503), 524_288, _MAXIMUM_DEPTH)


def test_checks_the_status_before_the_representation_limits() -> None:
    """A refused request is reported as refused, whatever the error body looked like."""
    with pytest.raises(ServiceRequestError):
        _consume(HttpResponse(500, {"content-type": "text/html"}, b"<h1>oops</h1>"), 10, 16)


# -- freshness ------------------------------------------------------------------------------------


def test_reads_an_expires_written_with_an_unknown_zone() -> None:
    """RFC 5322's "-0000" means an unknown zone, which parses to a value carrying no zone at all.

    A cache that stored one would raise the next time it compared that value against its clock, so
    the zone ODP means -- UTC -- is supplied here instead.
    """
    now = utc_now()
    expires = _expiration({"expires": "Thu, 01 Dec 2050 16:00:00 -0000"}, timedelta(), now)

    assert expires.tzinfo is not None
    assert now < expires


def test_treats_an_unreadable_expires_as_already_expired() -> None:
    """RFC 9111 5.3: an invalid `Expires`, "0" above all, names a time in the past."""
    now = utc_now()

    assert _expiration({"expires": "0"}, timedelta(hours=1), now) == now
    assert _expiration({"expires": "not a date"}, timedelta(hours=1), now) == now
    assert _expiration({"expires": "Wed, 21 Oct 2037 07:28:00 GMT"}, timedelta(), now) > now


@pytest.mark.asyncio
async def test_does_not_reuse_a_representation_whose_expires_was_unreadable() -> None:
    """The whole point of the rule above: the next request revalidates rather than reusing."""
    headers = {"cache-control": "public", "etag": '"v1"', "expires": "0"}
    client = _client(
        response(SERVICE_DOCUMENT, headers=headers),
        response(SERVICE_DOCUMENT, headers=headers),
    )
    await client.inspect()
    await client.inspect()

    requests = client._transport.requests  # type: ignore[attr-defined]
    assert len(requests) == 2
    assert requests[1].headers["if-none-match"] == '"v1"'


@pytest.mark.asyncio
async def test_quarantines_a_sort_two_sources_publish() -> None:
    """The cross-source rule applies to sorts, including the scope bookkeeping behind them."""
    result = SearchCapabilityCatalog()
    target: dict[str, SortDefinition] = {}
    scopes: dict[str, CapabilityScope] = {}
    for scope in (CapabilityScope.SERVICE, CapabilityScope.COLLECTION):
        await _add_sorts(
            _client(),
            result,
            target,
            scopes,
            scope,
            SearchCapabilities(
                sorts=SortCapabilitySource(inline=[_sort("light"), _sort(f"{scope.value}-only")])
            ),
        )

    assert sorted(target) == ["collection-only", "service-only"]
    assert sorted(scopes) == ["collection-only", "service-only"]
    assert result.issues[-1].message == "Duplicate sorts: light"


def test_refuses_a_supporting_document_nested_past_the_parser(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A document deep enough to exhaust the JSON parser is refused, not raised as a RecursionError.

    An Attribute Schema carries no nesting-depth ceiling of its own, so depth alone can stop the
    parser before any limit is measured. What matters is that the failure reaches the caller as the
    unusable document it describes rather than as an interpreter error, which is what is asserted
    here -- the depth at which a given build of CPython actually gives up is not this SDK's to fix,
    and pinning a test to it would make the test a property of the platform.
    """
    from offering_protocol.agent.client import _decode_json_object

    def exhausted(*args: object, **kwargs: object) -> object:
        raise RecursionError("maximum recursion depth exceeded while decoding a JSON object")

    monkeypatch.setattr(json, "loads", exhausted)

    with pytest.raises(AgentError, match="nested too deeply"):
        _decode_json_object(b"[[[]]]")


@pytest.mark.asyncio
@pytest.mark.parametrize("search", [False, True])
async def test_collection_page_inherits_version_without_requiring_it_on_items(search: bool) -> None:
    document = json.loads(SERVICE_DOCUMENT)
    operation = "search-collections" if search else "list-collections"
    document["operations"].append({"name": operation, "authentication": "not-required"})
    transport = QueueTransport(
        response(json.dumps(document)),
        response('{"odp_version":"1.7","items":[{"id":"plants","name":"Plants"}]}'),
    )
    client = ServiceClient("https://store.example", transport=transport)
    page = (
        await client.search_collections(CollectionSearchRequest())
        if search
        else await client.list_collections()
    )
    assert page.odp_version == "1.7"
    assert page.items[0].id == "plants"
    assert "odp_version" not in page.items[0].model_fields_set


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "location",
    [
        "https://other.example/schema.json",
        "https://schemas.example:444/schema.json",
        "/schema.json",
    ],
)
async def test_supporting_redirects_refuse_origin_changes_and_loops(location: str) -> None:
    offering = {
        "odp_version": "1.0",
        "id": "item",
        "name": "Item",
        "schema": {"url": "https://schemas.example/schema.json"},
        "attributes": {"colour": "green"},
    }
    supporting = QueueTransport(response("", status=302, headers={"location": location}))
    client = ServiceClient(
        "https://store.example",
        transport=QueueTransport(response(SERVICE_DOCUMENT), response(json.dumps(offering))),
        supporting_transport=supporting,
    )
    details = await client.get_offering_details("item")
    assert len(supporting.requests) == 1
    assert len(details.issues) == 1
    assert "redirect" in details.issues[0].message
    assert details.offering.name == "Item"
    assert not details.offering.attributes


@pytest.mark.asyncio
async def test_supporting_redirects_allow_explicit_default_port() -> None:
    supporting = QueueTransport(
        response("", status=302, headers={"location": "https://schemas.example:443/final.json"}),
        response("{}", content_type="application/json"),
    )
    client = ServiceClient("https://store.example", supporting_transport=supporting)
    assert (
        await client._supporting_json(
            "https://schemas.example/start.json",
            "schema",
            "application/json",
            {"application/json"},
            100,
        )
        == {}
    )
    assert len(supporting.requests) == 2
