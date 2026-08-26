from __future__ import annotations

import json

import pytest

from helpers import QueueTransport, response
from offering_protocol.agent import AgentError, ServiceClient
from offering_protocol.agent.schema import _document_url, _schema_references, resolve_schema

DIALECT = "https://json-schema.org/draft/2020-12/schema"


@pytest.mark.asyncio
async def test_resolves_bounded_external_schema_graph() -> None:
    root = {
        "$schema": DIALECT,
        "properties": {
            "plant": {"$ref": "external.json#/$defs/plant"},
            "plant2": {"$ref": "external.json#/$defs/plant"},
            "self": {"$ref": "#/$defs/self"},
        },
        "$defs": {"self": {"type": "string"}},
        "type": "object",
    }
    external = {
        "$defs": {"plant": {"type": "string"}},
        "$schema": DIALECT,
    }
    transport = QueueTransport(
        response(json.dumps(root), content_type="application/schema+json"),
        response(json.dumps(external), content_type="application/schema+json"),
    )
    resolved = await resolve_schema(
        ServiceClient("https://service.example", supporting_transport=transport),
        "https://schemas.example/root.json",
    )
    assert resolved.schema == root
    assert resolved.validator.is_valid({"plant": "rubber"})
    assert not resolved.validator.is_valid({"plant": 4})
    assert [request.url for request in transport.requests] == [
        "https://schemas.example/root.json",
        "https://schemas.example/external.json",
    ]


@pytest.mark.asyncio
async def test_composes_external_schema_with_fragment_dynamic_reference() -> None:
    root = {
        "$id": "https://schemas.example/offering.json",
        "$ref": "https://schemas.example/common.json",
        "$schema": DIALECT,
    }
    common = {
        "$dynamicAnchor": "node",
        "$id": "https://schemas.example/common.json",
        "$schema": DIALECT,
        "properties": {
            "children": {"items": {"$dynamicRef": "#node"}, "type": "array"},
            "name": {"type": "string"},
        },
        "required": ["name"],
        "type": "object",
    }
    resolved = await resolve_schema(
        ServiceClient(
            "https://service.example",
            supporting_transport=QueueTransport(
                response(json.dumps(root), content_type="application/schema+json"),
                response(json.dumps(common), content_type="application/schema+json"),
            ),
        ),
        "https://schemas.example/offering.json",
    )
    assert resolved.validator.is_valid({"children": [{"name": "child"}], "name": "root"})
    assert not resolved.validator.is_valid({"children": [{"name": 1}], "name": "root"})


@pytest.mark.asyncio
async def test_schema_resolution_rejects_invalid_dialect_and_vocabulary() -> None:
    invalid_dialect = ServiceClient(
        "https://service.example",
        supporting_transport=QueueTransport(
            response('{"type":"object"}', content_type="application/schema+json")
        ),
    )
    with pytest.raises(AgentError, match="Draft 2020-12"):
        await resolve_schema(invalid_dialect, "https://schemas.example/root.json")

    vocabulary = json.dumps(
        {
            "$schema": DIALECT,
            "$vocabulary": {"https://example.com/vocabulary": True},
        }
    )
    unsupported = ServiceClient(
        "https://service.example",
        supporting_transport=QueueTransport(
            response(vocabulary, content_type="application/schema+json")
        ),
    )
    with pytest.raises(AgentError, match="unsupported vocabulary"):
        await resolve_schema(unsupported, "https://schemas.example/root.json")

    supported_vocabulary = json.dumps(
        {
            "$schema": DIALECT,
            "$vocabulary": {
                "https://json-schema.org/draft/2020-12/vocab/core": True,
                "https://example.com/optional": False,
            },
            "allOf": [{"type": "object"}],
        }
    )
    supported = ServiceClient(
        "https://service.example",
        supporting_transport=QueueTransport(
            response(supported_vocabulary, content_type="application/schema+json")
        ),
    )
    assert (
        await resolve_schema(supported, "https://schemas.example/root.json")
    ).validator.is_valid({})

    for reference in ("https://schemas.example/common.json#node", "common.json#node", None):
        unsupported_dynamic_reference = ServiceClient(
            "https://service.example",
            supporting_transport=QueueTransport(
                response(
                    json.dumps({"$dynamicRef": reference, "$schema": DIALECT}),
                    content_type="application/schema+json",
                )
            ),
        )
        with pytest.raises(AgentError, match="fragment-only reference"):
            await resolve_schema(
                unsupported_dynamic_reference,
                "https://schemas.example/root.json",
            )


@pytest.mark.asyncio
async def test_schema_resolution_enforces_graph_limits(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = json.dumps({"$schema": DIALECT, "$ref": "external.json"})
    external = json.dumps({"$schema": DIALECT, "type": "object"})

    monkeypatch.setattr("offering_protocol.agent.schema._MAXIMUM_DOCUMENTS", 1)
    client = ServiceClient(
        "https://service.example",
        supporting_transport=QueueTransport(response(root, content_type="application/schema+json")),
    )
    with pytest.raises(AgentError, match="16 documents"):
        await resolve_schema(client, "https://schemas.example/root.json")

    monkeypatch.setattr("offering_protocol.agent.schema._MAXIMUM_DOCUMENTS", 16)
    monkeypatch.setattr("offering_protocol.agent.schema._MAXIMUM_DEPTH", 0)
    client = ServiceClient(
        "https://service.example",
        supporting_transport=QueueTransport(response(root, content_type="application/schema+json")),
    )
    with pytest.raises(AgentError, match="eight reference levels"):
        await resolve_schema(client, "https://schemas.example/root.json")

    monkeypatch.setattr("offering_protocol.agent.schema._MAXIMUM_DEPTH", 8)
    monkeypatch.setattr("offering_protocol.agent.schema._MAXIMUM_GRAPH_BYTES", 1)
    client = ServiceClient(
        "https://service.example",
        supporting_transport=QueueTransport(
            response(root, content_type="application/schema+json"),
            response(external, content_type="application/schema+json"),
        ),
    )
    with pytest.raises(AgentError, match="byte limit"):
        await resolve_schema(client, "https://schemas.example/root.json")


def test_schema_reference_validation_and_collection() -> None:
    with pytest.raises(AgentError, match="HTTPS"):
        _document_url("http://schemas.example/root.json")
    references = _schema_references(
        {
            "allOf": [{"$ref": "one.json"}, {"$dynamicRef": "#value"}],
            "ignored": 4,
        },
        "https://schemas.example/root.json",
    )
    assert references == ("https://schemas.example/one.json",)


def test_schema_references_apply_nested_ids_without_refetching_embedded_resources() -> None:
    references = _schema_references(
        {
            "$id": "https://schemas.example/catalog/root.json",
            "$defs": {
                "embedded": {
                    "$id": "embedded.json",
                    "$ref": "#/value",
                },
                "external": {"$ref": "../shared.json#/value"},
            },
        },
        "https://delivery.example/root.json",
    )
    assert references == ("https://schemas.example/shared.json",)


@pytest.mark.asyncio
async def test_resolves_embedded_schema_resources() -> None:
    root = {
        "$defs": {"plant": {"$id": "plant.json", "type": "string"}},
        "$schema": DIALECT,
        "properties": {"plant": {"$ref": "plant.json"}},
        "type": "object",
    }
    transport = QueueTransport(response(json.dumps(root), content_type="application/schema+json"))

    resolved = await resolve_schema(
        ServiceClient("https://service.example", supporting_transport=transport),
        "https://schemas.example/root.json",
    )

    assert resolved.validator.is_valid({"plant": "rubber"})
    assert not resolved.validator.is_valid({"plant": 4})
    assert len(transport.requests) == 1
