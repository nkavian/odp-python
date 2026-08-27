from __future__ import annotations

import json
from typing import cast

import pytest

from offering_protocol.core import (
    AuthenticationRequirement,
    Collection,
    CollectionSearchRequest,
    EnrollmentProtocol,
    Offering,
    OfferingPage,
    OfferingSearchRequest,
    Operation,
    Page,
    PaymentProtocol,
    Protocol,
    Representation,
    TrustProtocol,
)
from offering_protocol.service import (
    MEDIA_TYPE,
    Catalog,
    CatalogError,
    CatalogRequest,
    Request,
    RequestError,
    Service,
    ServiceBuilder,
    ServiceError,
    StaticCatalog,
    StaticCatalogOptions,
)
from offering_protocol.service.static_catalog import _encode_cursor


def _catalog() -> StaticCatalog:
    return StaticCatalog(
        StaticCatalogOptions(
            collections=(
                Collection(
                    description="Indoor plants.",
                    id="plants",
                    name="Plants",
                    odp_version="1.0",
                ),
            ),
            offerings=(
                Offering(
                    collection_ids=["plants"],
                    description="A resilient indoor plant.",
                    id="rubber-plant",
                    name="Rubber Plant",
                    odp_version="1.0",
                ),
                Offering(
                    collection_ids=["plants"],
                    id="snake-plant",
                    name="Snake Plant",
                    odp_version="1.0",
                ),
            ),
        )
    )


def _service(catalog: Catalog | None = None) -> Service:
    return (
        ServiceBuilder("Indica Flowers", "An AI-enabled plant store.", "en", "/odp")
        .keywords(["plants", "indoor-plants"])
        .localizations(["en"])
        .website_url("https://demo.inflowpay.ai")
        .documentation_url("/docs")
        .support_url("/support")
        .status_url("/status")
        .payment_origins(["https://demo.inflowpay.ai"])
        .operation_authentication(Operation.GET_OFFERING, AuthenticationRequirement.REQUIRED)
        .protocols(
            [EnrollmentProtocol(name=Protocol.AEP)],
            [],
            [TrustProtocol(name=Protocol.TAP)],
        )
        .build(catalog or _catalog())
    )


@pytest.mark.asyncio
async def test_serves_document_offerings_and_collections() -> None:
    service = _service()
    document = await service.handle(Request("GET", "/.well-known/odp"))
    assert document.status == 200
    assert json.loads(document.body)["name"] == "Indica Flowers"
    assert json.loads(document.body)["protocols"]["trust"] == [{"name": "tap"}]
    assert service.document.operations[0].authentication is AuthenticationRequirement.REQUIRED
    offerings = await service.handle(
        Request("GET", "/odp/offerings", headers={"Accept": MEDIA_TYPE}, query="limit=1")
    )
    assert offerings.status == 200
    first_page = json.loads(offerings.body)
    assert first_page["items"][0]["id"] == "rubber-plant"
    assert "odp_version" not in first_page["items"][0]
    assert first_page["next"]
    collection = await service.handle(Request("GET", "/odp/collections/plants"))
    assert json.loads(collection.body)["name"] == "Plants"
    assert json.loads(collection.body)["odp_version"] == "1.0"
    collections = await service.handle(Request("GET", "/odp/collections"))
    assert json.loads(collections.body)["items"][0]["id"] == "plants"
    collection_offerings = await service.handle(Request("GET", "/odp/collections/plants/offerings"))
    assert len(json.loads(collection_offerings.body)["items"]) == 2
    offering = await service.handle(Request("GET", "/odp/offerings/rubber-plant"))
    assert json.loads(offering.body)["name"] == "Rubber Plant"
    assert json.loads(offering.body)["odp_version"] == "1.0"
    full_offerings = await service.handle(
        Request("GET", "/odp/offerings", query="representation=full")
    )
    assert json.loads(full_offerings.body)["items"][0]["odp_version"] == "1.0"
    full_collections = await service.handle(
        Request("GET", "/odp/collections", query="representation=full")
    )
    assert json.loads(full_collections.body)["items"][0]["description"] == "Indoor plants."


@pytest.mark.asyncio
async def test_static_catalog_continuation_is_bound_to_request() -> None:
    catalog = _catalog()
    first = await catalog.list_offerings(
        CatalogRequest(limit=1, path="/odp/offerings", representation=Representation.TERSE)
    )
    cursor = first.next.split("cursor=", 1)[1].split("&", 1)[0]
    second = await catalog.list_offerings(
        CatalogRequest(
            cursor=cursor,
            limit=1,
            path="/odp/offerings",
            representation=Representation.TERSE,
        )
    )
    assert second.items[0].id == "snake-plant"
    with pytest.raises(RequestError):
        await catalog.list_offerings(CatalogRequest(cursor=cursor, limit=2, path="/odp/offerings"))


@pytest.mark.asyncio
async def test_returns_protocol_problem_responses() -> None:
    service = _service()
    cases = [
        (Request("POST", "/.well-known/odp"), 405),
        (Request("GET", "/other"), 404),
        (Request("GET", "/odp/offerings", headers={"accept": "text/plain"}), 406),
        (Request("GET", "/odp/offerings/bad/path"), 400),
        (Request("GET", "/odp/offerings/missing"), 404),
        (Request("GET", "/odp/collections/missing"), 404),
        (Request("DELETE", "/odp/offerings"), 405),
        (Request("GET", "/odp/unknown"), 404),
        (Request("GET", "/odp/offerings", query="representation=wrong"), 400),
        (Request("GET", "/odp/offerings", query="limit=101"), 400),
    ]
    for request, status in cases:
        response = await service.handle(request)
        assert response.status == status
        assert response.headers["content-type"] == "application/problem+json"


class SearchCatalog(StaticCatalog):
    def operations(self) -> list[Operation]:
        return [*super().operations(), Operation.SEARCH_COLLECTIONS, Operation.SEARCH_OFFERINGS]

    async def search_offerings(
        self, query: OfferingSearchRequest, request: CatalogRequest
    ) -> OfferingPage[Offering]:
        del query
        return await self.list_offerings(request)

    async def search_collections(
        self, query: CollectionSearchRequest, request: CatalogRequest
    ) -> Page[Collection]:
        del query
        return await self.list_collections(request)


@pytest.mark.asyncio
async def test_routes_search_requests_with_fixed_media_type() -> None:
    service = _service(SearchCatalog(_catalog_options()))
    offering = await service.handle(
        Request(
            "POST",
            "/odp/offerings/search",
            body=b'{"odp_version":"1.0","query":"plant"}',
            headers={"content-type": MEDIA_TYPE},
        )
    )
    assert offering.status == 200
    collection = await service.handle(
        Request(
            "POST",
            "/odp/collections/search",
            body=b'{"odp_version":"1.0","query":"plant"}',
            headers={"content-type": MEDIA_TYPE},
        )
    )
    assert collection.status == 200
    for request, status in [
        (Request("POST", "/odp/offerings/search", body=b"{}"), 415),
        (
            Request(
                "POST",
                "/odp/offerings/search",
                body=b"x" * 65_537,
                headers={"content-type": MEDIA_TYPE},
            ),
            413,
        ),
        (
            Request(
                "POST",
                "/odp/offerings/search",
                body=b"{}",
                headers={"content-type": MEDIA_TYPE},
            ),
            400,
        ),
    ]:
        assert (await service.handle(request)).status == status


def _catalog_options() -> StaticCatalogOptions:
    catalog = _catalog()
    return StaticCatalogOptions(catalog._collections, catalog._offerings)


def test_rejects_invalid_catalog_configuration() -> None:
    class IncompleteCatalog:
        def operations(self) -> list[Operation]:
            return []

    with pytest.raises(ServiceError):
        _service(cast(Catalog, IncompleteCatalog()))
    with pytest.raises(CatalogError):
        StaticCatalog(
            StaticCatalogOptions(
                offerings=(
                    Offering(id="same", name="One", odp_version="1.0"),
                    Offering(id="same", name="Two", odp_version="1.0"),
                )
            )
        )
    with pytest.raises(CatalogError):
        StaticCatalog(
            StaticCatalogOptions(offerings=(Offering.model_construct(id="bad/path", name="Bad"),))
        )


@pytest.mark.asyncio
async def test_catalog_and_service_response_boundaries() -> None:
    catalog = _catalog()
    request = CatalogRequest(limit=1, path="/odp/offerings")
    cursor = (
        _encode_cursor(request, 1, 999, catalog._continuation_key)
        .split("cursor=", 1)[1]
        .split("&", 1)[0]
    )
    with pytest.raises(RequestError):
        await catalog.list_offerings(CatalogRequest(cursor=cursor, limit=1, path="/odp/offerings"))
    with pytest.raises(RequestError):
        await catalog.list_offerings(
            CatalogRequest(cursor=f"{cursor}A", limit=1, path="/odp/offerings")
        )

    class MismatchedCatalog(StaticCatalog):
        async def get_offering(self, identifier: str, request: CatalogRequest) -> Offering | None:
            del identifier, request
            return Offering(id="other", name="Other", odp_version="1.0")

        async def get_collection(
            self, identifier: str, request: CatalogRequest
        ) -> Collection | None:
            del identifier, request
            return Collection(id="other", name="Other", odp_version="1.0")

    service = _service(MismatchedCatalog(_catalog_options()))
    assert (await service.handle(Request("GET", "/odp/offerings/rubber-plant"))).status == 500
    assert (await service.handle(Request("GET", "/odp/collections/plants"))).status == 500

    class ApplicationCatalog:
        def operations(self) -> list[Operation]:
            return [Operation.GET_OFFERING, Operation.LIST_OFFERINGS]

        async def list_offerings(self, request: CatalogRequest) -> OfferingPage[Offering]:
            del request
            invalid = Offering.model_construct(id="bad/path", name="Bad")
            return OfferingPage(odp_version="1.0", items=[invalid])

        async def get_offering(self, identifier: str, request: CatalogRequest) -> Offering | None:
            del identifier, request
            return None

    application_service = _service(cast(Catalog, ApplicationCatalog()))
    assert (await application_service.handle(Request("GET", "/odp/offerings"))).status == 500

    enrollment = EnrollmentProtocol(name=Protocol.AEP)
    payment = PaymentProtocol(
        authentication=AuthenticationRequirement.NOT_REQUIRED, name=Protocol.MPP
    )
    ServiceBuilder("Plants", "Plants", "en", "/odp").protocols([enrollment], []).build(_catalog())
    ServiceBuilder("Plants", "Plants", "en", "/odp").protocols([], [payment]).build(_catalog())

    collection_catalog = StaticCatalog(
        StaticCatalogOptions(
            collections=(
                Collection(id="one", name="One", odp_version="1.0"),
                Collection(id="two", name="Two", odp_version="1.0"),
            ),
            offerings=(Offering(id="plant", name="Plant", odp_version="1.0"),),
        )
    )
    collection_page = await collection_catalog.list_collections(
        CatalogRequest(limit=1, path="/odp/collections")
    )
    assert collection_page.next
    with pytest.raises(CatalogError):
        StaticCatalog(
            StaticCatalogOptions(
                offerings=(
                    Offering(
                        collection_ids=["missing"],
                        id="one",
                        name="One",
                        odp_version="1.0",
                    ),
                )
            )
        )
