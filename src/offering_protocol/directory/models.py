"""Canonical Directory request and response models."""

from __future__ import annotations

from enum import StrEnum

from pydantic import Field

from offering_protocol.core.models import (
    AuthenticationRequirement,
    EnrollmentProtocol,
    OdpModel,
    Operation,
    OperationDescriptor,
    PaymentOption,
    Protocol,
    ServiceProtocols,
)


class Environment(StrEnum):
    PRODUCTION = "production"
    SANDBOX = "sandbox"

    @property
    def origin(self) -> str:
        if self is Environment.PRODUCTION:
            return "https://api.inflowpay.ai"
        return "https://sandbox.inflowpay.ai"


class OperationFilter(OdpModel):
    authentication: AuthenticationRequirement | None = None
    name: Operation


class PaymentFilter(OdpModel):
    authentication: AuthenticationRequirement | None = None
    name: Protocol
    options: list[PaymentOption] = Field(default_factory=list)


class ServiceFilters(OdpModel):
    enrollment: list[EnrollmentProtocol] = Field(default_factory=list)
    keywords: list[str] = Field(default_factory=list)
    operations: list[OperationFilter] = Field(default_factory=list)
    payments: list[PaymentFilter] = Field(default_factory=list)


class SearchRequest(OdpModel):
    filters: ServiceFilters | None = None
    limit: int = 0
    query: str = ""


class DirectoryService(OdpModel):
    description: str
    documentation_url: str = ""
    indexed_at: str
    keywords: list[str] = Field(default_factory=list)
    language: str
    localizations: list[str]
    name: str
    operations: list[OperationDescriptor]
    protocols: ServiceProtocols | None = None
    service_origin: str
    status_url: str = ""
    support_url: str = ""
    website_url: str = ""


class PaymentOptionFacetValue(OdpModel):
    name: Protocol
    option: PaymentOption


class Facet(OdpModel):
    count: int
    value: object


class Facets(OdpModel):
    enrollment: list[Facet] = Field(default_factory=list)
    keywords: list[Facet] = Field(default_factory=list)
    operations: list[Facet] = Field(default_factory=list)
    payment_options: list[Facet] = Field(default_factory=list)
    payments: list[Facet] = Field(default_factory=list)


class SearchPage(OdpModel):
    facets: Facets | None = None
    items: list[DirectoryService]
    next: str = ""


class SuggestionRequest(OdpModel):
    limit: int = 0
    prefix: str


class IterationOptions(OdpModel):
    max_items: int = 0
    max_pages: int = 0
