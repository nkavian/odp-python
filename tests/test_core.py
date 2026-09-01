from __future__ import annotations

import json
from collections.abc import Callable

import pytest

from helpers import OFFERING, OFFERING_PAGE, SERVICE_DOCUMENT
from offering_protocol.core import (
    OdpValidationError,
    Operation,
    Protocol,
    ReferenceError,
    ResourceType,
    build_operation_url,
    create_resource_identity,
    derive_service_origin,
    is_local_resource_identifier,
    operation_method,
    operation_path,
    parse_agent_service_document,
    parse_collection,
    parse_collection_page,
    parse_collection_search_request,
    parse_filter_definition,
    parse_filter_definition_page,
    parse_offering,
    parse_offering_page,
    parse_offering_search_request,
    parse_problem_response,
    parse_resource_identity,
    parse_service_document,
    parse_sort_definition,
    parse_sort_definition_page,
    resolve_continuation,
    resolve_resource_reference,
    validate_value,
)
from offering_protocol.core.validation import _normalize_agent_response


def test_normalizes_agent_response_capabilities() -> None:
    service = _normalize_agent_response(
        {
            "operations": [
                {"authentication": "not-required", "name": "list-offerings"},
                {"authentication": "not-required", "name": "future"},
                {"authentication": "not-required", "name": "get-offering", "future": True},
            ],
            "mcp": [
                {"type": "streamable-http", "url": "/mcp"},
                {"type": "future", "url": "/future"},
                {"type": "streamable-http", "url": "/future-member", "future": True},
            ],
            "branding": {
                "icon": {"src": "/icon", "type": "image/future"},
                "logo": {"src": "/logo", "type": "image/png", "future": True},
                "future": {},
            },
            "protocols": {
                "payments": [
                    {
                        "authentication": "not-required",
                        "name": "mpp",
                        "options": ["inflow", "future"],
                    }
                ]
            },
            "search_capabilities": {
                "filters": {
                    "inline": [
                        {"type": "string", "operators": ["eq"]},
                        {"type": "future", "operators": ["eq"]},
                    ]
                },
                "sorts": {
                    "inline": [
                        {"keys": [{"direction": "ascending", "missing": "last"}]},
                        {"keys": [{"direction": "future", "missing": "last"}]},
                    ]
                },
            },
        },
        "service-document",
    )
    assert service["operations"] == [{"authentication": "not-required", "name": "list-offerings"}]
    assert service["mcp"] == [{"type": "streamable-http", "url": "/mcp"}]
    assert service["branding"] == {"logo": {"src": "/logo", "type": "image/png"}}
    assert service["protocols"]["payments"][0]["options"] == ["inflow"]
    assert len(service["search_capabilities"]["filters"]["inline"]) == 1
    assert len(service["search_capabilities"]["sorts"]["inline"]) == 1

    offering = _normalize_agent_response(
        {
            "images": [
                {"src": "/image", "type": "image/png", "future": True},
                {"src": "/future", "type": "image/future"},
            ],
            "schema": {"url": "/schema", "future": True},
            "price": {"type": "future"},
            "actions": [
                {
                    "authentication": "future-authentication",
                    "http": {"href": "/unsupported", "method": "POST"},
                    "id": "unsupported",
                    "rel": "invoke",
                },
                {
                    "authentication": "not-required",
                    "http": {"href": "/run", "method": "POST"},
                    "id": "run",
                    "rel": "future",
                },
                {
                    "authentication": "not-required",
                    "http": {"href": "/future", "method": "PATCH"},
                    "id": "future",
                    "rel": "invoke",
                },
                {
                    "authentication": "not-required",
                    "http": {"href": "/future", "method": "POST", "future": True},
                    "id": "future-member",
                    "rel": "invoke",
                },
            ],
        },
        "offering",
    )
    assert offering == {
        "images": [{"src": "/image", "type": "image/png"}],
        "actions": [
            {
                "authentication": "not-required",
                "http": {"href": "/run", "method": "POST"},
                "id": "run",
                "rel": "future",
            }
        ],
    }

    assert (
        "payments"
        not in _normalize_agent_response(
            {
                "protocols": {
                    "payments": [{"authentication": "future-authentication", "name": "mpp"}]
                }
            },
            "service-document",
        )["protocols"]
    )

    assert _normalize_agent_response(
        {"items": [{"images": [{"src": "/future", "type": "image/future"}]}]},
        "collection-page",
    ) == {"items": [{}]}
    assert _normalize_agent_response(
        {"items": [{"type": "string", "operators": ["eq"]}, {"type": "future"}]},
        "filter-page",
    ) == {"items": [{"type": "string", "operators": ["eq"]}]}
    assert _normalize_agent_response(
        {"items": [{"keys": []}, {"keys": [{"missing": "future"}]}]}, "sort-page"
    ) == {"items": [{"keys": []}]}
    assert _normalize_agent_response(
        {"invalid_params": [{"in": "query"}, {"in": "future"}]}, "problem"
    ) == {"invalid_params": [{"in": "query"}]}


def test_normalizes_agent_response_boundary_edges() -> None:
    assert "operations" not in _normalize_agent_response(
        {"operations": [{"name": "future"}]}, "service-document"
    )
    assert "operations" not in _normalize_agent_response(
        {"operations": [{"name": "list-offerings", "future": True}]}, "service-document"
    )
    assert _normalize_agent_response(
        {
            "protocols": {
                "payments": [
                    {"authentication": "not-required", "name": "mpp", "options": ["future"]}
                ]
            }
        },
        "service-document",
    )["protocols"]["payments"][0] == {
        "authentication": "not-required",
        "name": "mpp",
    }
    assert "branding" not in _normalize_agent_response(
        {"branding": {"icon": {"src": "/icon", "type": "image/future"}}},
        "service-document",
    )
    assert "search_capabilities" not in _normalize_agent_response(
        {
            "search_capabilities": {
                "filters": {"inline": [{"type": "future"}]},
                "sorts": {"inline": [{"keys": [{"direction": "future"}]}]},
            }
        },
        "collection",
    )
    assert (
        _normalize_agent_response({"actions": [{"id": "future", "future": True}]}, "offering") == {}
    )
    assert (
        _normalize_agent_response(
            {"actions": [{"id": "future", "http": {"request": {"future": True}}}]},
            "offering",
        )
        == {}
    )
    assert (
        _normalize_agent_response(
            {"actions": [{"id": "future", "http": {"request": {"schema": {"future": True}}}}]},
            "offering",
        )
        == {}
    )
    assert (
        _normalize_agent_response(
            {"actions": [{"id": "future", "openapi": {"future": True}}]}, "offering"
        )
        == {}
    )
    assert _normalize_agent_response(
        {"items": [None, {"type": "string", "operators": ["future"]}]}, "filter-page"
    ) == {"items": [None]}
    assert _normalize_agent_response({"items": [None]}, "sort-page") == {"items": [None]}


def test_parses_normative_documents_and_preserves_extensions() -> None:
    document = parse_service_document(SERVICE_DOCUMENT[:-2] + ',"vendor":"example"}')
    assert document.name == "Indica Flowers"
    assert document.additional == {"vendor": "example"}
    assert document.to_dict()["vendor"] == "example"
    offering = parse_offering(OFFERING)
    assert offering.id == "rubber-plant"
    assert parse_offering_page(OFFERING_PAGE).items == [offering]
    collection = parse_collection('{"id":"plants","name":"Plants","odp_version":"1.0"}')
    page = parse_collection_page('{"items":[{"id":"plants","name":"Plants"}],"odp_version":"1.0"}')
    assert page.items[0].id == collection.id
    assert page.items[0].odp_version == ""


def test_parses_requests_capabilities_and_identity() -> None:
    assert (
        parse_collection_search_request('{"odp_version":"1.0","query":"plants"}').query == "plants"
    )
    assert parse_offering_search_request('{"odp_version":"1.0","query":"plants"}').query == "plants"
    filter_definition = parse_filter_definition(
        '{"description":"Color","id":"color","operators":["eq"],"title":"Color","type":"string"}'
    )
    assert (
        parse_filter_definition_page(
            '{"items":['
            + filter_definition.model_dump_json(by_alias=True, exclude_unset=True)
            + '],"odp_version":"1.0"}'
        )
        .items[0]
        .id
        == "color"
    )
    sort_definition = parse_sort_definition(
        '{"description":"Name","id":"name","keys":[{"direction":"ascending",'
        '"filter_id":"color","missing":"last"}],"title":"Name"}'
    )
    assert (
        parse_sort_definition_page(
            '{"items":[' + sort_definition.model_dump_json(by_alias=True) + '],"odp_version":"1.0"}'
        )
        .items[0]
        .id
        == "name"
    )
    identity = parse_resource_identity(
        '{"id":"rubber-plant","service":"https://plants.example","type":"offering"}'
    )
    assert identity.key == "https://plants.example\0offering\0rubber-plant"


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("plant", True),
        ("plant_1", True),
        ("", False),
        (".", False),
        ("bad/path", False),
        ("x" * 129, False),
    ],
)
def test_local_resource_identifier(value: str, expected: bool) -> None:
    assert is_local_resource_identifier(value) is expected


def test_resolves_origins_operations_and_references() -> None:
    assert derive_service_origin("https://EXAMPLE.com:443/path") == "https://example.com"
    assert derive_service_origin("http://localhost:8080/path") == "http://localhost:8080"
    assert operation_method(Operation.SEARCH_OFFERINGS) == "POST"
    assert operation_method(Operation.GET_OFFERING) == "GET"
    assert operation_path(Operation.GET_OFFERING, "plant") == "/offerings/plant"
    assert (
        operation_path(Operation.LIST_COLLECTION_OFFERINGS, "plants")
        == "/collections/plants/offerings"
    )
    assert operation_path(Operation.LIST_OFFERINGS) == "/offerings"
    assert (
        build_operation_url("/odp", Operation.GET_OFFERING, "https://example.com", "plant")
        == "https://example.com/odp/offerings/plant"
    )
    assert (
        resolve_resource_reference("/images/plant.png", "https://example.com")
        == "https://example.com/images/plant.png"
    )
    assert (
        resolve_continuation("/odp/offerings?cursor=one", "https://example.com")
        == "https://example.com/odp/offerings?cursor=one"
    )
    identity = create_resource_identity("https://EXAMPLE.com/path", ResourceType.OFFERING, "plant")
    assert identity.service == "https://example.com"


@pytest.mark.parametrize(
    "call",
    [
        lambda: derive_service_origin("ftp://example.com"),
        lambda: derive_service_origin("http://example.com"),
        lambda: derive_service_origin("https://user@example.com"),
        lambda: derive_service_origin("https://"),
        lambda: operation_path(Operation.GET_OFFERING),
        lambda: operation_path(Operation.LIST_OFFERINGS, "extra"),
        lambda: resolve_resource_reference("mailto:test@example.com", "https://example.com"),
        lambda: resolve_resource_reference("/path#fragment", "https://example.com"),
        lambda: resolve_continuation("https://other.example/path", "https://example.com"),
        lambda: create_resource_identity("https://example.com", ResourceType.OFFERING, "bad/id"),
    ],
)
def test_rejects_invalid_references(call: Callable[[], object]) -> None:
    with pytest.raises(ReferenceError):
        call()


def test_reports_schema_and_semantic_validation_issues() -> None:
    with pytest.raises(OdpValidationError) as malformed:
        parse_offering("{")
    assert malformed.value.issues[0].keyword == "json"
    with pytest.raises(OdpValidationError) as schema:
        parse_offering("{}")
    assert schema.value.document_type == "Offering"
    with pytest.raises(OdpValidationError) as prohibited:
        parse_service_document(SERVICE_DOCUMENT[:-2] + ',"id":"wrong"}')
    assert prohibited.value.issues[0].keyword == "prohibited"
    with pytest.raises(OdpValidationError):
        parse_service_document(SERVICE_DOCUMENT.replace('["en"]', '["EN","en"]'))
    with pytest.raises(OdpValidationError):
        parse_service_document(
            SERVICE_DOCUMENT.replace('"language":"en"', '"language":"en-a"').replace(
                '["en"]', '["en-a"]'
            )
        )
    with pytest.raises(OdpValidationError):
        parse_service_document(
            SERVICE_DOCUMENT.replace('"language":"en"', '"language":"sl-rozaj-rozaj"').replace(
                '["en"]', '["sl-rozaj-rozaj"]'
            )
        )
    with pytest.raises(OdpValidationError):
        parse_service_document(
            SERVICE_DOCUMENT.replace(
                '"name":"Indica', '"keywords":["' + "x" * 1025 + '"],"name":"Indica'
            )
        )
    with pytest.raises(OdpValidationError):
        parse_service_document(
            SERVICE_DOCUMENT[:-2]
            + ',"search_capabilities":{"filters":{"inline":[{"description":"Color",'
            '"id":"color","operators":["eq"],"title":"Color","type":"string"}]}}}'
        )
    with pytest.raises(OdpValidationError):
        parse_offering(OFFERING[:-2] + ',"images":[{"src":"/a"},{"src":"/a"}]}')
    with pytest.raises(OdpValidationError):
        parse_filter_definition(
            '{"description":"Color","id":"color","operators":["gt"],'
            '"title":"Color","type":"string"}'
        )
    with pytest.raises(OdpValidationError):
        parse_filter_definition(
            '{"description":"Available","id":"available","operators":["eq"],'
            '"title":"Available","type":"boolean","unit":{"code":"1",'
            '"system":"ucum"}}'
        )
    with pytest.raises(RuntimeError):
        validate_value({}, "missing.schema.json", "Missing")


def test_parses_tap_trust_protocol() -> None:
    document = parse_service_document(
        SERVICE_DOCUMENT[:-2] + ',"protocols":{"trust":[{"name":"tap"}]}}'
    )
    assert document.protocols is not None
    assert document.protocols.trust[0].name.value == "tap"


def test_agent_parser_filters_unknown_protocols_strictly() -> None:
    data = (
        SERVICE_DOCUMENT[:-2]
        + ',"protocols":{"enrollment":[{"name":"future-enrollment"},{"name":"aep"}],'
        '"payments":[{"authentication":"not-required","name":"future-payment"},'
        '{"authentication":"not-required","name":"mpp"},'
        '{"authentication":"not-required","name":"x402"}],'
        '"trust":[{"name":"future-trust"},{"name":"tap"}]}}'
    )
    with pytest.raises(OdpValidationError):
        parse_service_document(data)
    document = parse_agent_service_document(data)
    assert document.protocols is not None
    assert [value.name for value in document.protocols.enrollment] == [Protocol.AEP]
    assert [value.name for value in document.protocols.payments] == [Protocol.MPP, Protocol.X402]
    assert [value.name for value in document.protocols.trust] == [Protocol.TAP]

    unknown_only = SERVICE_DOCUMENT[:-2] + ',"protocols":{"trust":[{"name":"future"}]}}'
    assert parse_agent_service_document(unknown_only).protocols is None

    malformed = SERVICE_DOCUMENT[:-2] + ',"protocols":{"trust":[{"name":"tap","extra":true}]}}'
    with pytest.raises(OdpValidationError):
        parse_agent_service_document(malformed)

    with pytest.raises(OdpValidationError):
        parse_agent_service_document("invalid")
    with pytest.raises(OdpValidationError):
        parse_agent_service_document("[]")


def test_problem_status_must_match_http_status() -> None:
    body = json.dumps(
        {
            "code": "NOT_FOUND",
            "status": 404,
            "title": "Not found",
            "type": "https://offeringprotocol.org/problems/not-found",
        }
    )
    assert parse_problem_response(body, 404).code == "NOT_FOUND"
    with pytest.raises(OdpValidationError):
        parse_problem_response(body, 400)
    with pytest.raises(OdpValidationError):
        parse_problem_response(
            body.replace("problems/not-found", "problems/validation-failed"), 404
        )


def test_page_parsers_apply_nested_semantic_validation() -> None:
    with pytest.raises(OdpValidationError):
        parse_offering_page(
            '{"items":[{"id":"plant","language":"en","localizations":["ja"],'
            '"name":"Plant"}],"odp_version":"1.0"}'
        )
    with pytest.raises(OdpValidationError):
        parse_collection_page(
            '{"items":[{"id":"plants","language":"en","localizations":["ja"],'
            '"name":"Plants"}],"odp_version":"1.0"}'
        )

    offering = parse_offering_page(
        '{"items":[{"id":"plant","name":"Plant"}],"odp_version":"1.0"}'
    ).items[0]
    collection = parse_collection_page(
        '{"items":[{"id":"plants","name":"Plants"}],"odp_version":"1.0"}'
    ).items[0]
    assert offering.to_dict() == {"id": "plant", "name": "Plant"}
    assert collection.to_dict() == {"id": "plants", "name": "Plants"}
    with pytest.raises(OdpValidationError):
        parse_filter_definition_page(
            '{"items":[{"description":"Name","id":"name","operators":["gt"],'
            '"title":"Name","type":"string"}],"odp_version":"1.0"}'
        )
