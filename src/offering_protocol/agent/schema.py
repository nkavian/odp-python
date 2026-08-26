"""Bounded Attribute Schema resolution."""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Protocol
from urllib.parse import urldefrag, urljoin, urlsplit

from jsonschema.validators import validator_for
from referencing import Registry, Resource

from offering_protocol.agent.client import AgentError, ServiceClient

_DIALECT = "https://json-schema.org/draft/2020-12/schema"
_MAXIMUM_DOCUMENT_BYTES = 262_144
_MAXIMUM_DOCUMENTS = 16
_MAXIMUM_DEPTH = 8
_MAXIMUM_GRAPH_BYTES = 1_048_576
_STANDARD_VOCABULARY = "https://json-schema.org/draft/2020-12/vocab/"


class SchemaValidator(Protocol):
    def is_valid(self, instance: Any) -> bool: ...


@dataclass(frozen=True, slots=True)
class ResolvedSchema:
    schema: dict[str, object]
    validator: SchemaValidator


async def resolve_schema(client: ServiceClient, target: str) -> ResolvedSchema:
    root_url = _document_url(target)
    documents: dict[str, dict[str, object]] = {}
    graph_bytes = 0

    async def load(document_url: str, depth: int) -> None:
        nonlocal graph_bytes
        if document_url in documents:
            return
        if len(documents) >= _MAXIMUM_DOCUMENTS:
            raise AgentError("ODP Attribute Schema graph exceeds 16 documents")
        if depth > _MAXIMUM_DEPTH:
            raise AgentError("ODP Attribute Schema graph exceeds eight reference levels")
        document = await client._supporting_json(
            document_url,
            "attribute-schema",
            "application/schema+json",
            {"application/schema+json"},
            _MAXIMUM_DOCUMENT_BYTES,
        )
        _require_schema(document)
        encoded = json.dumps(document, separators=(",", ":")).encode()
        graph_bytes += len(encoded)
        if graph_bytes > _MAXIMUM_GRAPH_BYTES:
            raise AgentError("ODP Attribute Schema graph exceeds its byte limit")
        documents[document_url] = document
        for reference_url in _schema_references(document, document_url):
            await load(reference_url, depth + 1)

    await load(root_url, 0)
    root = documents[root_url]
    registry: Registry[Any] = Registry().with_resources(
        (url, Resource.from_contents(document)) for url, document in documents.items()
    )
    validation_root = {"$id": root_url, **root}
    validator_type = validator_for(validation_root)
    validator_type.check_schema(validation_root)
    return ResolvedSchema(root, validator_type(validation_root, registry=registry))


def _document_url(value: str) -> str:
    target, _ = urldefrag(value)
    parsed = urlsplit(target)
    if (
        parsed.scheme != "https"
        or parsed.hostname is None
        or parsed.username is not None
        or parsed.password is not None
    ):
        raise AgentError("ODP Attribute Schema references must use HTTPS")
    return target


def _require_schema(document: dict[str, object]) -> None:
    if document.get("$schema") != _DIALECT:
        raise AgentError("ODP Attribute Schema must declare JSON Schema Draft 2020-12")
    pending: list[object] = [document]
    while pending:
        value = pending.pop()
        if isinstance(value, list):
            pending.extend(value)
        elif isinstance(value, dict):
            if "$dynamicRef" in value:
                reference = value["$dynamicRef"]
                if not isinstance(reference, str) or not reference.startswith("#"):
                    raise AgentError(
                        "ODP Attribute Schema $dynamicRef must be a fragment-only reference"
                    )
            vocabulary = value.get("$vocabulary")
            if isinstance(vocabulary, dict):
                for uri, required in vocabulary.items():
                    if required is True and not str(uri).startswith(_STANDARD_VOCABULARY):
                        raise AgentError(
                            f"ODP Attribute Schema requires unsupported vocabulary {uri}"
                        )
            pending.extend(value.values())


def _schema_references(document: object, retrieval_url: str) -> tuple[str, ...]:
    references: list[str] = []
    local_resources = {_document_url(retrieval_url)}
    pending = [(document, retrieval_url)]
    while pending:
        value, inherited_base = pending.pop()
        if isinstance(value, list):
            pending.extend((child, inherited_base) for child in value)
        elif isinstance(value, dict):
            base = inherited_base
            identifier = value.get("$id")
            if isinstance(identifier, str):
                base = urljoin(inherited_base, identifier)
                local_resources.add(_document_url(base))
            reference = value.get("$ref")
            if isinstance(reference, str):
                references.append(_document_url(urljoin(base, reference)))
            pending.extend((child, base) for keyword, child in value.items() if keyword != "$ref")
    return tuple(reference for reference in references if reference not in local_resources)
