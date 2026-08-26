"""In-memory Catalog for small Services and runnable examples."""

from __future__ import annotations

import base64
import binascii
import hmac
import json
import secrets
import time
from dataclasses import dataclass
from typing import TypeVar

from offering_protocol.core import (
    VERSION,
    Collection,
    CollectionSearchRequest,
    Offering,
    OfferingPage,
    OfferingSearchRequest,
    Operation,
    Page,
    parse_collection,
    parse_offering,
)
from offering_protocol.service.service import CatalogError, CatalogRequest, RequestError

_DEFAULT_PAGE_LIMIT = 50
_CONTINUATION_LIFETIME_SECONDS = 3600


@dataclass(frozen=True, slots=True)
class StaticCatalogOptions:
    collections: tuple[Collection, ...] = ()
    offerings: tuple[Offering, ...] = ()


class StaticCatalog:
    def __init__(self, options: StaticCatalogOptions) -> None:
        self._continuation_key = secrets.token_bytes(32)
        try:
            self._collections = tuple(
                parse_collection(_model_bytes(item)) for item in options.collections
            )
            self._offerings = tuple(
                parse_offering(_model_bytes(item)) for item in options.offerings
            )
        except ValueError as error:
            raise CatalogError(f"Static Catalog contains an invalid resource: {error}") from error
        self._collection_by_id = _unique(self._collections, "Collection")
        self._offering_by_id = _unique(self._offerings, "Offering")
        for offering in self._offerings:
            if any(
                identifier not in self._collection_by_id for identifier in offering.collection_ids
            ):
                raise CatalogError(f"Offering {offering.id} refers to an unknown Collection")

    def operations(self) -> list[Operation]:
        values = [Operation.GET_OFFERING, Operation.LIST_OFFERINGS]
        if self._collections:
            values.extend(
                [
                    Operation.GET_COLLECTION,
                    Operation.LIST_COLLECTION_OFFERINGS,
                    Operation.LIST_COLLECTIONS,
                ]
            )
        return values

    async def list_offerings(self, request: CatalogRequest) -> OfferingPage[Offering]:
        items, next_reference = _page(self._offerings, request, self._continuation_key)
        return _offering_page(
            [_represent_offering(item, request, True) for item in items], next_reference
        )

    async def get_offering(self, identifier: str, request: CatalogRequest) -> Offering | None:
        offering = self._offering_by_id.get(identifier)
        return None if offering is None else _represent_offering(offering, request, False)

    async def search_offerings(
        self, query: OfferingSearchRequest, request: CatalogRequest
    ) -> OfferingPage[Offering]:
        del query, request
        raise CatalogError("search-offerings is unsupported")

    async def list_collections(self, request: CatalogRequest) -> Page[Collection]:
        items, next_reference = _page(self._collections, request, self._continuation_key)
        return _collection_page(
            [_represent_collection(item, request, True) for item in items], next_reference
        )

    async def get_collection(self, identifier: str, request: CatalogRequest) -> Collection | None:
        collection = self._collection_by_id.get(identifier)
        return None if collection is None else _represent_collection(collection, request, False)

    async def search_collections(
        self, query: CollectionSearchRequest, request: CatalogRequest
    ) -> Page[Collection]:
        del query, request
        raise CatalogError("search-collections is unsupported")

    async def list_collection_offerings(
        self, collection_id: str, request: CatalogRequest
    ) -> OfferingPage[Offering]:
        if collection_id not in self._collection_by_id:
            raise RequestError(404, "NOT_FOUND", "Collection not found")
        offerings = tuple(item for item in self._offerings if collection_id in item.collection_ids)
        items, next_reference = _page(offerings, request, self._continuation_key)
        return _offering_page(
            [_represent_offering(item, request, True) for item in items], next_reference
        )


Resource = TypeVar("Resource", Collection, Offering)


def _unique(values: tuple[Resource, ...], label: str) -> dict[str, Resource]:
    result: dict[str, Resource] = {}
    for value in values:
        if value.id in result:
            raise CatalogError(f"{label} identifiers must be unique")
        result[value.id] = value
    return result


def _model_bytes(value: Resource) -> bytes:
    return value.model_dump_json(by_alias=True, exclude_unset=True).encode()


def _offering_page(items: list[Offering], next_reference: str) -> OfferingPage[Offering]:
    values: dict[str, object] = {"items": items, "odp_version": VERSION}
    if next_reference:
        values["next"] = next_reference
    return OfferingPage[Offering].model_validate(values)


def _collection_page(items: list[Collection], next_reference: str) -> Page[Collection]:
    values: dict[str, object] = {"items": items, "odp_version": VERSION}
    if next_reference:
        values["next"] = next_reference
    return Page[Collection].model_validate(values)


def _page(
    values: tuple[Resource, ...], request: CatalogRequest, continuation_key: bytes
) -> tuple[list[Resource], str]:
    limit = request.limit or _DEFAULT_PAGE_LIMIT
    offset = _decode_cursor(request, limit, continuation_key)
    if offset > len(values):
        raise _invalid_cursor()
    end = min(offset + limit, len(values))
    next_reference = (
        _encode_cursor(request, limit, end, continuation_key) if end < len(values) else ""
    )
    return list(values[offset:end]), next_reference


def _represent_offering(value: Offering, request: CatalogRequest, embedded: bool) -> Offering:
    if request.representation.value == "full":
        return value
    document: dict[str, object] = {
        "id": value.id,
        "name": value.name,
    }
    for name in (
        "auth_expands",
        "collection_ids",
        "description",
        "images",
        "language",
        "localizations",
        "price",
        "web_url",
    ):
        if name in value.model_fields_set:
            document[name] = getattr(value, name)
    if not embedded:
        document["odp_version"] = value.odp_version
    return Offering.model_validate(document)


def _represent_collection(value: Collection, request: CatalogRequest, embedded: bool) -> Collection:
    if request.representation.value == "full":
        return value
    document: dict[str, object] = {
        "id": value.id,
        "name": value.name,
    }
    for name in (
        "auth_expands",
        "description",
        "images",
        "language",
        "localizations",
        "parent_ids",
        "web_url",
    ):
        if name in value.model_fields_set:
            document[name] = getattr(value, name)
    if not embedded:
        document["odp_version"] = value.odp_version
    return Collection.model_validate(document)


def _encode_cursor(
    request: CatalogRequest, limit: int, offset: int, continuation_key: bytes
) -> str:
    value = {
        "expires": int(time.time()) + _CONTINUATION_LIFETIME_SECONDS,
        "limit": limit,
        "offset": offset,
        "path": request.path,
        "representation": request.representation.value,
    }
    payload = (
        base64.urlsafe_b64encode(json.dumps(value, separators=(",", ":"), sort_keys=True).encode())
        .rstrip(b"=")
        .decode()
    )
    signature = (
        base64.urlsafe_b64encode(hmac.digest(continuation_key, payload.encode(), "sha256"))
        .rstrip(b"=")
        .decode()
    )
    token = f"{payload}.{signature}"
    return (
        f"{request.path}?cursor={token}&limit={limit}&representation={request.representation.value}"
    )


def _decode_cursor(request: CatalogRequest, limit: int, continuation_key: bytes) -> int:
    if request.cursor is None:
        return 0
    try:
        payload, signature = request.cursor.split(".")
        signature_padding = "=" * (-len(signature) % 4)
        supplied_signature = base64.urlsafe_b64decode(signature + signature_padding)
        expected_signature = hmac.digest(continuation_key, payload.encode(), "sha256")
        if not hmac.compare_digest(supplied_signature, expected_signature):
            raise ValueError
        payload_padding = "=" * (-len(payload) % 4)
        value = json.loads(base64.urlsafe_b64decode(payload + payload_padding))
        if (
            not isinstance(value, dict)
            or value.get("expires", 0) < int(time.time())
            or value.get("limit") != limit
            or value.get("path") != request.path
            or value.get("representation") != request.representation.value
            or not isinstance(value.get("offset"), int)
        ):
            raise ValueError
        return int(value["offset"])
    except (ValueError, TypeError, json.JSONDecodeError, binascii.Error) as error:
        raise _invalid_cursor() from error


def _invalid_cursor() -> RequestError:
    return RequestError(410, "CONTINUATION_UNAVAILABLE", "Continuation is unavailable")
