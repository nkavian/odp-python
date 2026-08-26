"""Agent-friendly Offering details and Action resolution."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from urllib.parse import urljoin, urlsplit

from offering_protocol.agent.client import AgentError, ServiceClient
from offering_protocol.agent.schema import resolve_schema
from offering_protocol.core import (
    Action,
    ActionRelation,
    ActionRequest,
    AuthenticationRequirement,
    Offering,
)

_MAXIMUM_OPENAPI_BYTES = 1_048_576


class OfferingIssueScope(StrEnum):
    ACTION = "action"
    ATTRIBUTE_SCHEMA = "attribute_schema"
    ATTRIBUTES = "attributes"


@dataclass(frozen=True, slots=True)
class OfferingIssue:
    scope: OfferingIssueScope
    message: str
    action_id: str | None = None


@dataclass(frozen=True, slots=True)
class DiscoveredHttpAction:
    method: str
    request: ActionRequest | None
    response_content_types: tuple[str, ...]
    url: str


@dataclass(frozen=True, slots=True)
class DiscoveredOpenApiAction:
    operation_id: str
    url: str


@dataclass(frozen=True, slots=True)
class DiscoveredAction:
    authentication: AuthenticationRequirement
    description: str
    http: DiscoveredHttpAction | None
    id: str
    openapi: DiscoveredOpenApiAction | None
    rel: ActionRelation


@dataclass(frozen=True, slots=True)
class OfferingDetails:
    actions: tuple[DiscoveredAction, ...]
    attribute_schema: dict[str, object] | None
    issues: tuple[OfferingIssue, ...]
    offering: Offering


@dataclass(frozen=True, slots=True)
class ResolvedAction:
    action: DiscoveredAction
    openapi_document: dict[str, object] | None = None
    operation: dict[str, object] | None = None
    request_schema: dict[str, object] | None = None


async def get_offering_details(client: ServiceClient, identifier: str) -> OfferingDetails:
    inspection = await client.inspect()
    offering = await client.get_offering(identifier)
    service_openapi = (
        inspection.document.http.openapi.url if inspection.document.http.openapi is not None else ""
    )
    actions, issues = _normalize_actions(offering.actions, client.service_origin, service_openapi)
    attribute_schema: dict[str, object] | None = None
    if offering.schema_ is not None:
        try:
            target = _resolve_https_reference(offering.schema_.url, client.service_origin)
            resolved_schema = await resolve_schema(client, target)
            attribute_schema = resolved_schema.schema
            if not resolved_schema.validator.is_valid(offering.attributes):
                offering = offering.model_copy(update={"attributes": {}})
                issues.append(
                    OfferingIssue(
                        OfferingIssueScope.ATTRIBUTES,
                        "Offering attributes do not match their Attribute Schema",
                    )
                )
        except (AgentError, ValueError) as error:
            offering = offering.model_copy(update={"attributes": {}})
            issues.append(OfferingIssue(OfferingIssueScope.ATTRIBUTE_SCHEMA, str(error)))
    return OfferingDetails(tuple(actions), attribute_schema, tuple(issues), offering)


async def resolve_action(client: ServiceClient, offering_id: str, action_id: str) -> ResolvedAction:
    details = await get_offering_details(client, offering_id)
    action = next((item for item in details.actions if item.id == action_id), None)
    if action is None:
        raise AgentError(f"ODP Offering does not expose usable Action {action_id}")
    if action.http is not None:
        request_schema = None
        if action.http.request is not None and action.http.request.schema_ is not None:
            target = _resolve_https_reference(
                action.http.request.schema_.url, client.service_origin
            )
            request_schema = (await resolve_schema(client, target)).schema
        return ResolvedAction(action=action, request_schema=request_schema)
    if action.openapi is None:
        raise AgentError("ODP Action has no usable target")
    document = await client._supporting_json(
        action.openapi.url,
        "openapi",
        "application/vnd.oai.openapi+json;version=3.1, application/json;q=0.9",
        {"application/vnd.oai.openapi+json", "application/json"},
        _MAXIMUM_OPENAPI_BYTES,
    )
    version = document.get("openapi")
    if not isinstance(version, str) or not version.startswith("3.1."):
        raise AgentError("ODP Action requires an OpenAPI 3.1 document")
    matches = _openapi_operations(document, action.openapi.operation_id)
    if len(matches) != 1:
        raise AgentError(
            f"ODP Action operation_id {action.openapi.operation_id} must resolve exactly once"
        )
    return ResolvedAction(action=action, openapi_document=document, operation=matches[0])


def _normalize_actions(
    actions: list[Action], service_origin: str, service_openapi: str
) -> tuple[list[DiscoveredAction], list[OfferingIssue]]:
    counts: dict[str, int] = {}
    for action in actions:
        counts[action.id] = counts.get(action.id, 0) + 1
    discovered: list[DiscoveredAction] = []
    issues: list[OfferingIssue] = []
    reported: set[str] = set()
    for action in actions:
        if counts[action.id] > 1:
            if action.id not in reported:
                reported.add(action.id)
                issues.append(
                    OfferingIssue(
                        OfferingIssueScope.ACTION,
                        f"Duplicate Action identifier {action.id}",
                        action.id,
                    )
                )
            continue
        try:
            value = _normalize_action(action, service_origin, service_openapi)
            if value is not None:
                discovered.append(value)
        except AgentError as error:
            issues.append(OfferingIssue(OfferingIssueScope.ACTION, str(error), action.id))
    return discovered, issues


def _normalize_action(
    action: Action, service_origin: str, service_openapi: str
) -> DiscoveredAction | None:
    if action.http is not None:
        return DiscoveredAction(
            authentication=action.authentication,
            description=action.description,
            http=DiscoveredHttpAction(
                method=action.http.method,
                request=action.http.request,
                response_content_types=tuple(action.http.response_content_types),
                url=_resolve_http_reference(action.http.href, service_origin),
            ),
            id=action.id,
            openapi=None,
            rel=action.rel,
        )
    if action.openapi is not None:
        target = action.openapi.url or service_openapi
        if not target:
            raise AgentError("OpenAPI Action has no OpenAPI document URL")
        return DiscoveredAction(
            authentication=action.authentication,
            description=action.description,
            http=None,
            id=action.id,
            openapi=DiscoveredOpenApiAction(
                action.openapi.operation_id,
                _resolve_https_reference(target, service_origin),
            ),
            rel=action.rel,
        )
    return None


def _resolve_http_reference(reference: str, base: str) -> str:
    target = urljoin(f"{base}/", reference)
    parsed = urlsplit(target)
    if parsed.scheme not in {"http", "https"} or parsed.hostname is None:
        raise AgentError("ODP Action target must use HTTP or HTTPS")
    return target


def _resolve_https_reference(reference: str, base: str) -> str:
    target = _resolve_http_reference(reference, base)
    if urlsplit(target).scheme != "https":
        raise AgentError("ODP supporting document URL must use HTTPS")
    return target


def _openapi_operations(document: dict[str, object], operation_id: str) -> list[dict[str, object]]:
    paths = document.get("paths")
    if not isinstance(paths, dict):
        raise AgentError("ODP OpenAPI document must contain paths")
    matches: list[dict[str, object]] = []
    for path in paths.values():
        if not isinstance(path, dict):
            continue
        for method in ("delete", "get", "head", "options", "patch", "post", "put", "trace"):
            operation = path.get(method)
            if isinstance(operation, dict) and operation.get("operationId") == operation_id:
                matches.append(operation)
    return matches
