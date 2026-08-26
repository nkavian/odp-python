"""Directory-to-Service federated Offering discovery."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Protocol

from offering_protocol.agent.client import AgentError, ServiceClient, TraversalOptions
from offering_protocol.core import Offering, OfferingSearchRequest, Representation
from offering_protocol.directory import (
    DirectoryClient,
    DirectoryError,
    DirectoryService,
    Environment,
    IterationOptions,
    SearchRequest,
    TransportError,
)


class ServiceClientFactory(Protocol):
    def create(self, service: DirectoryService) -> ServiceClient: ...


class DefaultServiceClientFactory:
    def create(self, service: DirectoryService) -> ServiceClient:
        return ServiceClient(service.service_origin)


@dataclass(frozen=True, slots=True)
class FederatedSearchRequest:
    concurrency: int = 0
    max_offerings_per_service: int = 0
    max_services: int = 0
    offerings: OfferingSearchRequest = field(default_factory=OfferingSearchRequest)
    services: SearchRequest = field(default_factory=SearchRequest)


@dataclass(frozen=True, slots=True)
class DiscoveryEvent:
    service: DirectoryService
    offering: Offering | None = None
    issue: str | None = None


class Agent:
    def __init__(
        self,
        environment: Environment = Environment.PRODUCTION,
        *,
        directory: DirectoryClient | None = None,
        factory: ServiceClientFactory | None = None,
    ) -> None:
        self._owns_directory = directory is None
        self.directory = directory or DirectoryClient(environment)
        self.factory = factory or DefaultServiceClientFactory()

    async def __aenter__(self) -> Agent:
        return self

    async def __aexit__(self, *args: object) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        if self._owns_directory:
            await self.directory.aclose()

    @property
    def environment(self) -> Environment:
        return self.directory.environment

    async def search_offerings_across_services(
        self, request: FederatedSearchRequest
    ) -> list[DiscoveryEvent]:
        maximum_services = _bounded(request.max_services, 10, 100, "max_services")
        maximum_offerings = _bounded(
            request.max_offerings_per_service, 10, 100, "max_offerings_per_service"
        )
        concurrency = _bounded(request.concurrency, 4, 16, "concurrency")
        try:
            services = await self.directory.search_services(
                request.services,
                IterationOptions(max_items=maximum_services, max_pages=16),
            )
        except (DirectoryError, TransportError) as error:
            raise AgentError(f"ODP Directory failed: {error}") from error
        semaphore = asyncio.Semaphore(concurrency)

        async def search(service: DirectoryService) -> list[DiscoveryEvent]:
            async with semaphore:
                try:
                    offerings = await _search_service(
                        self.factory,
                        service,
                        request.offerings,
                        maximum_offerings,
                    )
                    return [DiscoveryEvent(service, offering) for offering in offerings]
                except Exception as error:
                    return [DiscoveryEvent(service, issue=str(error))]

        results = await asyncio.gather(*(search(service) for service in services))
        return [event for group in results for event in group]


async def _search_service(
    factory: ServiceClientFactory,
    service: DirectoryService,
    request: OfferingSearchRequest,
    maximum: int,
) -> list[Offering]:
    async with factory.create(service) as client:
        options = TraversalOptions(max_items=maximum, max_pages=16)
        if _has_search(request):
            return await client.search_all_offerings(request, Representation.TERSE, options)
        return await client.list_all_offerings(Representation.TERSE, 0, options)


def _has_search(request: OfferingSearchRequest) -> bool:
    return bool(
        request.query
        or request.filters
        or request.include_descendants
        or request.sort
        or request.refinements
        or request.collection_id
    )


def _bounded(value: int, fallback: int, maximum: int, name: str) -> int:
    result = fallback if value == 0 else value
    if not 1 <= result <= maximum:
        raise AgentError(f"{name} must be from 1 through {maximum}")
    return result
