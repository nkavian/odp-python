"""Agent-side ODP Service client."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from email.utils import parsedate_to_datetime
from enum import StrEnum
from typing import TYPE_CHECKING, cast
from urllib.parse import parse_qsl, urlencode, urljoin, urlsplit, urlunsplit

from offering_protocol.agent.cache import Cache, CacheFallbacks, CacheRecord, MemoryCache, utc_now
from offering_protocol.core import (
    Collection,
    CollectionSearchRequest,
    Offering,
    OfferingPage,
    OfferingSearchRequest,
    Operation,
    Page,
    ProblemDetails,
    Representation,
    ServiceDocument,
    build_operation_url,
    derive_service_origin,
    parse_agent_service_document,
    resolve_continuation,
)
from offering_protocol.core import (
    parse_collection as parse_collection_strict,
)
from offering_protocol.core import (
    parse_collection_page as parse_collection_page_strict,
)
from offering_protocol.core import (
    parse_offering as parse_offering_strict,
)
from offering_protocol.core import (
    parse_offering_page as parse_offering_page_strict,
)
from offering_protocol.core import (
    parse_problem_response as parse_problem_response_strict,
)
from offering_protocol.core.validation import _normalize_agent_response
from offering_protocol.directory.transport import (
    HttpRequest,
    HttpResponse,
    HttpxTransport,
    Transport,
    TransportError,
)

if TYPE_CHECKING:
    from offering_protocol.agent.capabilities import SearchCapabilityCatalog
    from offering_protocol.agent.details import OfferingDetails, ResolvedAction

MEDIA_TYPE = "application/odp+json"
_MAXIMUM_DOCUMENT_BYTES = 65_536
_MAXIMUM_RESOURCE_BYTES = 524_288
_MAXIMUM_REDIRECTS = 5


class Freshness(StrEnum):
    FETCHED = "fetched"
    FRESH = "fresh"
    REVALIDATED = "revalidated"


@dataclass(frozen=True, slots=True)
class Inspection:
    document: ServiceDocument
    final_url: str
    freshness: Freshness
    requested_url: str
    service_origin: str


@dataclass(frozen=True, slots=True)
class TraversalOptions:
    max_items: int = 10_000
    max_pages: int = 16


class AgentError(RuntimeError):
    """Base error for Service discovery operations."""


class UnsupportedOperationError(AgentError):
    def __init__(self, operation: Operation) -> None:
        super().__init__(f"ODP Service does not advertise {operation.value}")
        self.operation = operation


class ServiceRequestError(AgentError):
    def __init__(self, status: int, message: str, headers: dict[str, str]) -> None:
        super().__init__(f"ODP request failed with HTTP {status}: {message}")
        self.status = status
        self.headers = headers


@dataclass(frozen=True, slots=True)
class _FetchedResponse:
    body: bytes
    final_url: str
    freshness: Freshness


class ServiceClient:
    def __init__(
        self,
        service_url: str,
        *,
        accept_language: str | None = None,
        allow_local_network: bool = False,
        cache: Cache | None = None,
        cache_fallbacks: CacheFallbacks | None = None,
        cache_partition: str = "anonymous",
        supporting_transport: Transport | None = None,
        transport: Transport | None = None,
    ) -> None:
        self.service_origin = derive_service_origin(service_url)
        self._accept_language = accept_language
        self._cache = cache or MemoryCache()
        self._cache_fallbacks = cache_fallbacks or CacheFallbacks()
        self._cache_partition = cache_partition
        self._owns_transport = transport is None
        self._transport = transport or HttpxTransport(allow_local_network=allow_local_network)
        self._supporting_transport = supporting_transport or self._transport

    async def __aenter__(self) -> ServiceClient:
        return self

    async def __aexit__(self, *args: object) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        if self._owns_transport:
            await self._transport.aclose()

    async def inspect(self) -> Inspection:
        requested_url = f"{self.service_origin}/.well-known/odp"
        response = await self._request_cached(
            "GET",
            requested_url,
            b"",
            _MAXIMUM_DOCUMENT_BYTES,
            self._cache_fallbacks.service_document,
            parse_agent_service_document,
        )
        return Inspection(
            document=parse_agent_service_document(response.body),
            final_url=response.final_url,
            freshness=response.freshness,
            requested_url=requested_url,
            service_origin=self.service_origin,
        )

    async def get_offering_details(self, identifier: str) -> OfferingDetails:
        from offering_protocol.agent.details import get_offering_details

        return await get_offering_details(self, identifier)

    async def resolve_action(self, offering_id: str, action_id: str) -> ResolvedAction:
        from offering_protocol.agent.details import resolve_action

        return await resolve_action(self, offering_id, action_id)

    async def get_collection_search_capabilities(self, identifier: str) -> SearchCapabilityCatalog:
        from offering_protocol.agent.capabilities import get_collection_search_capabilities

        return await get_collection_search_capabilities(self, identifier)

    async def get_offering_search_capabilities(
        self, collection_id: str | None = None
    ) -> SearchCapabilityCatalog:
        from offering_protocol.agent.capabilities import get_offering_search_capabilities

        return await get_offering_search_capabilities(self, collection_id)

    async def list_collections(
        self, representation: Representation = Representation.TERSE, limit: int = 0
    ) -> Page[Collection]:
        body = await self._get_page(Operation.LIST_COLLECTIONS, None, representation, limit)
        page = parse_collection_page(body)
        for item in page.items:
            parse_collection(_encode(item))
        return page

    async def get_collection(self, identifier: str) -> Collection:
        body = await self._get_page(Operation.GET_COLLECTION, identifier, Representation.FULL, 0)
        return parse_collection(body)

    async def search_collections(
        self,
        request: CollectionSearchRequest,
        representation: Representation = Representation.TERSE,
    ) -> Page[Collection]:
        body = await self._post_search(
            Operation.SEARCH_COLLECTIONS, request.to_dict(), representation
        )
        page = parse_collection_page(body)
        for item in page.items:
            parse_collection(_encode(item))
        return page

    async def list_offerings(
        self, representation: Representation = Representation.TERSE, limit: int = 0
    ) -> OfferingPage[Offering]:
        body = await self._get_page(Operation.LIST_OFFERINGS, None, representation, limit)
        return parse_offering_page(body)

    async def list_collection_offerings(
        self,
        collection_id: str,
        representation: Representation = Representation.TERSE,
        limit: int = 0,
    ) -> OfferingPage[Offering]:
        body = await self._get_page(
            Operation.LIST_COLLECTION_OFFERINGS, collection_id, representation, limit
        )
        return parse_offering_page(body)

    async def get_offering(self, identifier: str) -> Offering:
        body = await self._get_page(Operation.GET_OFFERING, identifier, Representation.FULL, 0)
        return parse_offering(body)

    async def search_offerings(
        self,
        request: OfferingSearchRequest,
        representation: Representation = Representation.TERSE,
    ) -> OfferingPage[Offering]:
        body = await self._post_search(
            Operation.SEARCH_OFFERINGS, request.to_dict(), representation
        )
        return parse_offering_page(body)

    async def continue_collections(self, next_reference: str) -> Page[Collection]:
        target = resolve_continuation(next_reference, self.service_origin)
        response = await self._request_cached(
            "GET",
            target,
            b"",
            _MAXIMUM_RESOURCE_BYTES,
            self._cache_fallbacks.collection,
            parse_collection_page,
        )
        return parse_collection_page(response.body)

    async def continue_offerings(self, next_reference: str) -> OfferingPage[Offering]:
        target = resolve_continuation(next_reference, self.service_origin)
        response = await self._request_cached(
            "GET",
            target,
            b"",
            _MAXIMUM_RESOURCE_BYTES,
            self._cache_fallbacks.offering,
            parse_offering_page,
        )
        return parse_offering_page(response.body)

    async def list_all_collections(
        self,
        representation: Representation = Representation.TERSE,
        limit: int = 0,
        options: TraversalOptions | None = None,
    ) -> list[Collection]:
        maximum_items, maximum_pages = _traversal_bounds(options or TraversalOptions())
        page = await self.list_collections(representation, limit)
        result: list[Collection] = []
        for page_number in range(maximum_pages):
            result.extend(page.items[: maximum_items - len(result)])
            if len(result) == maximum_items or not page.next:
                break
            if page_number + 1 < maximum_pages:
                page = await self.continue_collections(page.next)
        return result

    async def list_all_offerings(
        self,
        representation: Representation = Representation.TERSE,
        limit: int = 0,
        options: TraversalOptions | None = None,
    ) -> list[Offering]:
        resolved_options = options or TraversalOptions()
        _traversal_bounds(resolved_options)
        page = await self.list_offerings(representation, limit)
        return await self._collect_offerings(page, resolved_options)

    async def search_all_offerings(
        self,
        request: OfferingSearchRequest,
        representation: Representation = Representation.TERSE,
        options: TraversalOptions | None = None,
    ) -> list[Offering]:
        resolved_options = options or TraversalOptions()
        _traversal_bounds(resolved_options)
        page = await self.search_offerings(request, representation)
        return await self._collect_offerings(page, resolved_options)

    async def _collect_offerings(
        self, page: OfferingPage[Offering], options: TraversalOptions
    ) -> list[Offering]:
        result: list[Offering] = []
        maximum_items, maximum_pages = _traversal_bounds(options)
        for page_number in range(maximum_pages):
            result.extend(page.items[: maximum_items - len(result)])
            if len(result) == maximum_items or not page.next:
                break
            if page_number + 1 < maximum_pages:
                page = await self.continue_offerings(page.next)
        return result

    async def _get_page(
        self,
        operation: Operation,
        identifier: str | None,
        representation: Representation,
        limit: int,
    ) -> bytes:
        inspection = await self._require_operation(operation)
        target = build_operation_url(
            inspection.document.http.endpoint_base,
            operation,
            self.service_origin,
            identifier,
        )
        query = {"representation": representation.value}
        if limit:
            query["limit"] = str(limit)
        target = _append_query(target, query)
        fallback = (
            self._cache_fallbacks.collection
            if operation
            in {
                Operation.GET_COLLECTION,
                Operation.LIST_COLLECTIONS,
                Operation.SEARCH_COLLECTIONS,
            }
            else self._cache_fallbacks.offering
        )
        parser = _operation_parser(operation)
        response = await self._request_cached(
            "GET", target, b"", _MAXIMUM_RESOURCE_BYTES, fallback, parser
        )
        return response.body

    async def _post_search(
        self, operation: Operation, value: Mapping[str, object], representation: Representation
    ) -> bytes:
        inspection = await self._require_operation(operation)
        target = build_operation_url(
            inspection.document.http.endpoint_base, operation, self.service_origin, None
        )
        target = _append_query(target, {"representation": representation.value})
        fallback = (
            self._cache_fallbacks.collection
            if operation is Operation.SEARCH_COLLECTIONS
            else self._cache_fallbacks.offering
        )
        response = await self._request_cached(
            "POST",
            target,
            json.dumps(value, separators=(",", ":")).encode(),
            _MAXIMUM_RESOURCE_BYTES,
            fallback,
            _operation_parser(operation),
        )
        return response.body

    async def _require_operation(self, operation: Operation) -> Inspection:
        inspection = await self.inspect()
        if not any(item.name is operation for item in inspection.document.operations):
            raise UnsupportedOperationError(operation)
        return inspection

    async def _request_cached(
        self,
        method: str,
        target: str,
        body: bytes,
        maximum_bytes: int,
        fallback: timedelta,
        parser: object,
    ) -> _FetchedResponse:
        key = self._cache_key(method, target, body)
        cached = self._cache.get(key)
        now = utc_now()
        if cached is not None and now < cached.expires:
            return _FetchedResponse(cached.body, cached.final_url, Freshness.FRESH)
        headers: dict[str, str] = {}
        request_target = target
        if cached is not None:
            if derive_service_origin(cached.final_url) == derive_service_origin(target):
                request_target = cached.final_url
            if cached.etag:
                headers["if-none-match"] = cached.etag
            if cached.last_modified:
                headers["if-modified-since"] = cached.last_modified
        response, final_url = await self._request_raw(method, request_target, body, headers)
        if response.status == 304:
            if cached is None:
                raise AgentError("ODP response returned 304 without a cached representation")
            if _no_store(response.headers):
                self._cache.delete(key)
                return _FetchedResponse(cached.body, cached.final_url, Freshness.REVALIDATED)
            lifetime = cached.expires - cached.stored
            expires = (
                _expiration(response.headers, fallback, now)
                if _has_freshness(response.headers)
                else now + max(lifetime, timedelta())
            )
            record = replace(cached, expires=expires, final_url=final_url, stored=now)
            self._cache.set(key, record)
            return _FetchedResponse(record.body, record.final_url, Freshness.REVALIDATED)
        response = _consume(response, maximum_bytes)
        _invoke_parser(parser, response.body)
        if _cacheable(method, response.headers, fallback):
            self._cache.set(
                key,
                CacheRecord(
                    body=response.body,
                    etag=response.headers.get("etag"),
                    expires=_expiration(response.headers, fallback, now),
                    final_url=final_url,
                    last_modified=response.headers.get("last-modified"),
                    status=response.status,
                    stored=now,
                ),
            )
        else:
            self._cache.delete(key)
        return _FetchedResponse(response.body, final_url, Freshness.FETCHED)

    async def _request_raw(
        self, method: str, target: str, body: bytes, conditional: dict[str, str]
    ) -> tuple[HttpResponse, str]:
        redirect_origin = derive_service_origin(target)
        for redirects in range(_MAXIMUM_REDIRECTS + 1):
            headers = {"accept": MEDIA_TYPE, **conditional}
            if self._accept_language:
                headers["accept-language"] = self._accept_language
            if body:
                headers["content-type"] = MEDIA_TYPE
            try:
                response = await self._transport.send(HttpRequest(method, target, headers, body))
            except TransportError as error:
                raise AgentError(f"ODP Service request failed: {error}") from error
            if response.status not in {301, 302, 303, 307, 308}:
                return response, target
            if redirects == _MAXIMUM_REDIRECTS:
                raise AgentError("ODP response exceeded five redirects")
            location = response.headers.get("location")
            if location is None:
                raise AgentError("ODP redirect omitted Location")
            next_target = urljoin(target, location)
            if derive_service_origin(next_target) != redirect_origin:
                raise AgentError("ODP redirect changed Service origin")
            if response.status == 303 or (response.status in {301, 302} and method == "POST"):
                method, body = "GET", b""
            target = next_target
        raise AgentError("ODP response exceeded its redirect limit")  # pragma: no cover

    async def _linked_odp(self, target: str, fallback: timedelta, parser: object) -> bytes:
        response = await self._request_cached(
            "GET", target, b"", _MAXIMUM_RESOURCE_BYTES, fallback, parser
        )
        return response.body

    async def _supporting_json(
        self,
        target: str,
        resource_class: str,
        accept: str,
        media_types: set[str],
        maximum_bytes: int,
    ) -> dict[str, object]:
        current = target
        if not _is_https_url(current):
            raise AgentError("ODP supporting document URL must use HTTPS")
        key = f"anonymous:{resource_class}\nGET\n{target}\n{accept}"
        cached = self._cache.get(key)
        now = utc_now()
        if cached is not None and now < cached.expires:
            return _decode_json_object(cached.body)
        conditional: dict[str, str] = {}
        if cached is not None:
            if cached.etag:
                conditional["if-none-match"] = cached.etag
            if cached.last_modified:
                conditional["if-modified-since"] = cached.last_modified
        for redirects in range(_MAXIMUM_REDIRECTS + 1):
            try:
                response = await self._supporting_transport.send(
                    HttpRequest("GET", current, {"accept": accept, **conditional})
                )
            except TransportError as error:
                raise AgentError(f"ODP supporting document request failed: {error}") from error
            if response.status in {301, 302, 303, 307, 308}:
                if redirects == _MAXIMUM_REDIRECTS:
                    raise AgentError("ODP supporting document exceeded five redirects")
                location = response.headers.get("location")
                if location is None:
                    raise AgentError("ODP supporting document redirect omitted Location")
                current = urljoin(current, location)
                if not _is_https_url(current):
                    raise AgentError("ODP supporting document redirect must use HTTPS")
                continue
            if response.status == 304:
                if cached is None:
                    raise AgentError(
                        "ODP supporting document returned 304 without a cached representation"
                    )
                if _no_store(response.headers):
                    self._cache.delete(key)
                else:
                    lifetime = cached.expires - cached.stored
                    expires = (
                        _expiration(response.headers, timedelta(), now)
                        if _has_freshness(response.headers)
                        else now + max(lifetime, timedelta())
                    )
                    self._cache.set(
                        key,
                        replace(cached, expires=expires, final_url=current, stored=now),
                    )
                return _decode_json_object(cached.body)
            if not 200 <= response.status < 300:
                raise ServiceRequestError(
                    response.status,
                    f"ODP supporting document returned HTTP {response.status}",
                    response.headers,
                )
            if len(response.body) > maximum_bytes:
                raise AgentError("ODP supporting document exceeds its byte limit")
            content_type = response.headers.get("content-type", "").split(";", 1)[0].strip().lower()
            if content_type not in media_types:
                raise AgentError("ODP supporting document has an unsupported media type")
            value = _decode_json_object(response.body)
            if _cacheable("GET", response.headers, timedelta()):
                self._cache.set(
                    key,
                    CacheRecord(
                        body=response.body,
                        etag=response.headers.get("etag"),
                        expires=_expiration(response.headers, timedelta(), now),
                        final_url=current,
                        last_modified=response.headers.get("last-modified"),
                        status=response.status,
                        stored=now,
                    ),
                )
            else:
                self._cache.delete(key)
            return value
        raise AgentError("ODP supporting document exceeded its redirect limit")  # pragma: no cover

    def _cache_key(self, method: str, target: str, body: bytes) -> str:
        digest = hashlib.sha256(body).hexdigest()
        return "\n".join(
            (self._cache_partition, method, target, self._accept_language or "", digest)
        )


def _invoke_parser(parser: object, body: bytes) -> None:
    if not callable(parser):
        raise TypeError("ODP parser is not callable")
    try:
        parser(body)
    except ValueError as error:
        raise AgentError(str(error)) from error


def _operation_parser(operation: Operation) -> object:
    if operation is Operation.GET_COLLECTION:
        return parse_collection
    if operation is Operation.GET_OFFERING:
        return parse_offering
    if operation in {Operation.LIST_COLLECTIONS, Operation.SEARCH_COLLECTIONS}:
        return parse_collection_page
    return parse_offering_page


def parse_collection(data: bytes | str) -> Collection:
    return parse_collection_strict(_normalize_body(data, "collection"))


def parse_offering(data: bytes | str) -> Offering:
    return parse_offering_strict(_normalize_body(data, "offering"))


def parse_collection_page(data: bytes | str) -> Page[Collection]:
    return parse_collection_page_strict(_normalize_body(data, "collection-page"))


def parse_offering_page(data: bytes | str) -> OfferingPage[Offering]:
    return parse_offering_page_strict(_normalize_body(data, "offering-page"))


def parse_problem_response(data: bytes | str, status: int) -> ProblemDetails:
    return parse_problem_response_strict(_normalize_body(data, "problem"), status)


def _normalize_body(data: bytes | str, kind: str) -> str:
    raw = json.loads(data)
    if not isinstance(raw, dict):
        return data.decode() if isinstance(data, bytes) else data
    return json.dumps(_normalize_agent_response(raw, kind), separators=(",", ":"))


def _encode(value: object) -> bytes:
    if not hasattr(value, "model_dump_json"):
        raise TypeError("ODP model is not serializable")
    encoded = value.model_dump_json(by_alias=True, exclude_unset=True)
    return cast(str, encoded).encode()


def _append_query(target: str, values: dict[str, str]) -> str:
    parts = urlsplit(target)
    query = dict(parse_qsl(parts.query, keep_blank_values=True))
    query.update(values)
    return urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(query), parts.fragment))


def _is_https_url(value: str) -> bool:
    parsed = urlsplit(value)
    return parsed.scheme == "https" and parsed.hostname is not None


def _decode_json_object(data: bytes) -> dict[str, object]:
    try:
        value = json.loads(data)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise AgentError(f"ODP supporting document is invalid JSON: {error}") from error
    if not isinstance(value, dict) or any(not isinstance(key, str) for key in value):
        raise AgentError("ODP supporting document must be a JSON object")
    return cast(dict[str, object], value)


def _consume(response: HttpResponse, maximum_bytes: int) -> HttpResponse:
    if len(response.body) > maximum_bytes:
        raise AgentError("ODP response exceeds its byte limit")
    if not 200 <= response.status < 300:
        try:
            problem = parse_problem_response(response.body, response.status)
            message = problem.detail or problem.title
        except ValueError:
            message = response.body.decode(errors="replace")
        raise ServiceRequestError(response.status, message, response.headers)
    content_type = response.headers.get("content-type", "").split(";", 1)[0].strip().lower()
    if content_type != MEDIA_TYPE:
        raise AgentError(f"ODP response must use {MEDIA_TYPE}")
    return response


def _traversal_bounds(options: TraversalOptions) -> tuple[int, int]:
    if not 1 <= options.max_items <= 10_000 or not 1 <= options.max_pages <= 16:
        raise AgentError("traversal exceeds 10000 items or 16 pages")
    return options.max_items, options.max_pages


def _cache_directives(headers: dict[str, str]) -> dict[str, str]:
    result: dict[str, str] = {}
    for value in headers.get("cache-control", "").split(","):
        value = value.strip()
        if value:
            name, _, setting = value.partition("=")
            result[name.lower()] = setting.strip('"')
    return result


def _no_store(headers: dict[str, str]) -> bool:
    return "no-store" in _cache_directives(headers)


def _has_freshness(headers: dict[str, str]) -> bool:
    directives = _cache_directives(headers)
    return bool({"max-age", "no-cache", "no-store"} & directives.keys()) or "expires" in headers


def _cacheable(method: str, headers: dict[str, str], fallback: timedelta) -> bool:
    vary = {value.strip().lower() for value in headers.get("vary", "").split(",") if value.strip()}
    if not vary <= {"accept", "accept-language", "content-type"} or _no_store(headers):
        return False
    directives = _cache_directives(headers)
    explicit = "max-age" in directives or "expires" in headers
    return (method == "GET" and (fallback > timedelta() or "no-cache" in directives)) or explicit


def _expiration(headers: dict[str, str], fallback: timedelta, now: datetime) -> datetime:
    directives = _cache_directives(headers)
    if "no-cache" in directives:
        return now
    try:
        duration = max(0, int(directives["max-age"]) - int(headers.get("age", "0")))
        return now + timedelta(seconds=duration)
    except (KeyError, ValueError):
        pass
    if "expires" in headers:
        try:
            return parsedate_to_datetime(headers["expires"])
        except (TypeError, ValueError):
            pass
    return now + fallback
