"""Bounded Attribute Schema resolution."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol, cast
from urllib.parse import urldefrag, urljoin, urlsplit

from jsonschema.validators import Draft202012Validator, validator_for
from referencing import Registry, Resource
from referencing.jsonschema import DRAFT202012

from offering_protocol.agent.client import AgentError, ServiceClient, _decode_json_object

_DIALECT = "https://json-schema.org/draft/2020-12/schema"
_MAXIMUM_DOCUMENT_BYTES = 262_144
_MAXIMUM_DOCUMENTS = 16
_MAXIMUM_DEPTH = 8
_MAXIMUM_GRAPH_BYTES = 1_048_576
_SUPPORTED_VOCABULARIES = frozenset(Draft202012Validator.META_SCHEMA["$vocabulary"])


class SchemaValidator(Protocol):
    def is_valid(self, instance: Any) -> bool: ...


@dataclass(frozen=True, slots=True)
class ResolvedSchema:
    schema: dict[str, object]
    validator: SchemaValidator


async def resolve_schema(client: ServiceClient, target: str) -> ResolvedSchema:
    root_url = _document_url(target)
    documents: dict[str, dict[str, object]] = {}
    retrievals: dict[str, str] = {}
    graph_bytes = 0

    async def load(document_url: str, depth: int) -> None:
        nonlocal graph_bytes
        if document_url in documents:
            return
        if len(documents) >= _MAXIMUM_DOCUMENTS:
            raise AgentError("ODP Attribute Schema graph exceeds 16 documents")
        if depth > _MAXIMUM_DEPTH:
            raise AgentError("ODP Attribute Schema graph exceeds eight reference levels")
        response = await client._supporting_document(
            document_url,
            "attribute-schema",
            "application/schema+json",
            {"application/schema+json"},
            _MAXIMUM_DOCUMENT_BYTES,
        )
        document = _decode_json_object(response.body)
        _require_schema(document)
        graph_bytes += len(response.body)
        if graph_bytes > _MAXIMUM_GRAPH_BYTES:
            raise AgentError("ODP Attribute Schema graph exceeds its byte limit")
        documents[document_url] = document
        retrievals[document_url] = response.final_url
        for reference_url in _schema_references(document, response.final_url):
            await load(reference_url, depth + 1)

    await load(root_url, 0)
    root = documents[root_url]
    registry: Registry[Any] = Registry().with_resources(
        (
            url,
            Resource.from_contents(
                {
                    **document,
                    "$id": urljoin(retrievals[requested], cast("str", document.get("$id", ""))),
                }
            ),
        )
        for requested, document in documents.items()
        for url in {requested, retrievals[requested]}
    )
    validation_root = {
        **root,
        "$id": urljoin(retrievals[root_url], cast("str", root.get("$id", ""))),
    }
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
    validator_for(document).check_schema(document)
    pending = [DRAFT202012.create_resource(document)]
    while pending:
        resource = pending.pop()
        value = resource.contents
        if isinstance(value, dict):
            if "$dynamicRef" in value:
                reference = value["$dynamicRef"]
                if not isinstance(reference, str) or not reference.startswith("#"):
                    raise AgentError(
                        "ODP Attribute Schema $dynamicRef must be a fragment-only reference"
                    )
            vocabulary = value.get("$vocabulary")
            if isinstance(vocabulary, dict):
                for uri, required in vocabulary.items():
                    if required is True and uri not in _SUPPORTED_VOCABULARIES:
                        raise AgentError(
                            f"ODP Attribute Schema requires unsupported vocabulary {uri}"
                        )
        pending.extend(resource.subresources())


def _schema_references(document: Any, retrieval_url: str) -> tuple[str, ...]:
    references: list[str] = []
    local_resources = {_document_url(retrieval_url)}
    pending = [(DRAFT202012.create_resource(document), retrieval_url)]
    while pending:
        resource, inherited_base = pending.pop()
        value = resource.contents
        base = inherited_base
        if isinstance(value, dict):
            identifier = value.get("$id")
            if isinstance(identifier, str):
                base = urljoin(inherited_base, identifier)
                local_resources.add(_document_url(base))
            reference = value.get("$ref")
            if isinstance(reference, str):
                references.append(_document_url(urljoin(base, reference)))
        pending.extend((child, base) for child in resource.subresources())
    return tuple(reference for reference in references if reference not in local_resources)
