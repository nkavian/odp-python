"""Effective Service and Collection search capabilities."""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, cast

from offering_protocol.agent.client import AgentError, ServiceClient
from offering_protocol.core import (
    Collection,
    FilterCapabilitySource,
    FilterDefinition,
    Operation,
    ReferenceError,
    SearchCapabilities,
    SortCapabilitySource,
    SortDefinition,
    parse_filter_definition_page,
    parse_sort_definition_page,
    resolve_continuation,
)
from offering_protocol.core.validation import _agent_body

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
    await _add_source(
        client,
        result,
        CapabilityKind.FILTERS,
        scope,
        capabilities.filters,
        result.filters,
        _MAXIMUM_FILTERS,
        _load_filters,
        None,
    )


async def _add_sorts(
    client: ServiceClient,
    result: SearchCapabilityCatalog,
    target: dict[str, SortDefinition],
    scopes: dict[str, CapabilityScope],
    scope: CapabilityScope,
    capabilities: SearchCapabilities,
) -> None:
    await _add_source(
        client,
        result,
        CapabilityKind.SORTS,
        scope,
        capabilities.sorts,
        target,
        _MAXIMUM_SORTS,
        _load_sorts,
        scopes,
    )


async def _add_source(
    client: ServiceClient,
    result: SearchCapabilityCatalog,
    kind: CapabilityKind,
    scope: CapabilityScope,
    source: FilterCapabilitySource | SortCapabilitySource | None,
    target: dict[str, Any],
    maximum: int,
    load: Callable[[ServiceClient, str, int, frozenset[str]], Awaitable[list[Any]]],
    scopes: dict[str, CapabilityScope] | None,
) -> None:
    """Merges one capability source into the effective catalog, or reports why it cannot be.

    FLT-55 makes a source atomic: every page is retrieved and the source's own rules are enforced
    before any of its definitions is exposed. So the work below decides everything first and writes
    to `target` only once the source has been accepted -- an invalid or oversized source leaves the
    sources merged before it exactly as they were.
    """
    if source is None:
        return
    try:
        values: Sequence[Any] = (
            await load(client, source.linked.href, maximum, frozenset(target))
            if source.linked is not None
            else list(source.inline)
        )
        # FLT-55: a source enforces its own uniqueness, so an identifier this source publishes
        # twice makes the whole source unusable -- there is no basis for choosing between them.
        identifiers: set[str] = set()
        for value in values:
            if value.id in identifiers:
                raise AgentError(f"Duplicate {kind.value} identifier {value.id} within one source")
            identifiers.add(value.id)
        # An identifier two effective sources both publish is quarantined instead: neither copy
        # wins, and nothing else about either source is affected.
        shared = sorted(identifier for identifier in identifiers if identifier in target)
        accepted = [value for value in values if value.id not in shared]
        # FLT-62: the bound is checked before anything is written, so a source that overflows the
        # effective catalog cannot take the earlier valid sources down with it.
        if len(target) - len(shared) + len(accepted) > maximum:
            raise AgentError(f"Effective {kind.value} exceed their limit")
    except AgentError as error:
        result.issues.append(CapabilityIssue(kind, str(error), scope))
        return
    for identifier in shared:
        target.pop(identifier, None)
        if scopes is not None:
            scopes.pop(identifier, None)
    for value in accepted:
        target[value.id] = value
        if scopes is not None:
            scopes[value.id] = scope
    if shared:
        result.issues.append(
            CapabilityIssue(kind, f"Duplicate {kind.value}: {', '.join(shared)}", scope)
        )


async def _load_filters(
    client: ServiceClient,
    reference: str,
    budget: int = _MAXIMUM_FILTERS,
    existing: frozenset[str] = frozenset(),
) -> list[FilterDefinition]:
    values = await _load_definitions(
        client, reference, budget, parse_filter_definition_page, CapabilityKind.FILTERS, existing
    )
    return cast("list[FilterDefinition]", values)


async def _load_sorts(
    client: ServiceClient,
    reference: str,
    budget: int = _MAXIMUM_SORTS,
    existing: frozenset[str] = frozenset(),
) -> list[SortDefinition]:
    values = await _load_definitions(
        client, reference, budget, parse_sort_definition_page, CapabilityKind.SORTS, existing
    )
    return cast("list[SortDefinition]", values)


async def _load_definitions(
    client: ServiceClient,
    reference: str,
    budget: int,
    parse: Callable[[bytes | str], Any],
    kind: CapabilityKind,
    existing: frozenset[str],
) -> list[Any]:
    """Stop when new identifiers cannot fit even if all earlier identifiers are quarantined."""

    def parse_page(body: bytes | str) -> Any:
        return parse(
            _agent_body(body, "filter-page" if kind is CapabilityKind.FILTERS else "sort-page")
        )

    values: list[Any] = []
    next_reference = reference
    visited: set[str] = set()
    for _ in range(_MAXIMUM_CAPABILITY_PAGES):
        if not next_reference:
            return values
        target = _resolve_reference(next_reference, client.service_origin)
        if target in visited:
            raise AgentError("ODP capability pagination loop detected")
        visited.add(target)
        fallback = (
            client._cache_fallbacks.filters
            if kind is CapabilityKind.FILTERS
            else client._cache_fallbacks.sorts
        )
        body = await client._linked_odp(target, fallback, parse_page)
        page = parse_page(body)
        values.extend(page.items)
        if len({value.id for value in values} - existing) > budget:
            raise AgentError(f"Effective {kind.value} exceed their limit")
        next_reference = page.next
    if next_reference:
        raise AgentError("ODP capability source exceeded 16 pages")
    return values


def _resolve_reference(reference: str, origin: str) -> str:
    """Resolves a linked capability reference, which stays on the Service that advertised it.

    FLT-52 makes `href` a same-origin Resource Reference and FLT-53 puts every `next` under the
    common continuation contract, so both are resolved the way a page continuation is. Without
    that, a Service Document written by somebody else could send this Agent's ODP requests to a
    host of its choosing.
    """
    try:
        return resolve_continuation(reference, origin)
    except ReferenceError as error:
        raise AgentError(str(error)) from error
