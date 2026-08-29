"""Normative schema-backed ODP parsing and validation."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from functools import lru_cache
from importlib.resources import files
from typing import Any, TypeVar

from jsonschema import FormatChecker
from jsonschema.exceptions import ValidationError as JsonSchemaValidationError
from jsonschema.validators import validator_for
from pydantic import BaseModel
from pydantic import ValidationError as ModelValidationError
from referencing import Registry, Resource

from offering_protocol.core.models import (
    Collection,
    CollectionSearchRequest,
    FilterDefinition,
    FilterOperator,
    FilterType,
    Offering,
    OfferingPage,
    OfferingSearchRequest,
    Operation,
    Page,
    ProblemDetails,
    ResourceIdentity,
    ServiceDocument,
    SortDefinition,
)


@dataclass(frozen=True, slots=True)
class ValidationIssue:
    path: str
    keyword: str
    message: str
    params: dict[str, object] = field(default_factory=dict)


class OdpValidationError(ValueError):
    def __init__(self, document_type: str, issues: list[ValidationIssue]) -> None:
        super().__init__(f"invalid ODP {document_type}")
        self.document_type = document_type
        self.issues = tuple(issues)


Model = TypeVar("Model", bound=BaseModel)


def parse_service_document(data: bytes | str) -> ServiceDocument:
    value = _parse(data, "service-document.schema.json", "Service Document", ServiceDocument)
    issues: list[ValidationIssue] = []
    if "id" in value.additional:
        issues.append(_issue("/id", "prohibited", "must not appear in a Service Document"))
    if "web_url" in value.additional:
        issues.append(_issue("/web_url", "prohibited", "must not appear in a Service Document"))
    _validate_localizations(value.language, value.localizations, True, issues)
    if sum(len(keyword) for keyword in value.keywords) > 1024:
        issues.append(
            _issue(
                "/keywords",
                "max-code-points",
                "must contain no more than 1024 code points in total",
            )
        )
    if value.search_capabilities is not None and not any(
        operation.name is Operation.SEARCH_OFFERINGS for operation in value.operations
    ):
        issues.append(
            _issue(
                "/search_capabilities",
                "operation-support",
                "requires the search-offerings operation",
            )
        )
    _raise_refinement("Service Document", issues)
    return value


def parse_agent_service_document(data: bytes | str) -> ServiceDocument:
    try:
        raw = json.loads(data)
    except (UnicodeDecodeError, json.JSONDecodeError):
        return parse_service_document(data)
    if isinstance(raw, dict):
        raw = _normalize_agent_response(raw, "service-document")
    return parse_service_document(json.dumps(raw, separators=(",", ":")))


def _normalize_agent_response(value: dict[str, Any], kind: str) -> dict[str, Any]:
    normalized = dict(value)
    if kind == "service-document":
        _filter_agent_protocols(normalized)
        protocols = normalized.get("protocols")
        if isinstance(protocols, dict):
            _filter_unknown_authentication(protocols, "payments")
        _filter_named_list(
            normalized,
            "operations",
            {
                "get-collection",
                "get-offering",
                "list-collection-offerings",
                "list-collections",
                "list-offerings",
                "search-collections",
                "search-offerings",
            },
        )
        _filter_unknown_authentication(normalized, "operations")
        _filter_typed_list(normalized, "mcp", {"streamable-http"})
        _filter_closed_object_list(normalized, "operations", {"authentication", "name"})
        _filter_closed_object_list(normalized, "mcp", {"description", "name", "type", "url"})
        _filter_payment_options(normalized)
        _normalize_branding(normalized)
        _normalize_search_capabilities(normalized)
    elif kind in {"collection", "offering"}:
        _filter_typed_list(
            normalized,
            "images",
            {"image/avif", "image/jpeg", "image/png", "image/svg+xml", "image/webp"},
        )
        _strip_object_list(normalized, "images", {"alt", "height", "src", "type", "width"})
        _normalize_search_capabilities(normalized)
        if kind == "offering":
            _normalize_offering(normalized)
    elif kind in {"collection-page", "offering-page"} and isinstance(normalized.get("items"), list):
        item_kind = "offering" if kind == "offering-page" else "collection"
        normalized["items"] = [
            _normalize_agent_response(item, item_kind) if isinstance(item, dict) else item
            for item in normalized["items"]
        ]
    elif kind in {"filter-page", "sort-page"} and isinstance(normalized.get("items"), list):
        predicate = _known_filter if kind == "filter-page" else _known_sort
        normalized["items"] = [item for item in normalized["items"] if predicate(item)]
    elif kind == "problem" and isinstance(normalized.get("invalid_params"), list):
        normalized["invalid_params"] = [
            item
            for item in normalized["invalid_params"]
            if not (
                isinstance(item, dict)
                and isinstance(item.get("in"), str)
                and item["in"] not in {"body", "header", "path", "query"}
            )
        ]
    return normalized


def _filter_named_list(value: dict[str, Any], member: str, recognized: set[str]) -> None:
    items = value.get(member)
    if not isinstance(items, list):
        return
    filtered = [
        item
        for item in items
        if not (
            isinstance(item, dict)
            and isinstance(item.get("name"), str)
            and item["name"] not in recognized
        )
    ]
    if filtered:
        value[member] = filtered
    else:
        value.pop(member, None)


def _filter_typed_list(value: dict[str, Any], member: str, recognized: set[str]) -> None:
    items = value.get(member)
    if not isinstance(items, list):
        return
    filtered = [
        item
        for item in items
        if not (
            isinstance(item, dict)
            and isinstance(item.get("type"), str)
            and item["type"] not in recognized
        )
    ]
    if filtered:
        value[member] = filtered
    else:
        value.pop(member, None)


def _filter_closed_object_list(value: dict[str, Any], member: str, allowed: set[str]) -> None:
    items = value.get(member)
    if not isinstance(items, list):
        return
    filtered = [
        item for item in items if not isinstance(item, dict) or not set(item).difference(allowed)
    ]
    if filtered:
        value[member] = filtered
    else:
        value.pop(member, None)


def _filter_unknown_authentication(value: dict[str, Any], member: str) -> None:
    items = value.get(member)
    if not isinstance(items, list):
        return
    filtered = [
        item
        for item in items
        if not isinstance(item, dict) or not _has_unknown_authentication(item)
    ]
    if filtered:
        value[member] = filtered
    else:
        value.pop(member, None)


def _has_unknown_authentication(value: dict[str, Any]) -> bool:
    authentication = value.get("authentication")
    return isinstance(authentication, str) and authentication not in {
        "not-required",
        "optional",
        "required",
    }


def _strip_object_list(value: dict[str, Any], member: str, allowed: set[str]) -> None:
    items = value.get(member)
    if not isinstance(items, list):
        return
    value[member] = [
        {key: entry for key, entry in item.items() if key in allowed}
        if isinstance(item, dict)
        else item
        for item in items
    ]


def _filter_payment_options(value: dict[str, Any]) -> None:
    protocols = value.get("protocols")
    if not isinstance(protocols, dict) or not isinstance(protocols.get("payments"), list):
        return
    recognized = {
        "algorand",
        "aptos",
        "arbitrum",
        "avalanche",
        "base",
        "card",
        "ethereum",
        "hedera",
        "inflow",
        "lightning",
        "polygon",
        "solana",
        "stellar",
        "stripe",
        "tempo",
        "ton",
    }
    for payment in protocols["payments"]:
        if not isinstance(payment, dict) or not isinstance(payment.get("options"), list):
            continue
        options = [
            option
            for option in payment["options"]
            if not isinstance(option, str) or option in recognized
        ]
        if options:
            payment["options"] = options
        else:
            payment.pop("options", None)


def _normalize_branding(value: dict[str, Any]) -> None:
    branding = value.get("branding")
    if not isinstance(branding, dict):
        return
    normalized = {key: entry for key, entry in branding.items() if key in {"icon", "logo"}}
    for member in ("icon", "logo"):
        image = normalized.get(member)
        if (
            isinstance(image, dict)
            and isinstance(image.get("type"), str)
            and image["type"] not in {"image/png", "image/svg+xml", "image/webp"}
        ):
            normalized.pop(member, None)
        elif isinstance(image, dict):
            normalized[member] = {
                key: entry for key, entry in image.items() if key in {"src", "type"}
            }
    if normalized:
        value["branding"] = normalized
    else:
        value.pop("branding", None)


def _normalize_search_capabilities(value: dict[str, Any]) -> None:
    capabilities = value.get("search_capabilities")
    if not isinstance(capabilities, dict):
        return
    normalized = dict(capabilities)
    for member, predicate in (("filters", _known_filter), ("sorts", _known_sort)):
        source = normalized.get(member)
        if not isinstance(source, dict) or not isinstance(source.get("inline"), list):
            continue
        items = [item for item in source["inline"] if predicate(item)]
        if items:
            normalized[member] = {**source, "inline": items}
        else:
            normalized.pop(member, None)
    if normalized:
        value["search_capabilities"] = normalized
    else:
        value.pop("search_capabilities", None)


def _normalize_offering(value: dict[str, Any]) -> None:
    schema = value.get("schema")
    if isinstance(schema, dict) and set(schema).difference({"url"}):
        value.pop("schema", None)
    price = value.get("price")
    if (
        isinstance(price, dict)
        and isinstance(price.get("type"), str)
        and price["type"] not in {"fixed", "free", "metered", "quote", "range", "starting_at"}
    ):
        value.pop("price", None)
    actions = value.get("actions")
    if not isinstance(actions, list):
        return
    filtered = []
    for action in actions:
        if isinstance(action, dict) and _has_unknown_authentication(action):
            continue
        if isinstance(action, dict) and set(action).difference(
            {"authentication", "description", "http", "id", "openapi", "rel"}
        ):
            continue
        http = action.get("http") if isinstance(action, dict) else None
        if isinstance(http, dict) and set(http).difference(
            {"href", "method", "request", "response_content_types"}
        ):
            continue
        request = http.get("request") if isinstance(http, dict) else None
        if isinstance(request, dict) and set(request).difference({"content_type", "schema"}):
            continue
        schema = request.get("schema") if isinstance(request, dict) else None
        if isinstance(schema, dict) and set(schema).difference({"url"}):
            continue
        openapi = action.get("openapi") if isinstance(action, dict) else None
        if isinstance(openapi, dict) and set(openapi).difference({"operation_id", "url"}):
            continue
        method = http.get("method") if isinstance(http, dict) else None
        if isinstance(method, str) and method not in {"GET", "POST"}:
            continue
        filtered.append(action)
    if filtered:
        value["actions"] = filtered
    else:
        value.pop("actions", None)


def _known_filter(value: Any) -> bool:
    if not isinstance(value, dict):
        return True
    if isinstance(value.get("type"), str) and value["type"] not in {
        "boolean",
        "date",
        "date-time",
        "decimal",
        "integer",
        "number",
        "string",
    }:
        return False
    operators = value.get("operators")
    if isinstance(operators, list) and any(
        isinstance(operator, str)
        and operator not in {"eq", "exists", "gt", "gte", "in", "lt", "lte"}
        for operator in operators
    ):
        return False
    unit = value.get("unit")
    return not (
        isinstance(unit, dict)
        and isinstance(unit.get("system"), str)
        and unit["system"] not in {"service", "ucum"}
    )


def _known_sort(value: Any) -> bool:
    if not isinstance(value, dict) or not isinstance(value.get("keys"), list):
        return True
    return not any(
        isinstance(key, dict)
        and (
            (
                isinstance(key.get("direction"), str)
                and key["direction"] not in {"ascending", "descending"}
            )
            or (isinstance(key.get("missing"), str) and key["missing"] not in {"first", "last"})
        )
        for key in value["keys"]
    )


def _filter_agent_protocols(document: dict[str, Any]) -> None:
    protocols = document.get("protocols")
    if not isinstance(protocols, dict):
        return
    _filter_agent_protocol_category(protocols, "enrollment", {"aep"})
    _filter_agent_protocol_category(protocols, "payments", {"mpp", "x402"})
    _filter_agent_protocol_category(protocols, "trust", {"tap"})
    if not protocols:
        document.pop("protocols")


def _filter_agent_protocol_category(
    protocols: dict[str, Any], category: str, recognized: set[str]
) -> None:
    descriptors = protocols.get(category)
    if not isinstance(descriptors, list):
        return
    filtered = [
        descriptor
        for descriptor in descriptors
        if not (
            isinstance(descriptor, dict)
            and isinstance(descriptor.get("name"), str)
            and descriptor["name"] not in recognized
        )
    ]
    if descriptors and not filtered:
        protocols.pop(category)
    else:
        protocols[category] = filtered


def parse_collection(data: bytes | str) -> Collection:
    value = _parse(data, "collection.schema.json", "Collection", Collection)
    _validate_representation(
        value.language, value.localizations, [image.src for image in value.images]
    )
    return value


def parse_offering(data: bytes | str) -> Offering:
    value = _parse(data, "offering.schema.json", "Offering", Offering)
    _validate_representation(
        value.language, value.localizations, [image.src for image in value.images]
    )
    return value


def parse_problem_details(data: bytes | str) -> ProblemDetails:
    return _parse(data, "problem-details.schema.json", "Problem Details", ProblemDetails)


def parse_problem_response(data: bytes | str, http_status: int) -> ProblemDetails:
    value = parse_problem_details(data)
    if value.status != http_status:
        raise OdpValidationError(
            "Problem Details",
            [_issue("/status", "http-status", "must match the HTTP response status")],
        )
    return value


def parse_resource_identity(data: bytes | str) -> ResourceIdentity:
    return _parse(data, "resource-identity.schema.json", "resource identity", ResourceIdentity)


def parse_collection_page(data: bytes | str) -> Page[Collection]:
    value = _parse(data, "page-envelope.schema.json", "Collection page", Page[Collection])
    for item in value.items:
        parse_collection(_embedded_json(item, value.odp_version))
    return value


def parse_offering_page(data: bytes | str) -> OfferingPage[Offering]:
    value = _parse(
        data,
        "offering-search-response.schema.json",
        "Offering page",
        OfferingPage[Offering],
    )
    for item in value.items:
        parse_offering(_embedded_json(item, value.odp_version))
    return value


def parse_collection_search_request(data: bytes | str) -> CollectionSearchRequest:
    return _parse(
        data,
        "collection-search-request.schema.json",
        "Collection search request",
        CollectionSearchRequest,
    )


def parse_offering_search_request(data: bytes | str) -> OfferingSearchRequest:
    return _parse(
        data,
        "offering-search-request.schema.json",
        "Offering search request",
        OfferingSearchRequest,
    )


def parse_filter_definition(data: bytes | str) -> FilterDefinition:
    value = _parse(data, "filter-definition.schema.json", "Filter Definition", FilterDefinition)
    ordered = {
        FilterOperator.GREATER_THAN,
        FilterOperator.GREATER_THAN_OR_EQUAL,
        FilterOperator.LESS_THAN,
        FilterOperator.LESS_THAN_OR_EQUAL,
    }
    issues: list[ValidationIssue] = []
    if value.filter_type in {FilterType.STRING, FilterType.BOOLEAN} and any(
        operator in ordered for operator in value.operators
    ):
        issues.append(
            _issue(
                "/operators",
                "operator-type",
                "contains an operator incompatible with the Filter type",
            )
        )
    if value.filter_type is FilterType.BOOLEAN and value.unit is not None:
        issues.append(_issue("/unit", "unit-type", "must not appear on a boolean Filter"))
    _raise_refinement("Filter Definition", issues)
    return value


def parse_sort_definition(data: bytes | str) -> SortDefinition:
    return _parse(data, "sort-definition.schema.json", "Sort Definition", SortDefinition)


def parse_filter_definition_page(data: bytes | str) -> Page[FilterDefinition]:
    value = _parse(
        data,
        "filter-definition-page.schema.json",
        "Filter Definition page",
        Page[FilterDefinition],
    )
    for item in value.items:
        parse_filter_definition(_model_json(item))
    return value


def parse_sort_definition_page(data: bytes | str) -> Page[SortDefinition]:
    return _parse(
        data,
        "sort-definition-page.schema.json",
        "Sort Definition page",
        Page[SortDefinition],
    )


def validate_value(value: object, schema_name: str, document_type: str) -> None:
    validator = _validators().get(schema_name)
    if validator is None:
        raise RuntimeError(f"missing bundled schema {schema_name}")
    issues = [_schema_issue(error) for error in validator.iter_errors(value)]
    if issues:
        issues.sort(key=lambda issue: (issue.path, issue.keyword, issue.message))
        raise OdpValidationError(document_type, issues)


def _parse(data: bytes | str, schema_name: str, document_type: str, model: type[Model]) -> Model:
    try:
        raw = json.loads(data)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise OdpValidationError(document_type, [_issue("", "json", str(error))]) from error
    validate_value(raw, schema_name, document_type)
    try:
        return model.model_validate(raw)
    except ModelValidationError as error:
        issues = [
            ValidationIssue(
                path="/" + "/".join(str(part) for part in item["loc"]),
                keyword=str(item["type"]),
                message=str(item["msg"]),
            )
            for item in error.errors()
        ]
        raise OdpValidationError(document_type, issues) from error


def _model_json(value: BaseModel) -> str:
    return value.model_dump_json(by_alias=True, exclude_unset=True)


def _embedded_json(value: BaseModel, inherited_version: str) -> str:
    document = value.model_dump(mode="json", by_alias=True, exclude_unset=True)
    document.setdefault("odp_version", inherited_version)
    return json.dumps(document, separators=(",", ":"))


@lru_cache(maxsize=1)
def _validators() -> dict[str, Any]:
    schema_directory = files("offering_protocol.core").joinpath("schemas")
    documents: dict[str, dict[str, object]] = {}
    resources: list[tuple[str, Resource[dict[str, object]]]] = []
    for entry in schema_directory.iterdir():
        if entry.name.endswith(".schema.json"):  # pragma: no branch
            document = json.loads(entry.read_text(encoding="utf-8"))
            documents[entry.name] = document
            identifier = document.get("$id", f"https://offeringprotocol.org/schemas/{entry.name}")
            resources.append((str(identifier), Resource.from_contents(document)))
    registry = Registry().with_resources(resources)
    validators: dict[str, Any] = {}
    for name, document in documents.items():
        validator_type = validator_for(document)
        validator_type.check_schema(document)
        validators[name] = validator_type(
            document,
            registry=registry,
            format_checker=FormatChecker(),
        )
    return validators


def _schema_issue(error: JsonSchemaValidationError) -> ValidationIssue:
    path = "".join(f"/{str(part).replace('~', '~0').replace('/', '~1')}" for part in error.path)
    keyword = str(error.schema_path[-1]) if error.schema_path else "schema"
    return ValidationIssue(path=path, keyword=keyword, message=error.message)


def _validate_representation(language: str, localizations: list[str], images: list[str]) -> None:
    issues: list[ValidationIssue] = []
    _validate_localizations(language, localizations, False, issues)
    if len(images) != len(set(images)):
        issues.append(_issue("/images", "unique-image-source", "must contain unique image sources"))
    _raise_refinement("representation", issues)


def _validate_localizations(
    language: str, localizations: list[str], require_default: bool, issues: list[ValidationIssue]
) -> None:
    if language and not _is_language_tag(language):
        issues.append(_issue("/language", "language-tag", "must be a language tag"))
    if any(not _is_language_tag(tag) for tag in localizations):
        issues.append(
            _issue(
                "/localizations",
                "language-tag",
                "must contain only language tags",
            )
        )
    folded = [tag.casefold() for tag in localizations]
    if len(folded) != len(set(folded)):
        issues.append(
            _issue(
                "/localizations",
                "unique-language-tag",
                "must be unique without regard to case",
            )
        )
    if (require_default or (language and localizations)) and language.casefold() not in folded:
        issues.append(
            _issue(
                "/localizations",
                "contains-default-language" if require_default else "contains-language",
                "must contain the default language"
                if require_default
                else "must contain the representation language",
            )
        )


_ALPHANUMERIC = re.compile(r"^[A-Za-z0-9]+$")
_GRANDFATHERED_LANGUAGE_TAGS = {
    "art-lojban",
    "cel-gaulish",
    "en-gb-oed",
    "i-ami",
    "i-bnn",
    "i-default",
    "i-enochian",
    "i-hak",
    "i-klingon",
    "i-lux",
    "i-mingo",
    "i-navajo",
    "i-pwn",
    "i-tao",
    "i-tay",
    "i-tsu",
    "no-bok",
    "no-nyn",
    "sgn-be-fr",
    "sgn-be-nl",
    "sgn-ch-de",
    "zh-guoyu",
    "zh-hakka",
    "zh-min",
    "zh-min-nan",
    "zh-xiang",
}


def _is_language_tag(value: str) -> bool:
    if not value or len(value) > 255 or not _ALPHANUMERIC.match(value.replace("-", "")):
        return False
    subtags = value.lower().split("-")
    if value.lower() in _GRANDFATHERED_LANGUAGE_TAGS:
        return True
    if any(not subtag or len(subtag) > 8 for subtag in subtags):
        return False
    if subtags[0] == "x":
        return len(subtags) > 1
    if not (2 <= len(subtags[0]) <= 8 and subtags[0].isalpha()):
        return False

    variants: set[str] = set()
    extensions: set[str] = set()
    in_extension = False
    for subtag in subtags[1:]:
        if len(subtag) == 1:
            in_extension = True
            if subtag == "x":
                return subtag != subtags[-1]
            if subtag in extensions:
                return False
            extensions.add(subtag)
            continue
        if in_extension:
            continue
        is_variant = 5 <= len(subtag) <= 8 or (len(subtag) == 4 and subtag[0].isdigit())
        if is_variant:
            if subtag in variants:
                return False
            variants.add(subtag)
    return not in_extension or len(subtags[-1]) > 1


def _issue(path: str, keyword: str, message: str) -> ValidationIssue:
    return ValidationIssue(path=path, keyword=keyword, message=message)


def _raise_refinement(document_type: str, issues: list[ValidationIssue]) -> None:
    if issues:
        raise OdpValidationError(document_type, issues)
