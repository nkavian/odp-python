"""Directory conformance: what an Agent may believe about what a Directory tells it.

ODP defines no directory wire format (ROLE-07, CNF-14), so almost nothing here is a wire-format
rule. What binds is ROLE-03: a Directory is a discovery aid indexing metadata it does not own, and
an Agent must not treat what it publishes as authoritative Service data. Every test below states one
consequence of that -- a record is validated before it is handed back, an unusable record costs only
itself, and nothing a Directory made up is passed off as a checked Service Document member.
"""

from __future__ import annotations

import json
from ipaddress import ip_address

import pytest

from helpers import QueueTransport, response
from offering_protocol.core.models import Protocol, TrustProtocol
from offering_protocol.directory import (
    DirectoryClient,
    DirectoryError,
    DirectoryRequestError,
    Environment,
    SearchPage,
    SearchRequest,
    ServiceFilters,
    SuggestionRequest,
)
from offering_protocol.directory.addresses import is_public

BASELINE_OPERATIONS = [
    {"authentication": "not-required", "name": "get-offering"},
    {"authentication": "not-required", "name": "list-offerings"},
]
SERVICE: dict[str, object] = {
    "description": "An AI-enabled plant store.",
    "indexed_at": "2026-01-01T00:00:00Z",
    "language": "en",
    "localizations": ["en"],
    "name": "Plants",
    "operations": BASELINE_OPERATIONS,
    "service_origin": "https://plants.example",
}


def amend(**changes: object) -> dict[str, object]:
    return {**SERVICE, **changes}


def json_response(value: object, **kwargs: object) -> object:
    body = value if isinstance(value, str) else json.dumps(value)
    return response(body, content_type="application/json", **kwargs)  # type: ignore[arg-type]


def client(*replies: object) -> DirectoryClient:
    return DirectoryClient(Environment.PRODUCTION, transport=QueueTransport(*replies))  # type: ignore[arg-type]


async def read(*services: object, **page: object) -> SearchPage:
    body = json_response({"items": list(services), **page})
    return await client(body).search_services(SearchRequest())


# -- a record is validated before it is handed back ---------------------------------------


@pytest.mark.asyncio
async def test_reads_a_conformant_record() -> None:
    page = await read(SERVICE)

    assert len(page.items) == 1
    assert not page.issues
    assert page.items[0].service_origin == "https://plants.example"
    assert page.items[0].name == "Plants"


@pytest.mark.asyncio
async def test_normalizes_unknown_operations_before_model_decoding() -> None:
    page = await read(
        SERVICE,
        amend(
            operations=[
                *BASELINE_OPERATIONS,
                {"name": "future-operation", "authentication": "not-required"},
            ]
        ),
    )
    assert len(page.items) == 2
    assert not page.issues
    assert len(page.items[1].operations) == 2


@pytest.mark.asyncio
async def test_model_decode_failure_is_isolated_to_its_record() -> None:
    page = await read(SERVICE, amend(website_url=None), SERVICE)
    assert len(page.items) == 2
    assert len(page.issues) == 1
    assert page.issues[0].index == 1


@pytest.mark.asyncio
async def test_refuses_an_origin_that_is_not_a_canonical_https_origin() -> None:
    """IDN-01: a Service is identified by its canonical origin, so two spellings are not one."""
    for origin in (
        "https://PLANTS.example",
        "https://plants.example:443",
        "https://plants.example/odp",
        "https://plants.example?a=1",
        "http://plants.example",
        "not a url",
        "",
    ):
        page = await read(amend(service_origin=origin))
        assert not page.items, origin
        assert page.issues[0].index == 0, origin


@pytest.mark.asyncio
async def test_refuses_an_origin_that_is_not_a_string() -> None:
    page = await read(amend(service_origin=None), amend(service_origin=["https://a.example"]))

    assert not page.items
    assert len(page.issues) == 2


@pytest.mark.asyncio
async def test_refuses_an_origin_on_a_private_or_loopback_host() -> None:
    """A public Directory has no business pointing an Agent inside its own network.

    The default transport refuses these too, but a consumer who installed their own transport, or
    who enabled local development for a Service of their own, would otherwise have no guard left.
    """
    for origin in (
        "https://127.0.0.1",
        "https://10.0.0.1",
        "https://169.254.169.254",
        "https://[::1]",
        "https://[64:ff9b::a9fe:a9fe]",
    ):
        page = await read(amend(service_origin=origin))
        assert not page.items, origin
        assert "private or loopback" in page.issues[0].message, origin


@pytest.mark.asyncio
async def test_refuses_an_indexed_at_that_is_not_an_rfc_3339_date_time() -> None:
    """A caller compares or slices this value, so it needs one shape rather than whatever parsed."""
    for value in ("yesterday", "2026-01-01", "December 17, 1995", "2026-01-01T00:00:00", 17, None):
        page = await read(amend(indexed_at=value))
        assert not page.items, value
        assert "RFC 3339" in page.issues[0].message, value

    for value in ("2026-01-01T00:00:00Z", "2026-01-01t00:00:00.123z", "2026-01-01T00:00:00+02:00"):
        assert (await read(amend(indexed_at=value))).items, value


@pytest.mark.asyncio
async def test_holds_echoed_service_document_members_to_the_service_document_rules() -> None:
    """A record echoes Service-owned metadata.

    Echoing it does not lower the bar it has to clear: a Directory that published
    `"language": "not a tag"` would otherwise hand a caller a tag no language matcher can read.
    """
    changes: tuple[dict[str, object], ...] = (
        {"language": "not a tag"},
        {"localizations": ["fr"]},
        {"localizations": ["en", "EN"]},
        {"operations": []},
        {"operations": [{"authentication": "biometric", "name": "list-offerings"}]},
        {"name": ""},
        {"website_url": "javascript:alert(1)"},
    )
    for change in changes:
        page = await read(amend(**change))
        assert not page.items, change
        assert page.issues[0].index == 0, change


# -- one unusable record costs only itself (ROLE-03) ---------------------------------------


@pytest.mark.asyncio
async def test_keeps_the_services_beside_an_unusable_record() -> None:
    """Rejecting the page made every other Service in the result undiscoverable."""
    page = await read(
        SERVICE,
        amend(service_origin="not a url", name="Broken"),
        amend(service_origin="https://ferns.example", name="Ferns"),
    )

    assert [item.name for item in page.items] == ["Plants", "Ferns"]
    assert [issue.index for issue in page.issues] == [1]


@pytest.mark.asyncio
async def test_reports_each_unusable_record_at_its_own_position() -> None:
    page = await read(amend(indexed_at="yesterday"), SERVICE, amend(language="not a tag"))

    assert len(page.items) == 1
    assert [issue.index for issue in page.issues] == [0, 2]
    assert all(issue.message for issue in page.issues)


@pytest.mark.asyncio
async def test_reports_an_entry_that_is_not_an_object() -> None:
    page = await read("https://plants.example", 17, None)

    assert not page.items
    assert len(page.issues) == 3


# -- what a Directory says about protocols --------------------------------------------------


@pytest.mark.asyncio
async def test_drops_a_protocol_this_odp_version_does_not_name() -> None:
    page = await read(
        amend(protocols={"payments": [{"authentication": "not-required", "name": "future"}]})
    )

    assert page.items[0].protocols is None
    assert not page.issues


@pytest.mark.asyncio
async def test_keeps_a_protocol_this_odp_version_names() -> None:
    page = await read(amend(protocols={"trust": [{"name": "tap"}]}))

    protocols = page.items[0].protocols
    assert protocols is not None
    assert protocols.trust[0].name.value == "tap"


@pytest.mark.asyncio
async def test_refuses_a_recognized_descriptor_that_breaks_its_own_rules() -> None:
    """SVC-51: filtering the unknown does not make what remains valid."""
    page = await read(
        amend(protocols={"payments": [{"authentication": "not-required", "name": "mpp", "x": 1}]})
    )

    assert not page.items
    assert page.issues[0].index == 0


# -- nothing unchecked is passed off as checked -----------------------------------------------


@pytest.mark.asyncio
async def test_drops_service_document_members_it_does_not_validate() -> None:
    """A caller reading these off a record cannot tell they were never checked.

    `http` is the one that matters: a caller could build request URLs from an `endpoint_base` the
    Directory invented, which is exactly the authority ROLE-03 says a Directory does not have.
    """
    page = await read(
        amend(
            branding={"icon": {"src": "/i.png"}, "logo": {"src": "/l.png"}},
            http={"endpoint_base": "/somewhere-else"},
            mcp=[{"type": "streamable-http", "url": "https://elsewhere.example/mcp"}],
            odp_version="1.0",
            payment_origins=["https://pay.example"],
            search_capabilities={"filters": {"inline": []}},
        )
    )

    assert not set(page.items[0].additional)


@pytest.mark.asyncio
async def test_keeps_the_directory_owned_members_it_was_given() -> None:
    """Directory-owned signals are the Directory's to publish, and are passed through untouched."""
    page = await read(amend(ranking_score=0.94, verified=True))

    assert page.items[0].additional == {"ranking_score": 0.94, "verified": True}


# -- page-level rules -------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_refuses_a_page_carrying_more_services_than_one_page_holds() -> None:
    page = json_response({"items": [SERVICE] * 101})
    with pytest.raises(DirectoryError, match="100 Services"):
        await client(page).search_services(SearchRequest())


@pytest.mark.asyncio
async def test_refuses_a_body_that_is_not_a_search_page() -> None:
    for body in ("not json", "[]", '{"facets":{}}', '{"items":"none"}'):
        with pytest.raises(DirectoryError):
            await client(json_response(body)).search_services(SearchRequest())


@pytest.mark.asyncio
async def test_refuses_trust_facets_naming_another_protocol() -> None:
    """`tap` is the only trust protocol this ODP version names, so anything else is unreadable."""
    facets = {"trust": [{"count": 2, "value": {"name": "mpp"}}]}
    with pytest.raises(DirectoryError, match="trust facets"):
        await read(SERVICE, facets=facets)

    page = await read(SERVICE, facets={"trust": [{"count": 2, "value": {"name": "tap"}}]})
    assert page.facets is not None
    assert page.facets.trust[0].count == 2


# -- continuations stay on the canonical Directory -----------------------------------------------


@pytest.mark.asyncio
async def test_keeps_a_continuation_on_the_canonical_origin() -> None:
    directory = client(json_response({"items": [SERVICE]}))
    await directory.continue_search_services("/v1/services/search?cursor=2")

    assert directory._transport.requests[0].url == (  # type: ignore[attr-defined]
        "https://api.inflowpay.ai/v1/services/search?cursor=2"
    )


@pytest.mark.asyncio
async def test_refuses_a_continuation_that_leaves_the_canonical_origin() -> None:
    for reference in (
        "https://elsewhere.example/next",
        "//elsewhere.example/next",
        "https://user@api.inflowpay.ai/next",
        "mailto:someone@example.com",
    ):
        with pytest.raises(DirectoryError):
            await client().continue_search_services(reference)

    # A continuation format is the Directory's to define, so an ordinary relative reference is
    # followed -- what is checked is where it lands, not how it was spelled.
    directory = client(json_response({"items": []}))
    await directory.continue_search_services("services?cursor=2")
    assert directory._transport.requests[0].url.startswith(  # type: ignore[attr-defined]
        "https://api.inflowpay.ai/"
    )


@pytest.mark.asyncio
async def test_refuses_a_redirect_that_leaves_the_origin_or_points_nowhere() -> None:
    for location in ("https://elsewhere.example/x", "mailto:someone@example.com"):
        redirect = json_response("", status=302, headers={"location": location})
        with pytest.raises(DirectoryError):
            await client(redirect).search_services(SearchRequest())

    missing = json_response("", status=302)
    with pytest.raises(DirectoryError, match="Location"):
        await client(missing).search_services(SearchRequest())


# -- requests this client will not send ------------------------------------------------------------


@pytest.mark.asyncio
async def test_refuses_a_limit_that_is_not_a_count() -> None:
    """Zero means the Directory decides; anything below one asks for nothing."""
    for limit in (-1, 101):
        with pytest.raises(DirectoryError, match="1 through 100"):
            await client().search_services(SearchRequest(limit=limit))
    for limit in (-1, 26):
        with pytest.raises(DirectoryError, match="1 through 25"):
            await client().suggest_services(SuggestionRequest(prefix="pl", limit=limit))


@pytest.mark.asyncio
async def test_sends_the_limits_it_accepts() -> None:
    directory = client(json_response({"items": []}))
    await directory.search_services(SearchRequest(limit=100))
    assert json.loads(directory._transport.requests[0].body) == {"limit": 100}  # type: ignore[attr-defined]

    directory = client(json_response({"items": []}))
    await directory.suggest_services(SuggestionRequest(prefix="pl", limit=25))
    assert "limit=25" in directory._transport.requests[0].url  # type: ignore[attr-defined]

    directory = client(json_response({"items": []}))
    await directory.suggest_services(SuggestionRequest(prefix="pl"))
    assert "limit" not in directory._transport.requests[0].url  # type: ignore[attr-defined]


@pytest.mark.asyncio
async def test_refuses_a_query_or_keyword_set_it_cannot_send() -> None:
    for request in (
        SearchRequest(query=" padded "),
        SearchRequest(query="x" * 513),
        SearchRequest(filters=ServiceFilters(keywords=["x"] * 33)),
        SearchRequest(filters=ServiceFilters(keywords=[""])),
        SearchRequest(filters=ServiceFilters(keywords=["x" * 65])),
    ):
        with pytest.raises(DirectoryError):
            await client().search_services(request)


@pytest.mark.asyncio
async def test_refuses_a_trust_filter_that_is_not_the_one_descriptor_odp_names() -> None:
    for trust in ([], [TrustProtocol(name=Protocol.TAP), TrustProtocol(name=Protocol.TAP)]):
        with pytest.raises(DirectoryError, match="tap"):
            await client().search_services(SearchRequest(filters=ServiceFilters(trust=trust)))

    directory = client(json_response({"items": []}))
    await directory.search_services(
        SearchRequest(filters=ServiceFilters(trust=[TrustProtocol(name=Protocol.TAP)]))
    )
    assert directory._transport.requests  # type: ignore[attr-defined]


# -- refused requests and oversized responses -----------------------------------------------------


@pytest.mark.asyncio
async def test_reports_a_refused_request_by_its_status() -> None:
    """A refused request is a status, not a size complaint, whatever the body looked like."""
    with pytest.raises(DirectoryRequestError) as raised:
        await client(json_response("x" * 600_000, status=503)).search_services(SearchRequest())

    assert raised.value.status == 503
    assert len(str(raised.value)) < 4_096


@pytest.mark.asyncio
async def test_refuses_a_response_larger_than_one_page_may_be() -> None:
    body = json.dumps({"items": [], "padding": "x" * 600_000})
    with pytest.raises(DirectoryError, match="524288"):
        await client(json_response(body)).search_services(SearchRequest())


@pytest.mark.asyncio
async def test_refuses_a_response_that_is_not_json() -> None:
    with pytest.raises(DirectoryError, match="application/json"):
        await client(response('{"items":[]}', content_type="text/html")).search_services(
            SearchRequest()
        )


# -- suggestions are the Directory's index, not Offering-search values ----------------------------


@pytest.mark.asyncio
async def test_reads_suggestions_the_directory_returned() -> None:
    directory = client(json_response({"items": ["plants", "planters"]}))

    assert await directory.suggest_services(SuggestionRequest(prefix="plan")) == [
        "plants",
        "planters",
    ]


@pytest.mark.asyncio
async def test_refuses_suggestions_it_cannot_use() -> None:
    for body in (
        '{"suggestions":[]}',
        '["plants", 17]',
        '["plants", ""]',
        '["plants", " padded "]',
        f'["{"x" * 129}"]',
        json.dumps(["s"] * 26),
        "not json",
    ):
        with pytest.raises(DirectoryError):
            await client(json_response(body)).suggest_services(SuggestionRequest(prefix="plan"))


@pytest.mark.asyncio
async def test_refuses_a_prefix_it_cannot_send() -> None:
    for prefix in ("", "   ", "x" * 129):
        with pytest.raises(DirectoryError, match="prefix"):
            await client().suggest_services(SuggestionRequest(prefix=prefix))


# -- addresses the public internet does not route -------------------------------------------------


def test_judges_an_address_by_the_special_purpose_registries() -> None:
    """SEC-08, and the reason this table exists rather than `ipaddress.is_global`.

    The IPv6 transition ranges each embed an IPv4 address: without them a name resolving to
    `64:ff9b::a9fe:a9fe` reaches link-local 169.254.169.254 through a NAT64 gateway -- the cloud
    metadata endpoint. The standard library reports that address, and five other registry ranges,
    as globally routable.
    """
    for value in ("8.8.8.8", "1.1.1.1", "2606:4700::1111", "::ffff:8.8.8.8"):
        assert is_public(ip_address(value)), value

    for value in (
        "0.0.0.0",
        "10.0.0.1",
        "100.64.0.1",
        "127.0.0.1",
        "169.254.169.254",
        "172.16.0.1",
        "192.0.0.1",
        "192.0.2.1",
        "192.31.196.1",
        "192.88.99.1",
        "192.168.1.1",
        "192.175.48.1",
        "198.18.0.1",
        "198.51.100.1",
        "203.0.113.1",
        "224.0.0.1",
        "240.0.0.1",
        "::",
        "::1",
        "::ffff:169.254.169.254",
        "64:ff9b::a9fe:a9fe",
        "64:ff9b:1::1",
        "100::1",
        "2001::1",
        "2001:db8::1",
        "2002:a9fe:a9fe::1",
        "2620:4f:8000::1",
        "5f00::1",
        "fc00::1",
        "fe80::1",
        "fec0::1",
        "ff02::1",
    ):
        assert not is_public(ip_address(value)), value


def test_reads_an_ipv4_mapped_address_as_the_address_it_carries() -> None:
    assert is_public(ip_address("::ffff:8.8.8.8"))
    assert not is_public(ip_address("::ffff:10.0.0.1"))


@pytest.mark.asyncio
async def test_refuses_a_page_whose_own_members_are_unreadable() -> None:
    """Per-record tolerance does not extend to the page around the records.

    A record this client cannot read is one Service it cannot offer; a page it cannot read is a
    result set of unknown shape, and there is nothing to hand back.
    """
    for page in ({"items": [], "next": 17}, {"items": [], "facets": {"trust": "all"}}):
        with pytest.raises(DirectoryError, match="invalid Directory response"):
            await client(json_response(page)).search_services(SearchRequest())


@pytest.mark.asyncio
async def test_accepts_an_origin_that_is_a_public_address_literal() -> None:
    """The host check is about where the address routes, not about it being a name."""
    page = await read(amend(service_origin="https://8.8.8.8"))

    assert page.items[0].service_origin == "https://8.8.8.8"
    assert not page.issues
