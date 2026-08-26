"""Typed Offering Discovery Protocol models."""

from __future__ import annotations

from enum import StrEnum
from typing import Generic, TypeVar, cast

from pydantic import BaseModel, ConfigDict, Field, JsonValue

VERSION = "1.0"

__all__ = [
    "VERSION",
    "Action",
    "ActionRelation",
    "ActionRequest",
    "AuthenticationRequirement",
    "CapabilityLink",
    "Collection",
    "CollectionSearchRequest",
    "EnrollmentProtocol",
    "FilterCapabilitySource",
    "FilterDefinition",
    "FilterExpression",
    "FilterOperator",
    "FilterType",
    "FilterUnit",
    "HttpActionTarget",
    "HttpConfiguration",
    "InvalidParameter",
    "McpEndpoint",
    "McpEndpointType",
    "MissingPlacement",
    "OdpModel",
    "Offering",
    "OfferingPage",
    "OfferingSearchRequest",
    "OpenApiActionTarget",
    "Operation",
    "OperationDescriptor",
    "Page",
    "PaymentOption",
    "PaymentProtocol",
    "PricePreview",
    "PriceType",
    "ProblemDetails",
    "Protocol",
    "RefinementBucket",
    "RefinementGroup",
    "Representation",
    "ResourceIdentity",
    "ResourceImage",
    "ResourceImageType",
    "ResourceType",
    "SchemaReference",
    "SearchCapabilities",
    "ServiceBranding",
    "ServiceBrandingImage",
    "ServiceBrandingImageType",
    "ServiceDocument",
    "ServiceOpenApi",
    "ServiceProtocols",
    "SortCapabilitySource",
    "SortDefinition",
    "SortDirection",
    "SortKey",
    "TrustProtocol",
]


class OdpModel(BaseModel):
    """Base model that preserves additive protocol members."""

    model_config = ConfigDict(extra="allow", frozen=True, populate_by_name=True)

    @property
    def additional(self) -> dict[str, JsonValue]:
        return dict(self.model_extra or {})

    def to_dict(self) -> dict[str, JsonValue]:
        return cast(
            dict[str, JsonValue],
            self.model_dump(mode="json", by_alias=True, exclude_unset=True),
        )


class Representation(StrEnum):
    TERSE = "terse"
    FULL = "full"


class PriceType(StrEnum):
    FIXED = "fixed"
    FREE = "free"
    METERED = "metered"
    QUOTE = "quote"
    RANGE = "range"
    STARTING_AT = "starting_at"


class ActionRelation(StrEnum):
    DOWNLOAD = "download"
    INVOKE = "invoke"
    PURCHASE = "purchase"
    QUOTE = "quote"
    RESERVE = "reserve"


class ResourceType(StrEnum):
    COLLECTION = "collection"
    OFFERING = "offering"


class Operation(StrEnum):
    GET_COLLECTION = "get-collection"
    GET_OFFERING = "get-offering"
    LIST_COLLECTION_OFFERINGS = "list-collection-offerings"
    LIST_COLLECTIONS = "list-collections"
    LIST_OFFERINGS = "list-offerings"
    SEARCH_COLLECTIONS = "search-collections"
    SEARCH_OFFERINGS = "search-offerings"


class AuthenticationRequirement(StrEnum):
    NOT_REQUIRED = "not-required"
    OPTIONAL = "optional"
    REQUIRED = "required"


class Protocol(StrEnum):
    AEP = "aep"
    MPP = "mpp"
    TAP = "tap"
    X402 = "x402"


class PaymentOption(StrEnum):
    ALGORAND = "algorand"
    APTOS = "aptos"
    ARBITRUM = "arbitrum"
    AVALANCHE = "avalanche"
    BASE = "base"
    CARD = "card"
    ETHEREUM = "ethereum"
    HEDERA = "hedera"
    INFLOW = "inflow"
    LIGHTNING = "lightning"
    POLYGON = "polygon"
    SOLANA = "solana"
    STELLAR = "stellar"
    STRIPE = "stripe"
    TEMPO = "tempo"
    TON = "ton"


class FilterType(StrEnum):
    BOOLEAN = "boolean"
    DATE = "date"
    DATE_TIME = "date-time"
    DECIMAL = "decimal"
    INTEGER = "integer"
    NUMBER = "number"
    STRING = "string"


class FilterOperator(StrEnum):
    EQUAL = "eq"
    EXISTS = "exists"
    GREATER_THAN = "gt"
    GREATER_THAN_OR_EQUAL = "gte"
    IN = "in"
    LESS_THAN = "lt"
    LESS_THAN_OR_EQUAL = "lte"


class SortDirection(StrEnum):
    ASCENDING = "ascending"
    DESCENDING = "descending"


class MissingPlacement(StrEnum):
    FIRST = "first"
    LAST = "last"


class ServiceBrandingImageType(StrEnum):
    PNG = "image/png"
    SVG = "image/svg+xml"
    WEBP = "image/webp"


class ResourceImageType(StrEnum):
    AVIF = "image/avif"
    JPEG = "image/jpeg"
    PNG = "image/png"
    SVG = "image/svg+xml"
    WEBP = "image/webp"


class McpEndpointType(StrEnum):
    STREAMABLE_HTTP = "streamable-http"


class OperationDescriptor(OdpModel):
    authentication: AuthenticationRequirement
    name: Operation


class EnrollmentProtocol(OdpModel):
    name: Protocol


class PaymentProtocol(OdpModel):
    authentication: AuthenticationRequirement
    name: Protocol
    options: list[PaymentOption] = Field(default_factory=list)


class TrustProtocol(OdpModel):
    name: Protocol


class ServiceProtocols(OdpModel):
    enrollment: list[EnrollmentProtocol] = Field(default_factory=list)
    payments: list[PaymentProtocol] = Field(default_factory=list)
    trust: list[TrustProtocol] = Field(default_factory=list)


class ServiceOpenApi(OdpModel):
    url: str


class HttpConfiguration(OdpModel):
    endpoint_base: str
    openapi: ServiceOpenApi | None = None


class CapabilityLink(OdpModel):
    href: str


class FilterUnit(OdpModel):
    code: str
    system: str
    title: str = ""


class FilterDefinition(OdpModel):
    description: str
    id: str
    operators: list[FilterOperator]
    refinable: bool = False
    title: str
    filter_type: FilterType = Field(alias="type")
    unit: FilterUnit | None = None


class SortKey(OdpModel):
    direction: SortDirection
    filter_id: str
    missing: MissingPlacement


class SortDefinition(OdpModel):
    description: str
    id: str
    keys: list[SortKey]
    title: str


class FilterCapabilitySource(OdpModel):
    inline: list[FilterDefinition] = Field(default_factory=list)
    linked: CapabilityLink | None = None


class SortCapabilitySource(OdpModel):
    inline: list[SortDefinition] = Field(default_factory=list)
    linked: CapabilityLink | None = None


class SearchCapabilities(OdpModel):
    filters: FilterCapabilitySource | None = None
    sorts: SortCapabilitySource | None = None


class ServiceBrandingImage(OdpModel):
    src: str
    media_type: ServiceBrandingImageType | None = Field(default=None, alias="type")


class ServiceBranding(OdpModel):
    icon: ServiceBrandingImage
    logo: ServiceBrandingImage


class McpEndpoint(OdpModel):
    description: str = ""
    name: str = ""
    endpoint_type: McpEndpointType = Field(alias="type")
    url: str


class ServiceDocument(OdpModel):
    branding: ServiceBranding | None = None
    description: str
    documentation_url: str = ""
    http: HttpConfiguration
    keywords: list[str] = Field(default_factory=list)
    language: str
    localizations: list[str]
    mcp: list[McpEndpoint] = Field(default_factory=list)
    name: str
    odp_version: str
    operations: list[OperationDescriptor]
    payment_origins: list[str] = Field(default_factory=list)
    protocols: ServiceProtocols | None = None
    search_capabilities: SearchCapabilities | None = None
    status_url: str = ""
    support_url: str = ""
    website_url: str = ""


class ResourceImage(OdpModel):
    alt: str = ""
    height: int = 0
    src: str
    media_type: ResourceImageType | None = Field(default=None, alias="type")
    width: int = 0


class Collection(OdpModel):
    auth_expands: bool = False
    description: str = ""
    detail_fields: list[str] = Field(default_factory=list)
    id: str
    images: list[ResourceImage] = Field(default_factory=list)
    language: str = ""
    localizations: list[str] = Field(default_factory=list)
    name: str
    odp_version: str = ""
    parent_ids: list[str] = Field(default_factory=list)
    search_capabilities: SearchCapabilities | None = None
    web_url: str = ""


class SchemaReference(OdpModel):
    url: str


class PricePreview(OdpModel):
    amount: str = ""
    currency: str = ""
    maximum: str = ""
    minimum: str = ""
    price_type: PriceType = Field(alias="type")
    unit: str = ""


class ActionRequest(OdpModel):
    content_type: str = ""
    schema_: SchemaReference | None = Field(default=None, alias="schema")


class HttpActionTarget(OdpModel):
    href: str
    method: str
    request: ActionRequest | None = None
    response_content_types: list[str] = Field(default_factory=list)


class OpenApiActionTarget(OdpModel):
    operation_id: str
    url: str = ""


class Action(OdpModel):
    authentication: AuthenticationRequirement
    description: str = ""
    http: HttpActionTarget | None = None
    id: str
    openapi: OpenApiActionTarget | None = None
    rel: ActionRelation


class Offering(OdpModel):
    actions: list[Action] = Field(default_factory=list)
    auth_expands: bool = False
    attributes: dict[str, JsonValue] = Field(default_factory=dict)
    collection_ids: list[str] = Field(default_factory=list)
    description: str = ""
    detail_fields: list[str] = Field(default_factory=list)
    id: str
    images: list[ResourceImage] = Field(default_factory=list)
    language: str = ""
    localizations: list[str] = Field(default_factory=list)
    name: str
    odp_version: str = ""
    price: PricePreview | None = None
    schema_: SchemaReference | None = Field(default=None, alias="schema")
    web_url: str = ""


class InvalidParameter(OdpModel):
    location: str = Field(alias="in")
    name: str
    reason: str


class ProblemDetails(OdpModel):
    code: str
    detail: str = ""
    instance: str = ""
    invalid_params: list[InvalidParameter] = Field(default_factory=list)
    status: int
    title: str
    problem_type: str = Field(alias="type")


class ResourceIdentity(OdpModel):
    id: str
    service: str
    resource_type: ResourceType = Field(alias="type")

    @property
    def key(self) -> str:
        return f"{self.service}\0{self.resource_type.value}\0{self.id}"


class CollectionSearchRequest(OdpModel):
    limit: int = 0
    odp_version: str = VERSION
    parent_id: str | None = None
    query: str = ""


class FilterExpression(OdpModel):
    id: str
    operator: FilterOperator
    value: JsonValue


class OfferingSearchRequest(OdpModel):
    collection_id: str = ""
    filters: list[FilterExpression] = Field(default_factory=list)
    include_descendants: bool = False
    limit: int = 0
    odp_version: str = VERSION
    query: str = ""
    refinements: list[str] = Field(default_factory=list)
    sort: str = ""


class RefinementBucket(OdpModel):
    count: int
    count_relation: str = ""
    value: JsonValue


class RefinementGroup(OdpModel):
    filter_id: str
    values: list[RefinementBucket]


Item = TypeVar("Item")


class Page(OdpModel, Generic[Item]):
    auth_expands: bool = False
    items: list[Item]
    next: str = ""
    odp_version: str


class OfferingPage(Page[Item], Generic[Item]):
    refinements: list[RefinementGroup] = Field(default_factory=list)
