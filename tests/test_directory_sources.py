from __future__ import annotations

import copy
import json
import runpy
from pathlib import Path

import pytest
from pydantic import JsonValue, ValidationError

from offering_protocol.directory import (
    CollectionResult,
    DirectoryClient,
    DirectoryError,
    DirectoryIndexedService,
    DirectorySource,
    ResourceSearchRequest,
    SearchRequest,
    ServiceFilters,
    ServiceResult,
    SuggestionRequest,
)
from test_directory_mixed import item, service, transport_for


def imported(kind: str = "service") -> dict[str, JsonValue]:
    value = item(kind)
    parent = service()
    for field in (
        "description",
        "language",
        "localizations",
        "keywords",
        "operations",
        "protocols",
    ):
        parent.pop(field, None)
    parent["source"] = {
        "type": "openapi",
        "url": "https://docs.example/specs/api.json?version=3&key=a%2Fb",
        "x402_discovery": True,
    }
    value["service"] = parent
    return value


def change(value: dict[str, JsonValue], path: str, replacement: JsonValue) -> None:
    parts = path.split("/")
    for part in parts[:-1]:
        child = value[part]
        assert isinstance(child, dict)
        value = child
    value[parts[-1]] = replacement


@pytest.mark.asyncio
async def test_source_identity_optional_metadata_and_future_formats() -> None:
    first = imported()
    change(first, "service/source/extension", {"retained": True})
    for field in (
        "operations",
        "http",
        "branding",
        "mcp",
        "odp_version",
        "payment_origins",
        "search_capabilities",
    ):
        change(first, f"service/{field}", "not authoritative")
    future = imported("collection")
    change(future, "service/source/type", "future-format")
    change(future, "service/source/url", "HTTPS://Docs.Example:443/other.json?x=1")
    change(future, "service/service_id", "other-document")
    change(future, "service/description", "")
    change(future, "service/language", "en")
    change(future, "service/localizations", ["en"])
    change(future, "service/keywords", ["weather"])
    change(future, "service/website_url", "https://example.com/")
    payload = {"items": [first, imported("collection"), future]}
    original = copy.deepcopy(payload)
    transport = transport_for(payload)
    page = await DirectoryClient(transport=transport).continue_search(
        "/v1/directory/search?cursor=x"
    )
    assert payload == original
    assert not page.issues
    one, two, three = page.items
    assert isinstance(one, ServiceResult)
    assert isinstance(one.service, DirectoryIndexedService)
    assert isinstance(one.service.source, DirectorySource)
    assert one.service.source.type == "openapi"
    assert one.service.source.url == "https://docs.example/specs/api.json?version=3&key=a%2Fb"
    assert one.service.source.x402_discovery is True
    assert one.service.source.additional == {"extension": {"retained": True}}
    assert one.service.source.to_dict()["type"] == "openapi"
    assert one.service.description is None and one.service.language is None
    assert one.service.operations == []
    assert one.service.keywords == one.service.localizations == []
    assert one.service.protocols is None and one.service.additional == {}
    assert isinstance(two, CollectionResult) and isinstance(three, CollectionResult)
    assert two.collection.id == three.collection.id
    assert two.service.service_origin == three.service.service_origin
    assert two.service.service_id != three.service.service_id
    assert three.service.source.type == "future-format"
    assert three.service.source.url == "HTTPS://Docs.Example:443/other.json?x=1"
    assert three.service.description == "" and three.service.language == "en"
    assert three.service.keywords == ["weather"] and three.service.localizations == ["en"]
    assert three.service.website_url == "https://example.com/"
    assert len(transport.requests) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "path,value",
    [
        ("source", None),
        ("source", []),
        ("source/type", ""),
        ("source/type", " "),
        ("source/type", "x" * 129),
        ("source/type", 1),
        ("source/x402_discovery", "false"),
        ("source/x402_discovery", 1),
        ("source/url", None),
        ("source/url", "http://example.com/spec"),
        ("source/url", "https:example.com/spec"),
        ("source/url", "/openapi.json"),
        ("source/url", "https://user:pass@example.com/spec"),
        ("source/url", "https://example.com/spec#part"),
        ("source/url", "https://example.com/" + "x" * 2048),
        ("source/url", "https://example.com/a\nb"),
        ("source/url", "https://["),
        ("source/url", "https://example.com:70000/spec"),
        ("source/url", "https://localhost/spec"),
        ("source/url", "https://dev.localhost./spec"),
        ("source/url", "https://127.0.0.1/spec"),
        ("source/url", "https://[::1]/spec"),
        ("source/url", "https://10.0.0.1/spec"),
        ("name", ""),
        ("name", "x" * 129),
        ("name", None),
        ("keywords", [1]),
        ("keywords", None),
        ("localizations", None),
        ("description", None),
        ("language", 3),
        ("website_url", None),
        ("documentation_url", False),
        ("support_url", []),
        ("status_url", {}),
        ("protocols", None),
        ("protocols", []),
        ("protocols", {"trust": []}),
        ("protocols", {"trust": None}),
        ("protocols", {"trust": [None]}),
        ("protocols", {"trust": [{}]}),
        ("protocols", {"trust": [{"name": ""}]}),
        ("protocols", {"trust": [{"name": "tap"}, {"name": "tap"}]}),
        ("protocols", {"payments": [{"name": "x402"}]}),
        (
            "protocols",
            {"payments": [{"name": "x402", "authentication": "required", "options": ["unknown"]}]},
        ),
        ("protocols", {"enrollment": [{"name": "aep", "extra": True}]}),
    ],
)
async def test_invalid_imported_records_are_isolated(path: str, value: JsonValue) -> None:
    invalid = imported()
    change(invalid, f"service/{path}", value)
    page = await DirectoryClient(
        transport=transport_for({"items": [invalid, imported(), item()]})
    ).search(ResourceSearchRequest())
    assert len(page.items) == 2
    assert len(page.issues) == 1 and page.issues[0].index == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("field", ["type", "url", "x402_discovery"])
async def test_source_members_are_required(field: str) -> None:
    raw = imported()
    parent = raw["service"]
    assert isinstance(parent, dict)
    source = parent["source"]
    assert isinstance(source, dict)
    del source[field]
    page = await DirectoryClient(transport=transport_for({"items": [raw]})).search(
        ResourceSearchRequest()
    )
    assert not page.items and len(page.issues) == 1


@pytest.mark.asyncio
async def test_native_validation_remains_strict_and_public_ip_sources_are_readable() -> None:
    invalid = item()
    parent = invalid["service"]
    assert isinstance(parent, dict)
    del parent["language"]
    missing_source = item()
    parent = missing_source["service"]
    assert isinstance(parent, dict)
    del parent["source"]
    valid = imported()
    change(valid, "service/source/url", "https://8.8.8.8/spec")
    page = await DirectoryClient(
        transport=transport_for({"items": [invalid, missing_source, valid]})
    ).search(ResourceSearchRequest())
    assert len(page.items) == 1 and len(page.issues) == 2
    raw = imported()["service"]
    native = await DirectoryClient(
        transport=transport_for({"items": [raw, service()]})
    ).search_services(SearchRequest())
    assert len(native.items) == 1 and len(native.issues) == 1


@pytest.mark.asyncio
async def test_protocol_evidence_without_synthesizing_enrollment() -> None:
    valid = imported()
    change(
        valid,
        "service/protocols",
        {
            "payments": [
                {"name": "x402", "authentication": "required", "options": ["base"]},
                {"name": "future"},
            ],
            "trust": [{"name": "tap"}],
        },
    )
    enrollment = imported()
    change(enrollment, "service/protocols", {"enrollment": [{"name": "aep"}]})
    unknown = imported()
    change(unknown, "service/protocols", {"payments": [{"name": "future"}]})
    empty = imported()
    change(empty, "service/protocols", {})
    page = await DirectoryClient(
        transport=transport_for({"items": [valid, enrollment, unknown, empty]})
    ).search(ResourceSearchRequest())
    assert not page.issues and len(page.items) == 4
    first = page.items[0]
    assert isinstance(first, ServiceResult) and first.service.protocols is not None
    assert not first.service.protocols.enrollment
    assert len(first.service.protocols.payments) == len(first.service.protocols.trust) == 1
    assert first.service.protocols.payments[0].options == ["base"]


@pytest.mark.asyncio
async def test_source_filters_on_search_native_search_and_suggestions() -> None:
    filters = ServiceFilters(sources=["odp", "openapi"], keywords=["weather"])
    original = copy.deepcopy(filters.to_dict())
    for route in ("mixed", "native", "suggest"):
        transport = transport_for({"items": []})
        client = DirectoryClient(transport=transport)
        if route == "mixed":
            await client.search(ResourceSearchRequest(filters=filters))
            path = "/v1/directory/search"
        elif route == "native":
            await client.search_services(SearchRequest(filters=filters))
            path = "/v1/services/search"
        else:
            await client.suggest(SuggestionRequest(prefix="we", filters=filters))
            path = "/v1/directory/suggestions"
        request = transport.requests[0]
        assert request.method == "POST" and request.url.endswith(path)
        assert json.loads(request.body)["filters"] == original
        assert filters.to_dict() == original
        for sources in ([], ["odp", "odp"], ["odp", "openapi", "odp"]):
            invalid = ServiceFilters.model_validate({"sources": sources})
            with pytest.raises(DirectoryError, match="sources"):
                await client.search(ResourceSearchRequest(filters=invalid))
            with pytest.raises(DirectoryError, match="sources"):
                await client.search_services(SearchRequest(filters=invalid))
            with pytest.raises(DirectoryError, match="sources"):
                await client.suggest(SuggestionRequest(prefix="we", filters=invalid))
        assert len(transport.requests) == 1
    assert ServiceFilters().to_dict() == {}
    for unsupported in (["future"], ["ODP"], [None]):
        with pytest.raises(ValidationError):
            ServiceFilters.model_validate({"sources": unsupported})


@pytest.mark.asyncio
async def test_discovery_example_does_not_call_odp_for_imported_collections(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    example = runpy.run_path(str(Path(__file__).parents[1] / "examples" / "directory.py"))
    discover = example["discover"]
    transport = transport_for({"items": [imported(), imported("collection")]})

    def directory(*args: object) -> DirectoryClient:
        return DirectoryClient(transport=transport)

    def unexpected_service(*args: object) -> None:
        raise AssertionError("Imported discovery must not make ODP requests")

    monkeypatch.setitem(discover.__globals__, "DirectoryClient", directory)
    monkeypatch.setitem(discover.__globals__, "ServiceClient", unexpected_service)
    await discover(example["Environment"].PRODUCTION, "weather")
    output = capsys.readouterr().out
    assert output.count("Discovery document: https://docs.example/specs/api.json") == 2
    assert len(transport.requests) == 1
