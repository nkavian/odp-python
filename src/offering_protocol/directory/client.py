"""Canonical production and sandbox Directory client."""

from __future__ import annotations

import json
import re
from ipaddress import ip_address
from urllib.parse import urlencode, urljoin, urlsplit

from pydantic import ValidationError as ModelValidationError

from offering_protocol.core import (
    OdpValidationError,
    ReferenceError,
    ServiceDocument,
    derive_service_origin,
    parse_agent_service_document,
)
from offering_protocol.directory.addresses import is_public
from offering_protocol.directory.models import (
    DirectoryService,
    Environment,
    IterationOptions,
    ResourceSearchRequest,
    SearchPage,
    SearchRequest,
    SearchResponse,
    ServiceIssue,
    SuggestionRequest,
)
from offering_protocol.directory.results import parse_search_response
from offering_protocol.directory.transport import (
    HttpRequest,
    HttpResponse,
    HttpxTransport,
    Transport,
)

_MAXIMUM_REDIRECTS = 5
_MAXIMUM_RESPONSE_BYTES = 524_288
_MAXIMUM_ERROR_CHARACTERS = 2_048
_RFC_3339 = re.compile(
    r"^\d{4}-\d{2}-\d{2}[Tt]\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:[Zz]|[+-]\d{2}:\d{2})$"
)


class DirectoryError(RuntimeError):
    """Base error for canonical Directory operations."""


class DirectoryRequestError(DirectoryError):
    def __init__(self, status: int, message: str, headers: dict[str, str]) -> None:
        super().__init__(f"Directory request failed with HTTP {status}: {message}")
        self.status = status
        self.headers = headers


class DirectoryClient:
    def __init__(
        self,
        environment: Environment = Environment.PRODUCTION,
        *,
        transport: Transport | None = None,
    ) -> None:
        self.environment = environment
        self._owns_transport = transport is None
        self._transport = transport or HttpxTransport()

    async def __aenter__(self) -> DirectoryClient:
        return self

    async def __aexit__(self, *args: object) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        if self._owns_transport:
            await self._transport.aclose()

    async def search(self, request: ResourceSearchRequest) -> SearchResponse:
        _validate_search_request(request)
        if request.types is not None and (
            not request.types or len(set(request.types)) != len(request.types)
        ):
            raise DirectoryError("types must contain distinct service or collection values")
        response = await self._request(
            "POST",
            f"{self.environment.origin}/v1/directory/search",
            json.dumps(
                request.model_dump(mode="json", exclude_defaults=True, exclude_none=True)
            ).encode(),
        )
        return _parse_mixed_response(response.body)

    async def continue_search(self, next_reference: str) -> SearchResponse:
        response = await self._request("GET", self._continuation_url(next_reference))
        return _parse_mixed_response(response.body)

    async def search_services(self, request: SearchRequest) -> SearchPage:
        _validate_search_request(request)
        response = await self._request(
            "POST",
            f"{self.environment.origin}/v1/services/search",
            json.dumps(request.to_dict(), separators=(",", ":")).encode(),
        )
        return _parse_search_page(response.body)

    def _continuation_url(self, next_reference: str) -> str:
        if not next_reference.strip():
            raise DirectoryError("Directory continuation is empty")
        target = urljoin(f"{self.environment.origin}/", next_reference)
        # The continuation was written by the Directory, so a reference that is not a URL at all is
        # a Directory failure to report rather than an exception from the URL parser.
        if _origin_of(target, "Directory continuation") != self.environment.origin:
            raise DirectoryError("Directory continuation changed canonical origin")
        return target

    async def continue_search_services(self, next_reference: str) -> SearchPage:
        response = await self._request("GET", self._continuation_url(next_reference))
        return _parse_search_page(response.body)

    async def collect_services(
        self, request: SearchRequest, options: IterationOptions | None = None
    ) -> list[DirectoryService]:
        options = options or IterationOptions()
        maximum_items = _bounded(options.max_items, 10_000, 10_000, "max_items")
        maximum_responses = _bounded(options.max_pages, 16, 16, "max_pages")
        services: list[DirectoryService] = []
        page = await self.search_services(request)
        response_count = 1
        while True:
            services.extend(page.items[: maximum_items - len(services)])
            if (
                not page.next
                or len(services) == maximum_items
                or response_count == maximum_responses
            ):
                break
            page = await self.continue_search_services(page.next)
            response_count += 1
        return services

    async def suggest(self, request: SuggestionRequest) -> list[str]:
        return await self._suggestions("/v1/directory/suggestions", request, mixed=True)

    async def suggest_services(self, request: SuggestionRequest) -> list[str]:
        return await self._suggestions("/v1/services/suggestions", request)

    async def _suggestions(
        self, path: str, request: SuggestionRequest, *, mixed: bool = False
    ) -> list[str]:
        prefix = request.prefix.strip()
        if not prefix or len(prefix) > 128:
            raise DirectoryError("prefix must contain from 1 through 128 characters")
        if request.limit < 0 or request.limit > 25:
            raise DirectoryError("limit must be from 1 through 25")
        if mixed:
            _validate_search_request(SearchRequest(filters=request.filters))
            payload = request.model_dump(mode="json", exclude_defaults=True, exclude_none=True)
            payload["prefix"] = prefix
            response = await self._request(
                "POST", f"{self.environment.origin}{path}", json.dumps(payload).encode()
            )
        else:
            if request.filters is not None:
                raise DirectoryError("Service-only suggestions do not support filters")
            query = {"prefix": prefix}
            if request.limit:
                query["limit"] = str(request.limit)
            response = await self._request(
                "GET", f"{self.environment.origin}{path}?{urlencode(query)}"
            )
        try:
            envelope = json.loads(response.body)
            suggestions = envelope.get("items") if isinstance(envelope, dict) else None
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise DirectoryError(f"invalid Directory suggestions: {error}") from error
        if (
            not isinstance(suggestions, list)
            or len(suggestions) > 25
            or any(
                not isinstance(value, str)
                or not value
                or value.strip() != value
                or len(value) > 128
                for value in suggestions
            )
        ):
            raise DirectoryError("Directory suggestions are invalid")
        return suggestions

    async def _request(self, method: str, target: str, body: bytes = b"") -> HttpResponse:
        for redirects in range(_MAXIMUM_REDIRECTS + 1):
            headers = {"accept": "application/json"}
            if body:
                headers["content-type"] = "application/json"
            response = await self._transport.send(
                HttpRequest(method=method, url=target, headers=headers, body=body)
            )
            if response.status not in {301, 302, 303, 307, 308}:
                return _consume_response(response)
            if redirects == _MAXIMUM_REDIRECTS:
                raise DirectoryError("Directory response exceeded five redirects")
            location = response.headers.get("location")
            if location is None:
                raise DirectoryError("Directory redirect omitted Location")
            next_target = urljoin(target, location)
            if _origin_of(next_target, "Directory redirect") != _origin_of(target, "Directory"):
                raise DirectoryError("Directory redirect changed origin")
            if response.status == 303 or (response.status in {301, 302} and method == "POST"):
                method = "GET"
                body = b""
            target = next_target
        raise DirectoryError("Directory response exceeded its redirect limit")


def _parse_mixed_response(body: bytes) -> SearchResponse:
    try:
        return parse_search_response(body)
    except ValueError as error:
        raise DirectoryError(f"invalid Directory response: {error}") from error


def _parse_search_page(body: bytes) -> SearchPage:
    """Reads a Directory search page, keeping every record this client can read.

    ROLE-03: a Directory is a discovery aid, not an authority. One stale or nonconformant record
    used to reject the page, which made every other Service in the result undiscoverable; it is now
    dropped into `issues` and the rest of the page is handed back.
    """
    try:
        raw = json.loads(body)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise DirectoryError(f"invalid Directory response: {error}") from error
    if not isinstance(raw, dict) or not isinstance(raw.get("items"), list):
        raise DirectoryError("invalid Directory response: search page has no items")
    if len(raw["items"]) > 100:
        raise DirectoryError("Directory search page exceeds 100 Services")
    items: list[DirectoryService] = []
    issues: list[ServiceIssue] = []
    for index, entry in enumerate(raw["items"]):
        try:
            items.append(DirectoryService.model_validate(_read_service(entry)))
        except (ModelValidationError, OdpValidationError, ReferenceError, DirectoryError) as error:
            issues.append(ServiceIssue(index=index, message=str(error)))
    try:
        page = SearchPage.model_validate({**raw, "items": items})
    except (ModelValidationError, OdpValidationError) as error:
        raise DirectoryError(f"invalid Directory response: {error}") from error
    if page.facets is not None and any(
        facet.value.name.value != "tap" for facet in page.facets.trust
    ):
        raise DirectoryError("Directory trust facets are invalid")
    return page.model_copy(update={"issues": issues})


def _read_service(entry: object) -> dict[str, object]:
    """Validates one Directory record, or raises describing why it cannot be used.

    A Directory result echoes Service Document members, so those members are held to the Service
    Document's own rules rather than merely to their JSON types -- a Directory that published
    `"language": "not a tag"` would otherwise hand a caller a tag no language matcher can read.
    """
    if not isinstance(entry, dict):
        raise DirectoryError("Directory Service result is not an object")
    item = dict(entry)
    origin = item.get("service_origin")
    if not isinstance(origin, str) or _origin_of(origin, "Directory Service origin") != origin:
        raise DirectoryError("Directory Service origin is not a canonical HTTPS origin")
    _require_public_origin(origin)
    indexed_at = item.get("indexed_at")
    # A value `datetime.fromisoformat` happens to accept is not an RFC 3339 timestamp, and a caller
    # that compares or slices `indexed_at` needs the one shape.
    if not isinstance(indexed_at, str) or not _RFC_3339.match(indexed_at):
        raise DirectoryError("indexed_at must be an RFC 3339 date-time")
    document = _validate_as_service_document(item)
    # `protocols` is reinstated from the validated document only when something survived agent
    # filtering, so a block naming nothing this ODP version knows does not pass straight through.
    item.pop("protocols", None)
    if document.protocols is not None:
        item["protocols"] = document.protocols.model_dump(mode="json", exclude_defaults=True)
    item["operations"] = [operation.model_dump(mode="json") for operation in document.operations]
    return item


def _validate_as_service_document(item: dict[str, object]) -> ServiceDocument:
    """Holds the Service Document members a record echoes to the Service Document's own rules."""
    candidate: dict[str, object] = {
        "http": {"endpoint_base": "/"},
        "odp_version": "1.0",
        "operations": item.get("operations"),
    }
    for member in ("description", "keywords", "language", "localizations", "name", "protocols"):
        if member in item:
            candidate[member] = item[member]
    for member in ("documentation_url", "status_url", "support_url", "website_url"):
        value = item.get(member)
        if value not in (None, ""):
            candidate[member] = value
    return parse_agent_service_document(json.dumps(candidate, separators=(",", ":")))


def _require_public_origin(origin: str) -> None:
    """A public Directory has no business pointing an Agent at a loopback or private host.

    The default transport refuses these too, but that guarantee should not depend on which
    transport the consumer installed, nor on whether they enabled local development for a Service
    of their own.
    """
    host = urlsplit(origin).hostname or ""
    try:
        address = ip_address(host)
    except ValueError:
        return
    if not is_public(address):
        raise DirectoryError("Directory Service origin must not be a private or loopback host")


def _origin_of(value: str, subject: str) -> str:
    """Derives a Service Origin, reporting a reference that is not one as a Directory failure."""
    try:
        return derive_service_origin(value)
    except ReferenceError as error:
        raise DirectoryError(f"{subject} is not a usable origin: {error}") from error


def _validate_search_request(request: SearchRequest) -> None:
    # 0 means "the Directory decides"; anything else is a count, and a count below one asks for
    # nothing while still being sent on the wire.
    if request.limit < 0 or request.limit > 100:
        raise DirectoryError("limit must be from 1 through 100")
    if request.query.strip() != request.query or len(request.query) > 512:
        raise DirectoryError(
            "query must contain at most 512 characters without surrounding whitespace"
        )
    if request.filters is not None and (
        len(request.filters.keywords) > 32
        or any(not keyword or len(keyword) > 64 for keyword in request.filters.keywords)
    ):
        raise DirectoryError("keywords must contain at most 32 values of at most 64 characters")
    if (
        request.filters is not None
        and "trust" in request.filters.model_fields_set
        and (len(request.filters.trust) != 1 or request.filters.trust[0].name.value != "tap")
    ):
        raise DirectoryError("trust must contain exactly one tap descriptor")


def _consume_response(response: HttpResponse) -> HttpResponse:
    if not 200 <= response.status < 300:
        # The status is what describes the failure. Reading the size first turned a refused request
        # into a size complaint, and passing the whole body on made the error message as large as
        # the response the Directory sent.
        raise DirectoryRequestError(
            response.status,
            response.body.decode(errors="replace")[:_MAXIMUM_ERROR_CHARACTERS],
            response.headers,
        )
    if len(response.body) > _MAXIMUM_RESPONSE_BYTES:
        raise DirectoryError("Directory response exceeds 524288 bytes")
    content_type = response.headers.get("content-type", "").split(";", 1)[0].strip().lower()
    if content_type != "application/json":
        raise DirectoryError("Directory response must use application/json")
    return response


def _bounded(value: int, fallback: int, maximum: int, name: str) -> int:
    result = fallback if value == 0 else value
    if result < 1 or result > maximum:
        raise DirectoryError(f"{name} must be from 1 through {maximum}")
    return result
