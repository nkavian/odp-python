"""Canonical production and sandbox Directory client."""

from __future__ import annotations

import json
from urllib.parse import urlencode, urljoin

from pydantic import ValidationError as ModelValidationError

from offering_protocol.core import derive_service_origin
from offering_protocol.directory.models import (
    DirectoryService,
    Environment,
    IterationOptions,
    SearchPage,
    SearchRequest,
    SuggestionRequest,
)
from offering_protocol.directory.transport import (
    HttpRequest,
    HttpResponse,
    HttpxTransport,
    Transport,
)

_MAXIMUM_REDIRECTS = 5
_MAXIMUM_RESPONSE_BYTES = 524_288


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

    async def search(self, request: SearchRequest) -> SearchPage:
        _validate_search_request(request)
        response = await self._request(
            "POST",
            f"{self.environment.origin}/v1/services/search",
            json.dumps(request.to_dict(), separators=(",", ":")).encode(),
        )
        return _parse_search_page(response.body)

    async def continue_search(self, next_reference: str) -> SearchPage:
        target = urljoin(f"{self.environment.origin}/", next_reference)
        if derive_service_origin(target) != self.environment.origin:
            raise DirectoryError("Directory continuation changed canonical origin")
        response = await self._request("GET", target)
        return _parse_search_page(response.body)

    async def search_pages(
        self, request: SearchRequest, options: IterationOptions | None = None
    ) -> list[SearchPage]:
        options = options or IterationOptions()
        maximum_pages = _bounded(options.max_pages, 16, 16, "max_pages")
        pages: list[SearchPage] = []
        page = await self.search(request)
        for page_number in range(maximum_pages):
            pages.append(page)
            if not page.next:
                break
            if page_number + 1 < maximum_pages:
                page = await self.continue_search(page.next)
        return pages

    async def search_services(
        self, request: SearchRequest, options: IterationOptions | None = None
    ) -> list[DirectoryService]:
        options = options or IterationOptions()
        maximum_items = _bounded(options.max_items, 10_000, 10_000, "max_items")
        services: list[DirectoryService] = []
        for page in await self.search_pages(request, options):
            services.extend(page.items[: maximum_items - len(services)])
            if len(services) == maximum_items:
                break
        return services

    async def suggest(self, request: SuggestionRequest) -> list[str]:
        prefix = request.prefix.strip()
        if not prefix or len(prefix) > 128:
            raise DirectoryError("prefix must contain from 1 through 128 characters")
        if request.limit > 25:
            raise DirectoryError("limit must be from 1 through 25")
        query = {"prefix": prefix}
        if request.limit:
            query["limit"] = str(request.limit)
        response = await self._request(
            "GET", f"{self.environment.origin}/v1/services/suggestions?{urlencode(query)}"
        )
        try:
            suggestions = json.loads(response.body)
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
            if derive_service_origin(next_target) != derive_service_origin(target):
                raise DirectoryError("Directory redirect changed origin")
            if response.status == 303 or (response.status in {301, 302} and method == "POST"):
                method = "GET"
                body = b""
            target = next_target
        raise DirectoryError("Directory response exceeded its redirect limit")


def _parse_search_page(body: bytes) -> SearchPage:
    try:
        page = SearchPage.model_validate_json(body)
    except ModelValidationError as error:
        raise DirectoryError(f"invalid Directory response: {error}") from error
    if len(page.items) > 100:
        raise DirectoryError("Directory search page exceeds 100 Services")
    for service in page.items:
        if derive_service_origin(service.service_origin) != service.service_origin:
            raise DirectoryError("Directory Service origin is not canonical")
    return page


def _validate_search_request(request: SearchRequest) -> None:
    if request.limit > 100:
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


def _consume_response(response: HttpResponse) -> HttpResponse:
    if len(response.body) > _MAXIMUM_RESPONSE_BYTES:
        raise DirectoryError("Directory response exceeds 524288 bytes")
    if not 200 <= response.status < 300:
        raise DirectoryRequestError(
            response.status,
            response.body.decode(errors="replace"),
            response.headers,
        )
    content_type = response.headers.get("content-type", "").split(";", 1)[0].strip().lower()
    if content_type != "application/json":
        raise DirectoryError("Directory response must use application/json")
    return response


def _bounded(value: int, fallback: int, maximum: int, name: str) -> int:
    result = fallback if value == 0 else value
    if result < 1 or result > maximum:
        raise DirectoryError(f"{name} must be from 1 through {maximum}")
    return result
