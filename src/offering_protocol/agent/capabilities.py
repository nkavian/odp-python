"""Effective Service and Collection search capabilities."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from urllib.parse import urljoin, urlsplit

from offering_protocol.agent.client import AgentError, ServiceClient
from offering_protocol.core import (
    Collection,
    FilterDefinition,
    Operation,
    SearchCapabilities,
    SortDefinition,
    parse_filter_definition_page,
    parse_sort_definition_page,
)

_MAXIMUM_CAPABILITY_PAGES = 16
_MAXIMUM_FILTERS = 1_024
_MAXIMUM_SORTS = 128


class CapabilityScope(StrEnum):
    COLLECTION = "collection"
    SERVICE = "service"


class CapabilityKind(StrEnum):
    FILTERS = "filters"
    SORTS = "sorts"


@dataclass(frozen=True, slots=True)
class CapabilityIssue:
    kind: CapabilityKind
    message: str
    scope: CapabilityScope


@dataclass(frozen=True, slots=True)
class ResolvedSortDefinition:
    definition: SortDefinition
    filters: tuple[FilterDefinition, ...]


@dataclass(slots=True)
class SearchCapabilityCatalog:
    filters: dict[str, FilterDefinition] = field(default_factory=dict)
    issues: list[CapabilityIssue] = field(default_factory=list)
    sorts: dict[str, ResolvedSortDefinition] = field(default_factory=dict)


async def get_collection_search_capabilities(
    client: ServiceClient, identifier: str
) -> SearchCapabilityCatalog:
    collection = await client.get_collection(identifier)
    return await _resolve(client, collection)


async def get_offering_search_capabilities(
    client: ServiceClient, collection_id: str | None
) -> SearchCapabilityCatalog:
    collection = await client.get_collection(collection_id) if collection_id else None
    return await _resolve(client, collection)


async def _resolve(client: ServiceClient, collection: Collection | None) -> SearchCapabilityCatalog:
    inspection = await client.inspect()
    result = SearchCapabilityCatalog()
    supports_search = any(
        item.name is Operation.SEARCH_OFFERINGS for item in inspection.document.operations
    )
    if not supports_search:
        if collection is not None and collection.search_capabilities is not None:
            result.issues.append(
                CapabilityIssue(
                    CapabilityKind.FILTERS,
                    "Collection search capabilities require search-offerings",
                    CapabilityScope.COLLECTION,
                )
            )
        return result
    sorts: dict[str, SortDefinition] = {}
    sort_scopes: dict[str, CapabilityScope] = {}
    sources = (
        (inspection.document.search_capabilities, CapabilityScope.SERVICE),
        (
            collection.search_capabilities if collection is not None else None,
            CapabilityScope.COLLECTION,
        ),
    )
    for capabilities, scope in sources:
        if capabilities is None:
            continue
        await _add_filters(client, result, scope, capabilities)
        await _add_sorts(client, result, sorts, sort_scopes, scope, capabilities)
    for identifier, definition in sorts.items():
        filters = tuple(
            result.filters[key.filter_id]
            for key in definition.keys
            if key.filter_id in result.filters
        )
        if len(filters) != len(definition.keys):
            result.issues.append(
                CapabilityIssue(
                    CapabilityKind.SORTS,
                    f"Sort {identifier} references an unavailable filter",
                    sort_scopes[identifier],
                )
            )
        else:
            result.sorts[identifier] = ResolvedSortDefinition(definition, filters)
    return result


async def _add_filters(
    client: ServiceClient,
    result: SearchCapabilityCatalog,
    scope: CapabilityScope,
    capabilities: SearchCapabilities,
) -> None:
    if capabilities.filters is None:
        return
    try:
        values = (
            await _load_filters(client, capabilities.filters.linked.href)
            if capabilities.filters.linked is not None
            else capabilities.filters.inline
        )
    except AgentError as error:
        result.issues.append(CapabilityIssue(CapabilityKind.FILTERS, str(error), scope))
        return
    duplicates = _duplicates((item.id for item in values), result.filters)
    for identifier in duplicates:
        result.filters.pop(identifier, None)
    accepted = [item for item in values if item.id not in duplicates]
    if len(result.filters) + len(accepted) > _MAXIMUM_FILTERS:
        result.issues.append(
            CapabilityIssue(
                CapabilityKind.FILTERS,
                "Effective filters exceed 1024 entries",
                scope,
            )
        )
        return
    result.filters.update((item.id, item) for item in accepted)
    _report_duplicates(duplicates, CapabilityKind.FILTERS, scope, result.issues)


async def _add_sorts(
    client: ServiceClient,
    result: SearchCapabilityCatalog,
    target: dict[str, SortDefinition],
    scopes: dict[str, CapabilityScope],
    scope: CapabilityScope,
    capabilities: SearchCapabilities,
) -> None:
    if capabilities.sorts is None:
        return
    try:
        values = (
            await _load_sorts(client, capabilities.sorts.linked.href)
            if capabilities.sorts.linked is not None
            else capabilities.sorts.inline
        )
    except AgentError as error:
        result.issues.append(CapabilityIssue(CapabilityKind.SORTS, str(error), scope))
        return
    duplicates = _duplicates((item.id for item in values), target)
    for identifier in duplicates:
        target.pop(identifier, None)
        scopes.pop(identifier, None)
    accepted = [item for item in values if item.id not in duplicates]
    if len(target) + len(accepted) > _MAXIMUM_SORTS:
        result.issues.append(
            CapabilityIssue(CapabilityKind.SORTS, "Effective sorts exceed 128 entries", scope)
        )
        return
    target.update((item.id, item) for item in accepted)
    scopes.update((item.id, scope) for item in accepted)
    _report_duplicates(duplicates, CapabilityKind.SORTS, scope, result.issues)


async def _load_filters(client: ServiceClient, reference: str) -> list[FilterDefinition]:
    values: list[FilterDefinition] = []
    next_reference = reference
    visited: set[str] = set()
    for _ in range(_MAXIMUM_CAPABILITY_PAGES):
        if not next_reference:
            return values
        target = _resolve_reference(next_reference, client.service_origin)
        if target in visited:
            raise AgentError("ODP capability pagination loop detected")
        visited.add(target)
        body = await client._linked_odp(
            target, client._cache_fallbacks.collection, parse_filter_definition_page
        )
        page = parse_filter_definition_page(body)
        values.extend(page.items)
        next_reference = page.next
    if next_reference:
        raise AgentError("ODP capability source exceeded 16 pages")
    return values


async def _load_sorts(client: ServiceClient, reference: str) -> list[SortDefinition]:
    values: list[SortDefinition] = []
    next_reference = reference
    visited: set[str] = set()
    for _ in range(_MAXIMUM_CAPABILITY_PAGES):
        if not next_reference:
            return values
        target = _resolve_reference(next_reference, client.service_origin)
        if target in visited:
            raise AgentError("ODP capability pagination loop detected")
        visited.add(target)
        body = await client._linked_odp(
            target, client._cache_fallbacks.collection, parse_sort_definition_page
        )
        page = parse_sort_definition_page(body)
        values.extend(page.items)
        next_reference = page.next
    if next_reference:
        raise AgentError("ODP capability source exceeded 16 pages")
    return values


def _resolve_reference(reference: str, origin: str) -> str:
    target = urljoin(f"{origin}/", reference)
    parsed = urlsplit(target)
    if parsed.scheme not in {"http", "https"} or parsed.hostname is None:
        raise AgentError("ODP capability reference must use HTTP or HTTPS")
    return target


def _duplicates(values: Iterable[str], existing: Mapping[str, object]) -> set[str]:
    seen: set[str] = set()
    duplicates: set[str] = set()
    for value in values:
        if value in seen or value in existing:
            duplicates.add(value)
        seen.add(value)
    return duplicates


def _report_duplicates(
    duplicates: set[str],
    kind: CapabilityKind,
    scope: CapabilityScope,
    issues: list[CapabilityIssue],
) -> None:
    if duplicates:
        issues.append(
            CapabilityIssue(
                kind,
                f"Duplicate {kind.value}: {', '.join(sorted(duplicates))}",
                scope,
            )
        )
