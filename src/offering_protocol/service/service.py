"""Framework-neutral ODP Service integration."""

from __future__ import annotations

import base64
import hashlib
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
    TrustProtocol,
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
_MAXIMUM_DOCUMENT_BYTES = 65_536
_MAXIMUM_RESOURCE_BYTES = 524_288
#: Paths under the endpoint base that name an operation rather than a resource.
#:
#: Without this, `GET /offerings/search` reads as a request for an Offering called "search" and
#: answers 404, which tells an Agent the operation does not exist rather than that it uses POST.
_RESERVED_PATHS = {"/offerings/search": "POST", "/collections/search": "POST"}
#: RFC 9457: a title is a short summary of the problem *type* and does not change from one
#: occurrence to the next. The varying part of a failure belongs in `detail`.
_PROBLEM_TITLES = {
    "CONTINUATION_UNAVAILABLE": "Continuation unavailable",
    "INTERNAL_ERROR": "Internal error",
    "INVALID_REQUEST": "Invalid request",
    "METHOD_NOT_ALLOWED": "Method not allowed",
    "NOT_ACCEPTABLE": "Not acceptable",
    "NOT_FOUND": "Not found",
    "PRECONDITION_FAILED": "Precondition failed",
    "REQUEST_TOO_LARGE": "Request too large",
    "UNSUPPORTED_MEDIA_TYPE": "Unsupported media type",
}
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
    #: The language this Service selected for the response, by RFC 4647 Lookup over its
    #: localizations. A Catalog answers in this tag rather than parsing `accept_language` itself.
    language: str = ""
    limit: int = 0
    path: str = ""
    representation: Representation = Representation.TERSE


@dataclass(frozen=True, slots=True)
class _Exchange:
    """What building a response needs to know about the request it is answering."""

    headers: dict[str, str]
    language: str
    method: str


class ServiceError(RuntimeError):
    """Base error for Service integration failures."""


class CatalogError(ServiceError):
    """Raised when a Catalog operation cannot be completed."""


class RequestError(ServiceError):
    def __init__(
        self, status: int, code: str, message: str, headers: dict[str, str] | None = None
    ) -> None:
        super().__init__(message)
        self.status = status
        self.code = code
        self.headers = headers or {}


class Catalog(Protocol):
    def operations(self) -> list[Operation]: ...

    async def list_offerings(self, request: CatalogRequest) -> OfferingPage[Offering]: ...

    async def get_offering(self, identifier: str, request: CatalogRequest) -> Offering | None: ...

    async def search_offerings(
        self, query: OfferingSearchRequest, request: CatalogRequest
    ) -> OfferingPage[Offering]:
        del query, request
        raise CatalogError("Offering search is not supported")

    async def list_collections(self, request: CatalogRequest) -> Page[Collection]:
        del request
        raise CatalogError("Collection listing is not supported")

    async def get_collection(self, identifier: str, request: CatalogRequest) -> Collection | None:
        del identifier, request
        raise CatalogError("Collection retrieval is not supported")

    async def search_collections(
        self, query: CollectionSearchRequest, request: CatalogRequest
    ) -> Page[Collection]:
        del query, request
        raise CatalogError("Collection search is not supported")

    async def list_collection_offerings(
        self, collection_id: str, request: CatalogRequest
    ) -> OfferingPage[Offering]:
        del collection_id, request
        raise CatalogError("Collection Offering listing is not supported")


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
        self,
        enrollment: list[EnrollmentProtocol],
        payments: list[PaymentProtocol],
        trust: list[TrustProtocol] | None = None,
    ) -> ServiceBuilder:
        values: dict[str, object] = {}
        if enrollment:
            values["enrollment"] = enrollment
        if payments:
            values["payments"] = payments
        if trust:
            values["trust"] = trust
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
            return _problem(error.status, error.code, str(error), error.headers)
        except OdpValidationError as error:
            detail = "; ".join(f"{issue.path or '/'}: {issue.message}" for issue in error.issues)
            return _problem(400, "INVALID_REQUEST", detail)
        except ServiceError as error:
            return _problem(500, "INTERNAL_ERROR", str(error))

    async def _handle(self, request: Request) -> Response:
        headers = {name.lower(): value for name, value in request.headers.items()}
        _require_accept(headers.get("accept"))
        # RFC 9110 9.3.2: HEAD is GET without the body, so every resource answering GET answers
        # HEAD. Routing on the effective method keeps the two from drifting apart.
        method = request.method.upper()
        effective = "GET" if method == "HEAD" else method
        language = _select_language(
            headers.get("accept-language"),
            self._document.language,
            list(self._document.localizations),
        )
        exchange = _Exchange(headers=headers, language=self._document.language, method=method)
        if request.path == "/.well-known/odp":
            _require_method(effective, ("GET",))
            return _json_response(self._document, _MAXIMUM_DOCUMENT_BYTES, exchange)
        if not request.path.startswith(self._endpoint_base):
            raise RequestError(404, "NOT_FOUND", "ODP resource not found")
        path = request.path[len(self._endpoint_base) :]
        if path in _RESERVED_PATHS:
            _require_method(effective, (_RESERVED_PATHS[path],))
        operation = _path_operation(effective, path)
        if operation is not None and operation not in {
            item.name for item in self._document.operations
        }:
            raise RequestError(404, "NOT_FOUND", "ODP operation is not supported")
        catalog_request = _catalog_request(request, headers, language, operation)
        if (effective, path) == ("GET", "/offerings"):
            offering_page = await self._catalog.list_offerings(catalog_request)
            return _json_response(
                _offering_page(offering_page, catalog_request.representation),
                _MAXIMUM_RESOURCE_BYTES,
                exchange,
            )
        if (effective, path) == ("POST", "/offerings/search"):
            query = parse_offering_search_request(_search_body(request, headers))
            offering_page = await self._catalog.search_offerings(query, catalog_request)
            return _json_response(
                _offering_page(offering_page, catalog_request.representation),
                _MAXIMUM_RESOURCE_BYTES,
                exchange,
            )
        if (effective, path) == ("GET", "/collections"):
            collection_page = await self._catalog.list_collections(catalog_request)
            return _json_response(
                _collection_page(collection_page, catalog_request.representation),
                _MAXIMUM_RESOURCE_BYTES,
                exchange,
            )
        if (effective, path) == ("POST", "/collections/search"):
            collection_query = parse_collection_search_request(_search_body(request, headers))
            collection_page = await self._catalog.search_collections(
                collection_query, catalog_request
            )
            return _json_response(
                _collection_page(collection_page, catalog_request.representation),
                _MAXIMUM_RESOURCE_BYTES,
                exchange,
            )
        if effective == "GET":
            return await self._get_path(path, catalog_request, exchange)
        raise RequestError(
            405, "METHOD_NOT_ALLOWED", "ODP operation uses a fixed HTTP method", _allow(("GET",))
        )

    async def _get_path(self, path: str, request: CatalogRequest, exchange: _Exchange) -> Response:
        if path.startswith("/offerings/"):
            identifier = _require_identifier(path.removeprefix("/offerings/"), "Offering")
            offering = await self._catalog.get_offering(identifier, request)
            if offering is None:
                raise RequestError(404, "NOT_FOUND", "Offering not found")
            if offering.id != identifier:
                raise ServiceError("Offering identifier does not match request path")
            return _json_response(
                _offering(offering, request.representation), _MAXIMUM_RESOURCE_BYTES, exchange
            )
        if path.startswith("/collections/"):
            value = path.removeprefix("/collections/")
            if value.endswith("/offerings"):
                # SVC-66 substitutes an identifier verbatim, so a path segment that is not a Local
                # Resource Identifier names no resource this Service could ever hold -- and it is
                # the Catalog that would otherwise have to decide what to do with it.
                identifier = _require_identifier(value.removesuffix("/offerings"), "Collection")
                page = await self._catalog.list_collection_offerings(identifier, request)
                return _json_response(
                    _offering_page(page, request.representation),
                    _MAXIMUM_RESOURCE_BYTES,
                    exchange,
                )
            identifier = _require_identifier(value, "Collection")
            collection = await self._catalog.get_collection(identifier, request)
            if collection is None:
                raise RequestError(404, "NOT_FOUND", "Collection not found")
            if collection.id != identifier:
                raise ServiceError("Collection identifier does not match request path")
            return _json_response(
                _collection(collection, request.representation), _MAXIMUM_RESOURCE_BYTES, exchange
            )
        raise RequestError(404, "NOT_FOUND", "ODP resource not found")


def _catalog_request(
    request: Request, headers: dict[str, str], language: str, operation: Operation | None
) -> CatalogRequest:
    parameters = parse_qsl(request.query, keep_blank_values=True)
    # SVC-73: a repeated `representation` is rejected rather than resolved. Collapsing repeats into
    # a dict silently honoured whichever copy came last, so `representation=terse&representation=
    # full` served a Full Representation to a request that also asked for a Terse one.
    values: dict[str, str] = {}
    for name, value in parameters:
        if name in {"cursor", "limit", "representation"} and name in values:
            raise RequestError(400, "INVALID_REQUEST", f"{name} must not be repeated")
        values[name] = value
    try:
        default = (
            "full" if operation in {Operation.GET_COLLECTION, Operation.GET_OFFERING} else "terse"
        )
        representation = Representation(values.get("representation", default))
        limit = int(values.get("limit", "0"))
    except ValueError as error:
        raise RequestError(400, "INVALID_REQUEST", "query parameter is invalid") from error
    if not 0 <= limit <= 100:
        raise RequestError(400, "INVALID_REQUEST", "limit exceeds 100")
    return CatalogRequest(
        accept_language=headers.get("accept-language"),
        cursor=values.get("cursor"),
        language=language,
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


def _json_response(value: object, maximum_bytes: int, exchange: _Exchange) -> Response:
    """Serializes once, so the body can be measured, given a validator, and conditionally answered.

    SVC-60 and SVC-61 apply to every representation this Service serves, not only to the localized
    ones: `Vary` is what stops a shared cache handing an English body to a French request, and the
    entity tag is what lets an Agent revalidate instead of re-transferring (PAG-31).
    """
    body = _encode(value)
    if len(body) > maximum_bytes:
        raise ServiceError("response body is too large")
    if isinstance(value, Page):
        language = (
            ", ".join(
                dict.fromkeys(
                    getattr(item, "language", "") or exchange.language for item in value.items
                )
            )
            or exchange.language
        )
    else:
        language = getattr(value, "language", "") or exchange.language
    etag = _entity_tag(language, body)
    headers = {
        "content-language": language,
        "content-type": MEDIA_TYPE,
        "etag": etag,
        "vary": "Accept, Accept-Language",
    }
    if not _matches_entity_tag(exchange.headers.get("if-none-match"), etag):
        return Response(200, headers, b"" if exchange.method == "HEAD" else body)
    # RFC 9110 13.1.2: a matched `If-None-Match` is 304 for GET and HEAD, 412 for anything else.
    if exchange.method in {"GET", "HEAD"}:
        return Response(304, {name: headers[name] for name in ("etag", "vary")}, b"")
    raise RequestError(
        412, "PRECONDITION_FAILED", "If-None-Match matched the current representation"
    )


def _require_method(method: str, allowed: tuple[str, ...]) -> None:
    if method not in allowed:
        raise RequestError(
            405,
            "METHOD_NOT_ALLOWED",
            f"ODP operation requires {' or '.join(allowed)}",
            _allow(allowed),
        )


def _allow(allowed: tuple[str, ...]) -> dict[str, str]:
    """RFC 9110 15.5.6 requires a 405 to name every method the resource supports.

    Every ODP operation that answers GET also answers HEAD, so HEAD travels with it.
    """
    methods = [*allowed, "HEAD"] if "GET" in allowed else list(allowed)
    return {"allow": ", ".join(methods)}


def _require_identifier(value: str, label: str) -> str:
    if not is_local_resource_identifier(value):
        raise RequestError(400, "INVALID_REQUEST", f"{label} identifier is invalid")
    return value


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


def _problem(
    status: int, code: str, detail: str, headers: dict[str, str] | None = None
) -> Response:
    value = ProblemDetails(
        code=code,
        detail=detail,
        status=status,
        title=_PROBLEM_TITLES.get(code, code.replace("_", " ").capitalize()),
        type=f"https://offeringprotocol.org/problems/{code.lower().replace('_', '-')}",
    )
    return Response(status, {"content-type": PROBLEM_MEDIA_TYPE, **(headers or {})}, _encode(value))


def _encode(value: object) -> bytes:
    if isinstance(value, Page):
        document = value.model_dump(mode="json", by_alias=True, exclude_unset=True)
        for item in document["items"]:
            item.pop("odp_version", None)
        return json.dumps(document, separators=(",", ":"), ensure_ascii=False).encode()
    if hasattr(value, "model_dump_json"):
        encoded = value.model_dump_json(by_alias=True, exclude_unset=True)
        return cast(str, encoded).encode()
    return json.dumps(value, separators=(",", ":")).encode()


def _require_accept(value: str | None) -> None:
    """MED-03/MED-04: an `Accept` that excludes ODP is a refusal, an absent one is not.

    `application/*` is a wildcard media range that covers this media type, and RFC 9110 12.4.2
    makes `q=0` an explicit statement that a range is *not* acceptable -- so an `Accept` naming ODP
    with zero weight excludes it just as surely as one that never mentions it.
    """
    if value is None:
        return
    specificity = -1
    quality = 0.0
    for entry in value.split(","):
        media_type = entry.split(";", 1)[0].strip().lower()
        if media_type not in {"*/*", "application/*", MEDIA_TYPE}:
            continue
        precision = {"*/*": 0, "application/*": 1, MEDIA_TYPE: 2}[media_type]
        weight = _quality_of(entry)
        if precision > specificity:
            specificity, quality = precision, weight
        elif precision == specificity:
            quality = max(quality, weight)
    if quality > 0:
        return
    raise RequestError(406, "NOT_ACCEPTABLE", f"Accept must allow {MEDIA_TYPE}")


def _quality_of(entry: str) -> float:
    """The weight of one `Accept` or `Accept-Language` entry, or 0 when it carries no usable one."""
    for parameter in entry.split(";")[1:]:
        name, separator, value = parameter.partition("=")
        if name.strip().lower() != "q" or not separator:
            continue
        try:
            quality = float(value.strip())
        except ValueError:
            return 0.0
        return quality if 0 <= quality <= 1 else 0.0
    return 1.0


def _select_language(value: str | None, fallback: str, localizations: list[str]) -> str:
    """SVC-58/59: RFC 4647 Lookup over the localizations this Service advertises.

    Returning the default when nothing matches is the point of SVC-59: a Service never refuses a
    request over language, so the worst outcome of an unmatched range is the representation the
    Service would have served anyway.
    """
    if value is None:
        return fallback
    entries = [
        (entry.split(";", 1)[0].strip().lower(), _quality_of(entry)) for entry in value.split(",")
    ]
    entries = [(range_, quality) for range_, quality in entries if range_]
    # RFC 9110 12.4.2: a range weighted zero is unacceptable. It carves tags out of the `*`
    # residual below rather than competing for a match of its own.
    refused = [range_ for range_, quality in entries if quality == 0 and range_ != "*"]
    wanted = sorted(
        ((range_, quality) for range_, quality in entries if quality > 0 and range_ != "*"),
        key=lambda item: item[1],
        reverse=True,
    )
    for range_, _ in wanted:
        found = _lookup(range_, localizations)
        if found is not None:
            return found
    # RFC 9110 12.5.4: `*` matches every tag no other range in the field matched, so it is the
    # residual and can never outrank a range the caller named.
    if not any(range_ == "*" and quality > 0 for range_, quality in entries):
        return fallback
    for tag in (fallback, *localizations):
        if not any(_covers(range_, tag.lower()) for range_ in refused):
            return tag
    return fallback


def _lookup(range_: str, localizations: list[str]) -> str | None:
    """RFC 4647 Lookup: truncate the range at its subtag boundaries until a tag matches exactly."""
    available = [(tag.lower(), tag) for tag in localizations]
    candidate = range_
    while True:
        for lowered, tag in available:
            if lowered == candidate:
                return tag
        cut = candidate.rfind("-")
        if cut < 0:
            return None
        candidate = candidate[:cut]
        # A single-character subtag is an extension or private-use singleton; drop it with its
        # parent rather than leaving a range that names a singleton and nothing else.
        singleton = candidate.rfind("-")
        if singleton >= 0 and len(candidate) - singleton == 2:
            candidate = candidate[:singleton]


def _covers(range_: str, tag: str) -> bool:
    return tag == range_ or tag.startswith(f"{range_}-")


def _entity_tag(language: str, body: bytes) -> str:
    """SVC-61: a strong validator over the negotiated language and the exact bytes served.

    Hashing the body alone would give two language variants of an unlocalized body one validator,
    which is the one thing an entity tag must not do.
    """
    digest = hashlib.sha256(language.encode() + b"\x00" + body).digest()
    return '"' + base64.urlsafe_b64encode(digest).decode().rstrip("=")[:27] + '"'


def _matches_entity_tag(value: str | None, etag: str) -> bool:
    """RFC 9110 compares `If-None-Match` weakly, so `W/"x"` matches the strong `"x"` served here."""
    if value is None:
        return False
    for candidate in value.split(","):
        candidate = candidate.strip()
        if candidate.startswith("W/"):
            candidate = candidate[2:]
        if candidate in {"*", etag}:
            return True
    return False
