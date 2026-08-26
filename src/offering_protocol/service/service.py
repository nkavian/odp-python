"""Framework-neutral ODP Service integration."""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Protocol, TypeVar, cast
from urllib.parse import parse_qsl

from offering_protocol.core import (
    VERSION,
    AuthenticationRequirement,
    Collection,
    CollectionSearchRequest,
    EnrollmentProtocol,
    HttpConfiguration,
    McpEndpoint,
    OdpValidationError,
    Offering,
    OfferingPage,
    OfferingSearchRequest,
    Operation,
    OperationDescriptor,
    Page,
    PaymentProtocol,
    ProblemDetails,
    Representation,
    SearchCapabilities,
    ServiceBranding,
    ServiceDocument,
    ServiceOpenApi,
    ServiceProtocols,
    is_local_resource_identifier,
    parse_collection,
    parse_collection_page,
    parse_collection_search_request,
    parse_offering,
    parse_offering_page,
    parse_offering_search_request,
    parse_service_document,
)

MEDIA_TYPE = "application/odp+json"
PROBLEM_MEDIA_TYPE = "application/problem+json"
_MAXIMUM_REQUEST_BYTES = 65_536
_MAXIMUM_RESOURCE_BYTES = 524_288
Validated = TypeVar("Validated")


@dataclass(frozen=True, slots=True)
class Request:
    method: str
    path: str
    body: bytes = b""
    headers: dict[str, str] = field(default_factory=dict)
    query: str = ""


@dataclass(frozen=True, slots=True)
class Response:
    status: int
    headers: dict[str, str]
    body: bytes


@dataclass(frozen=True, slots=True)
class CatalogRequest:
    accept_language: str | None = None
    cursor: str | None = None
    limit: int = 0
    path: str = ""
    representation: Representation = Representation.TERSE


class ServiceError(RuntimeError):
    """Base error for Service integration failures."""


class CatalogError(ServiceError):
    """Raised when a Catalog operation cannot be completed."""


class RequestError(ServiceError):
    def __init__(self, status: int, code: str, message: str) -> None:
        super().__init__(message)
        self.status = status
        self.code = code


class Catalog(Protocol):
    def operations(self) -> list[Operation]: ...

    async def list_offerings(self, request: CatalogRequest) -> OfferingPage[Offering]: ...

    async def get_offering(self, identifier: str, request: CatalogRequest) -> Offering | None: ...

    async def search_offerings(
        self, query: OfferingSearchRequest, request: CatalogRequest
    ) -> OfferingPage[Offering]: ...

    async def list_collections(self, request: CatalogRequest) -> Page[Collection]: ...

    async def get_collection(
        self, identifier: str, request: CatalogRequest
    ) -> Collection | None: ...

    async def search_collections(
        self, query: CollectionSearchRequest, request: CatalogRequest
    ) -> Page[Collection]: ...

    async def list_collection_offerings(
        self, collection_id: str, request: CatalogRequest
    ) -> OfferingPage[Offering]: ...


class ServiceBuilder:
    def __init__(self, name: str, description: str, language: str, endpoint_base: str) -> None:
        self._document = ServiceDocument(
            description=description,
            http=HttpConfiguration(endpoint_base=endpoint_base),
            language=language,
            localizations=[language],
            name=name,
            odp_version=VERSION,
            operations=[],
        )

    def branding(self, value: ServiceBranding) -> ServiceBuilder:
        return self._updated(branding=value)

    def documentation_url(self, value: str) -> ServiceBuilder:
        return self._updated(documentation_url=value)

    def keywords(self, values: list[str]) -> ServiceBuilder:
        return self._updated(keywords=values)

    def localizations(self, values: list[str]) -> ServiceBuilder:
        return self._updated(localizations=values)

    def mcp(self, values: list[McpEndpoint]) -> ServiceBuilder:
        return self._updated(mcp=values)

    def openapi(self, value: ServiceOpenApi) -> ServiceBuilder:
        return self._updated(http=self._document.http.model_copy(update={"openapi": value}))

    def operation_authentication(
        self, operation: Operation, authentication: AuthenticationRequirement
    ) -> ServiceBuilder:
        values = [item for item in self._document.operations if item.name is not operation]
        values.append(OperationDescriptor(authentication=authentication, name=operation))
        return self._updated(operations=values)

    def payment_origins(self, values: list[str]) -> ServiceBuilder:
        return self._updated(payment_origins=values)

    def protocols(
        self, enrollment: list[EnrollmentProtocol], payments: list[PaymentProtocol]
    ) -> ServiceBuilder:
        values: dict[str, object] = {}
        if enrollment:
            values["enrollment"] = enrollment
        if payments:
            values["payments"] = payments
        return self._updated(protocols=ServiceProtocols.model_validate(values))

    def search_capabilities(self, value: SearchCapabilities) -> ServiceBuilder:
        return self._updated(search_capabilities=value)

    def status_url(self, value: str) -> ServiceBuilder:
        return self._updated(status_url=value)

    def support_url(self, value: str) -> ServiceBuilder:
        return self._updated(support_url=value)

    def website_url(self, value: str) -> ServiceBuilder:
        return self._updated(website_url=value)

    def build(self, catalog: Catalog) -> Service:
        return Service(self._document, catalog)

    def _updated(self, **values: object) -> ServiceBuilder:
        self._document = self._document.model_copy(update=values)
        return self


class Service:
    def __init__(self, document: ServiceDocument, catalog: Catalog) -> None:
        operations = catalog.operations()
        if not {Operation.GET_OFFERING, Operation.LIST_OFFERINGS} <= set(operations):
            raise ServiceError("Catalog must support list-offerings and get-offering")
        authentication = {item.name: item.authentication for item in document.operations}
        descriptors = [
            OperationDescriptor(
                authentication=authentication.get(
                    operation, AuthenticationRequirement.NOT_REQUIRED
                ),
                name=operation,
            )
            for operation in operations
        ]
        candidate = document.model_copy(update={"odp_version": VERSION, "operations": descriptors})
        self._document = parse_service_document(_encode(candidate))
        self._catalog = catalog
        self._endpoint_base = self._document.http.endpoint_base.rstrip("/")

    @property
    def document(self) -> ServiceDocument:
        return self._document

    async def handle(self, request: Request) -> Response:
        try:
            return await self._handle(request)
        except RequestError as error:
            return _problem(error.status, error.code, str(error))
        except OdpValidationError as error:
            detail = "; ".join(f"{issue.path or '/'}: {issue.message}" for issue in error.issues)
            return _problem(400, "INVALID_REQUEST", detail)
        except ServiceError as error:
            return _problem(500, "INTERNAL_ERROR", str(error))

    async def _handle(self, request: Request) -> Response:
        headers = {name.lower(): value for name, value in request.headers.items()}
        if not _accepts_odp(headers.get("accept")):
            return _problem(406, "NOT_ACCEPTABLE", f"Accept must allow {MEDIA_TYPE}")
        method = request.method.upper()
        if request.path == "/.well-known/odp":
            if method != "GET":
                return _problem(405, "METHOD_NOT_ALLOWED", "The Service Document requires GET")
            return _json_response(200, self._document, _MAXIMUM_REQUEST_BYTES)
        if not request.path.startswith(self._endpoint_base):
            return _problem(404, "NOT_FOUND", "ODP resource not found")
        path = request.path[len(self._endpoint_base) :]
        operation = _path_operation(method, path)
        if operation is not None and operation not in {
            item.name for item in self._document.operations
        }:
            return _problem(404, "NOT_FOUND", "ODP operation is not supported")
        catalog_request = _catalog_request(request, headers)
        if (method, path) == ("GET", "/offerings"):
            offering_page = await self._catalog.list_offerings(catalog_request)
            return _json_response(
                200,
                _offering_page(offering_page, catalog_request.representation),
                _MAXIMUM_RESOURCE_BYTES,
            )
        if (method, path) == ("POST", "/offerings/search"):
            query = parse_offering_search_request(_search_body(request, headers))
            offering_page = await self._catalog.search_offerings(query, catalog_request)
            return _json_response(
                200,
                _offering_page(offering_page, catalog_request.representation),
                _MAXIMUM_RESOURCE_BYTES,
            )
        if (method, path) == ("GET", "/collections"):
            collection_page = await self._catalog.list_collections(catalog_request)
            return _json_response(
                200,
                _collection_page(collection_page, catalog_request.representation),
                _MAXIMUM_RESOURCE_BYTES,
            )
        if (method, path) == ("POST", "/collections/search"):
            collection_query = parse_collection_search_request(_search_body(request, headers))
            collection_page = await self._catalog.search_collections(
                collection_query, catalog_request
            )
            return _json_response(
                200,
                _collection_page(collection_page, catalog_request.representation),
                _MAXIMUM_RESOURCE_BYTES,
            )
        if method == "GET":
            return await self._get_path(path, catalog_request)
        return _problem(405, "METHOD_NOT_ALLOWED", "ODP operation uses a fixed HTTP method")

    async def _get_path(self, path: str, request: CatalogRequest) -> Response:
        if path.startswith("/offerings/"):
            identifier = path.removeprefix("/offerings/")
            if not is_local_resource_identifier(identifier):
                return _problem(400, "INVALID_REQUEST", "Offering identifier is invalid")
            offering = await self._catalog.get_offering(identifier, request)
            if offering is None:
                return _problem(404, "NOT_FOUND", "Offering not found")
            if offering.id != identifier:
                raise ServiceError("Offering identifier does not match request path")
            return _json_response(
                200,
                _offering(offering, request.representation),
                _MAXIMUM_RESOURCE_BYTES,
            )
        if path.startswith("/collections/"):
            value = path.removeprefix("/collections/")
            if value.endswith("/offerings"):
                identifier = value.removesuffix("/offerings")
                page = await self._catalog.list_collection_offerings(identifier, request)
                return _json_response(
                    200,
                    _offering_page(page, request.representation),
                    _MAXIMUM_RESOURCE_BYTES,
                )
            collection = await self._catalog.get_collection(value, request)
            if collection is None:
                return _problem(404, "NOT_FOUND", "Collection not found")
            if collection.id != value:
                raise ServiceError("Collection identifier does not match request path")
            return _json_response(
                200,
                _collection(collection, request.representation),
                _MAXIMUM_RESOURCE_BYTES,
            )
        return _problem(404, "NOT_FOUND", "ODP resource not found")


def _catalog_request(request: Request, headers: dict[str, str]) -> CatalogRequest:
    parameters = dict(parse_qsl(request.query, keep_blank_values=True))
    try:
        representation = Representation(parameters.get("representation", "terse"))
        limit = int(parameters.get("limit", "0"))
    except ValueError as error:
        raise RequestError(400, "INVALID_REQUEST", "query parameter is invalid") from error
    if not 0 <= limit <= 100:
        raise RequestError(400, "INVALID_REQUEST", "limit exceeds 100")
    return CatalogRequest(
        accept_language=headers.get("accept-language"),
        cursor=parameters.get("cursor"),
        limit=limit,
        path=request.path,
        representation=representation,
    )


def _search_body(request: Request, headers: dict[str, str]) -> bytes:
    if len(request.body) > _MAXIMUM_REQUEST_BYTES:
        raise RequestError(413, "REQUEST_TOO_LARGE", "request body is too large")
    content_type = headers.get("content-type", "").split(";", 1)[0]
    if content_type != MEDIA_TYPE:
        raise RequestError(415, "UNSUPPORTED_MEDIA_TYPE", f"Content-Type must be {MEDIA_TYPE}")
    return request.body


def _path_operation(method: str, path: str) -> Operation | None:
    if (method, path) == ("GET", "/offerings"):
        return Operation.LIST_OFFERINGS
    if (method, path) == ("POST", "/offerings/search"):
        return Operation.SEARCH_OFFERINGS
    if (method, path) == ("GET", "/collections"):
        return Operation.LIST_COLLECTIONS
    if (method, path) == ("POST", "/collections/search"):
        return Operation.SEARCH_COLLECTIONS
    if method == "GET" and path.startswith("/offerings/"):
        return Operation.GET_OFFERING
    if method == "GET" and path.startswith("/collections/") and path.endswith("/offerings"):
        return Operation.LIST_COLLECTION_OFFERINGS
    if method == "GET" and path.startswith("/collections/"):
        return Operation.GET_COLLECTION
    return None


def _json_response(status: int, value: object, maximum_bytes: int) -> Response:
    body = _encode(value)
    if len(body) > maximum_bytes:
        raise ServiceError("response body is too large")
    return Response(status, {"content-type": MEDIA_TYPE}, body)


def _offering(value: Offering, representation: Representation) -> Offering:
    parsed = _validated(parse_offering, value)
    _validate_offering_representation(parsed, representation)
    return parsed


def _collection(value: Collection, representation: Representation) -> Collection:
    parsed = _validated(parse_collection, value)
    _validate_collection_representation(parsed, representation)
    return parsed


def _offering_page(
    value: OfferingPage[Offering], representation: Representation
) -> OfferingPage[Offering]:
    parsed = _validated(parse_offering_page, value)
    for offering in parsed.items:
        _validate_offering_representation(offering, representation)
    return parsed


def _collection_page(value: Page[Collection], representation: Representation) -> Page[Collection]:
    parsed = _validated(parse_collection_page, value)
    for collection in parsed.items:
        _validate_collection_representation(collection, representation)
    return parsed


def _validate_offering_representation(offering: Offering, representation: Representation) -> None:
    if representation is Representation.TERSE and "actions" in offering.model_fields_set:
        raise ServiceError("Catalog returned Actions in a Terse Offering")
    if representation is Representation.FULL and "detail_fields" in offering.model_fields_set:
        raise ServiceError("Catalog returned detail_fields in a Full Offering")


def _validate_collection_representation(
    collection: Collection, representation: Representation
) -> None:
    if representation is Representation.FULL and "detail_fields" in collection.model_fields_set:
        raise ServiceError("Catalog returned detail_fields in a Full Collection")


def _validated(parser: Callable[[bytes | str], Validated], value: object) -> Validated:
    try:
        return parser(_encode(value))
    except ValueError as error:
        raise ServiceError(f"Catalog returned an invalid ODP response: {error}") from error


def _problem(status: int, code: str, detail: str) -> Response:
    value = ProblemDetails(
        code=code,
        detail=detail,
        status=status,
        title=detail,
        type=f"https://offeringprotocol.org/problems/{code.lower().replace('_', '-')}",
    )
    return Response(status, {"content-type": PROBLEM_MEDIA_TYPE}, _encode(value))


def _encode(value: object) -> bytes:
    if hasattr(value, "model_dump_json"):
        encoded = value.model_dump_json(by_alias=True, exclude_unset=True)
        return cast(str, encoded).encode()
    return json.dumps(value, separators=(",", ":")).encode()


def _accepts_odp(value: str | None) -> bool:
    if value is None:
        return True
    return any(
        item.split(";", 1)[0].strip().lower() in {"*/*", MEDIA_TYPE} for item in value.split(",")
    )
