#!/usr/bin/env python3
from __future__ import annotations

import asyncio
import json
import sys
from collections.abc import Callable
from typing import Any

from offering_protocol.agent import ServiceClient
from offering_protocol.core import (
    CollectionSearchRequest,
    Offering,
    OfferingPage,
    OfferingSearchRequest,
    Operation,
    Page,
    derive_service_origin,
    is_local_resource_identifier,
    parse_collection,
    parse_collection_search_request,
    parse_filter_definition,
    parse_offering,
    parse_offering_page,
    parse_offering_search_request,
    parse_problem_response,
    parse_resource_identity,
    parse_service_document,
    parse_sort_definition,
    resolve_continuation,
    resolve_resource_reference,
    validate_value,
)
from offering_protocol.directory.transport import HttpRequest, HttpResponse
from offering_protocol.service import CatalogRequest, Request, ServiceBuilder


class MapTransport:
    def __init__(self, responses: dict[str, HttpResponse]) -> None:
        self.responses = responses
        self.requests: list[HttpRequest] = []

    async def send(self, request: HttpRequest) -> HttpResponse:
        self.requests.append(request)
        return self.responses.get(
            request.url,
            response({"title": "Not Found"}, 404, "application/problem+json"),
        )

    async def aclose(self) -> None:
        return None


class EmptyCatalog:
    def operations(self) -> list[Operation]:
        return [Operation.GET_OFFERING, Operation.LIST_OFFERINGS, Operation.SEARCH_OFFERINGS]

    async def list_offerings(self, request: CatalogRequest) -> OfferingPage[Offering]:
        return OfferingPage(odp_version="1.0", items=[])

    async def get_offering(self, identifier: str, request: CatalogRequest) -> Offering | None:
        return None

    async def search_offerings(
        self, query: OfferingSearchRequest, request: CatalogRequest
    ) -> OfferingPage[Offering]:
        return OfferingPage(odp_version="1.0", items=[])

    async def list_collections(self, request: CatalogRequest) -> Page[Any]:
        raise NotImplementedError

    async def get_collection(self, identifier: str, request: CatalogRequest) -> None:
        raise NotImplementedError

    async def search_collections(
        self, query: CollectionSearchRequest, request: CatalogRequest
    ) -> Page[Any]:
        raise NotImplementedError

    async def list_collection_offerings(
        self, collection_id: str, request: CatalogRequest
    ) -> OfferingPage[Offering]:
        raise NotImplementedError


def succeeds(operation: Callable[[], object]) -> bool:
    try:
        operation()
        return True
    except (TypeError, ValueError):
        return False


def response(
    value: object, status: int = 200, content_type: str = "application/odp+json"
) -> HttpResponse:
    return HttpResponse(
        status=status,
        headers={"content-type": content_type},
        body=json.dumps(value, separators=(",", ":")).encode(),
    )


def service_document() -> dict[str, object]:
    return {
        "odp_version": "1.0",
        "name": "Conformance Service",
        "description": "ODP Python conformance adapter",
        "language": "en",
        "localizations": ["en"],
        "operations": [
            {"authentication": "not-required", "name": "get-offering"},
            {"authentication": "not-required", "name": "list-offerings"},
        ],
        "http": {"endpoint_base": "/odp"},
    }


async def attribute_schema_details(
    documents: dict[str, HttpResponse],
    *,
    attributes: dict[str, object] | None = None,
    root_url: str = "https://schemas.example/root.json",
) -> tuple[object, MapTransport]:
    offering = {
        "odp_version": "1.0",
        "id": "item",
        "name": "Item",
        "schema": {"url": root_url},
        "attributes": attributes
        if attributes is not None
        else {"name": "root", "children": [{"name": "child"}]},
    }
    transport = MapTransport(
        {
            "https://service.example/.well-known/odp": response(service_document()),
            "https://service.example/odp/offerings/item?representation=full": response(offering),
        }
    )
    supporting = MapTransport(documents)
    client = ServiceClient(
        "https://service.example", transport=transport, supporting_transport=supporting
    )
    return await client.get_offering_details("item"), supporting


async def evaluate_attribute_schema(case: dict[str, Any]) -> bool | None:
    operation = case.get("operation")
    if operation == "validate-reference":
        offering = {
            "odp_version": "1.0",
            "id": "item",
            "name": "Item",
            "schema": case["reference"],
        }
        return succeeds(lambda: parse_offering(json.dumps(offering))) == case["valid"]
    if operation == "validate-response":
        details, _ = await attribute_schema_details(
            {
                "https://schemas.example/root.json": response(
                    case["document"], case["status"], case["content_type"]
                )
            }
        )
        return (details.attribute_schema is not None) == case["valid"]
    if operation == "validate-schema-reference-profile":
        documents = {
            document.get("$id", f"https://schemas.example/document-{index}.json"): response(
                document, content_type="application/schema+json"
            )
            for index, document in enumerate(case["documents"])
        }
        root_url = next(iter(documents))
        details, _ = await attribute_schema_details(documents, root_url=root_url)
        return (details.attribute_schema is not None) == case["valid"]
    if operation == "validation-scope":
        schema = {
            "$schema": "https://json-schema.org/draft/2020-12/schema",
            "type": "object",
            "properties": {"memory": {"type": "number"}},
        }
        if case["representation"] == "full":
            details, _ = await attribute_schema_details(
                {
                    "https://schemas.example/root.json": response(
                        schema, content_type="application/schema+json"
                    )
                },
                attributes={"memory": "invalid"},
            )
            complete = details.attribute_schema is not None and any(
                issue.scope.value == "attributes" for issue in details.issues
            )
        else:
            complete = False
        return complete == case["complete_instance_validation"]
    if operation == "failure-scope":
        details, _ = await attribute_schema_details(
            {
                "https://schemas.example/root.json": response(
                    {"title": "Unavailable"}, 503, "application/problem+json"
                )
            }
        )
        actual = {
            "offering_usable": details.offering.id == "item",
            "attributes_usable": bool(details.offering.attributes),
            "report_issue": any(
                issue.scope.value == "attribute_schema" for issue in details.issues
            ),
        }
        return all(actual[name] == expected for name, expected in case["expected"].items())
    return None


async def evaluate_errors_limits(case: dict[str, Any]) -> bool | None:
    if case.get("operation") == "validate-problem":
        valid = succeeds(
            lambda: parse_problem_response(json.dumps(case["problem"]), case["http_status"])
        )
        return valid == case["valid"]
    if case.get("operation") != "validate-limit" or case.get("resource") != "request":
        return None
    service = ServiceBuilder("Conformance", "Conformance", "en", "/odp").build(EmptyCatalog())
    prefix = json.dumps({"odp_version": "1.0", "query": "gpu"}, separators=(",", ":"))
    body = prefix.ljust(case["bytes"]).encode()
    result = await service.handle(
        Request(
            "POST",
            "/odp/offerings/search",
            body,
            {"content-type": "application/odp+json"},
        )
    )
    return (result.status == 200) == case["valid"]


async def evaluate_case(subject: str, case: dict[str, Any], role: str) -> bool | None:
    if subject == "local-identifier":
        return is_local_resource_identifier(case["value"]) == case["valid"]
    if subject == "identity-comparison":
        left = parse_resource_identity(json.dumps(case["left"]))
        right = parse_resource_identity(json.dumps(case["right"]))
        return (left.key == right.key) == case["same_identity"]
    if subject == "service-origin":
        valid = succeeds(lambda: derive_service_origin(case["value"]) == case["value"])
        if valid:
            valid = derive_service_origin(case["value"]) == case["value"]
        return valid == case["valid"]
    if subject == "resource-reference":
        valid = succeeds(
            lambda: resolve_resource_reference(case["value"], "https://service.example")
        )
        return valid == case["valid"]
    if subject == "service-document":
        return (
            succeeds(lambda: parse_service_document(json.dumps(case["document"]))) == case["valid"]
        )
    if subject == "collection-envelope":
        return succeeds(lambda: parse_collection(json.dumps(case["document"]))) == case["valid"]
    if subject == "offering-contract" and case.get("representation") == "full":
        return succeeds(lambda: parse_offering(json.dumps(case["document"]))) == case["valid"]
    if subject == "collection-search-contract" and case.get("operation") == "validate-request":
        return (
            succeeds(lambda: parse_collection_search_request(json.dumps(case["request"])))
            == case["valid"]
        )
    if subject == "offering-search-contract" and case.get("operation") == "validate-request":
        return (
            succeeds(lambda: parse_offering_search_request(json.dumps(case["request"])))
            == case["valid"]
        )
    if subject == "attribute-schema-retrieval":
        return await evaluate_attribute_schema(case)
    if subject == "filter-sort-contract":
        if case.get("operation") == "validate-definition":
            return (
                succeeds(lambda: parse_filter_definition(json.dumps(case["definition"])))
                == case["valid"]
            )
        if case.get("operation") == "validate-sort" and "definitions" not in case:
            return (
                succeeds(lambda: parse_sort_definition(json.dumps(case["sort"]))) == case["valid"]
            )
        return None
    if subject == "pagination-contract":
        if case.get("operation") == "validate-page":
            valid = succeeds(
                lambda: validate_value(case["page"], "page-envelope.schema.json", "Page")
            )
            return valid == case["valid"]
        if case.get("operation") == "validate-limit":
            limit = case["limit"]
            valid = isinstance(limit, int) and not isinstance(limit, bool) and 1 <= limit <= 100
            return valid == case["valid"]
        if case.get("operation") == "validate-next":
            valid = succeeds(lambda: resolve_continuation(case["next"], case["service_origin"]))
            return valid == case["valid"]
        return None
    if subject == "errors-limits-contract":
        return await evaluate_errors_limits(case)
    if subject == "role-baseline" and role == "service" and case.get("role") == "service":
        operations = set(case["operations"])
        valid = {"list-offerings", "get-offering"} <= operations
        valid = valid and succeeds(lambda: parse_offering_page(json.dumps(case["list_response"])))
        valid = valid and succeeds(lambda: parse_offering(json.dumps(case["get_response"])))
        return valid == case["valid"]
    return None


async def evaluate(request: dict[str, Any]) -> dict[str, object]:
    try:
        actual = await evaluate_case(request["vector"]["subject"], request["case"], request["role"])
        if actual is None:
            operation = request["case"].get(
                "operation",
                request["case"].get("representation", request["case"].get("name", "case")),
            )
            return {
                "status": "skipped",
                "message": (
                    f"No public Python operation maps {request['vector']['subject']}/{operation}"
                ),
            }
        return (
            {"status": "passed"}
            if actual
            else {"status": "failed", "message": "Public API result did not match the vector"}
        )
    except Exception as error:
        return {"status": "failed", "message": str(error)[:1024]}


async def main() -> None:
    for line in sys.stdin:
        if not line.strip():
            continue
        request = json.loads(line)
        result = await evaluate(request)
        output = {"protocol_version": "1", "sequence": request["sequence"], **result}
        sys.stdout.write(json.dumps(output, separators=(",", ":")) + "\n")
        sys.stdout.flush()


if __name__ == "__main__":
    asyncio.run(main())
