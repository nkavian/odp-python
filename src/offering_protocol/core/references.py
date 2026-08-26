"""ODP URL, reference, operation-path, and identity helpers."""

from __future__ import annotations

from ipaddress import ip_address
from typing import cast
from urllib.parse import SplitResult, urljoin, urlsplit, urlunsplit

from offering_protocol.core.models import Operation, ResourceIdentity, ResourceType


class ReferenceError(ValueError):
    """Raised when an ODP URL or reference violates protocol constraints."""


_RESOURCE_OPERATIONS = {
    Operation.GET_COLLECTION,
    Operation.GET_OFFERING,
    Operation.LIST_COLLECTION_OFFERINGS,
}

_OPERATION_PATHS = {
    Operation.LIST_COLLECTIONS: "/collections",
    Operation.SEARCH_COLLECTIONS: "/collections/search",
    Operation.LIST_OFFERINGS: "/offerings",
    Operation.SEARCH_OFFERINGS: "/offerings/search",
}


def is_local_resource_identifier(value: str) -> bool:
    return (
        bool(value)
        and value not in {".", ".."}
        and len(value) <= 128
        and all(
            character.isascii() and (character.isalnum() or character in "._~-")
            for character in value
        )
    )


def operation_method(operation: Operation) -> str:
    if operation in {Operation.SEARCH_COLLECTIONS, Operation.SEARCH_OFFERINGS}:
        return "POST"
    return "GET"


def derive_service_origin(service_url: str) -> str:
    parsed = _parse_secure_url(service_url)
    hostname = cast(str, parsed.hostname)
    host = f"[{hostname}]" if ":" in hostname else hostname
    port = parsed.port
    if port is not None and not (parsed.scheme == "https" and port == 443):
        host = f"{host}:{port}"
    return urlunsplit((parsed.scheme, host, "", "", ""))


def resolve_resource_reference(reference: str, service_origin: str) -> str:
    if reference.startswith("//"):
        raise ReferenceError("ODP resource reference cannot be scheme-relative")
    if not reference.startswith(
        ("/", "https://", "http://localhost", "http://127.0.0.1", "http://[::1]")
    ):
        raise ReferenceError(
            "ODP resource reference must be an origin-relative absolute path or secure absolute URL"
        )
    resolved = _parse_secure_url(urljoin(f"{derive_service_origin(service_origin)}/", reference))
    if resolved.fragment:
        raise ReferenceError("ODP resource reference cannot contain a fragment")
    return resolved.geturl()


def resolve_continuation(reference: str, service_origin: str) -> str:
    resolved = resolve_resource_reference(reference, service_origin)
    if derive_service_origin(resolved) != derive_service_origin(service_origin):
        raise ReferenceError("ODP continuation reference must remain on the Service origin")
    return resolved


def build_operation_url(
    endpoint_base: str,
    operation: Operation,
    service_origin: str,
    resource_id: str | None = None,
) -> str:
    if not endpoint_base.startswith("/") or endpoint_base.startswith("//"):
        raise ReferenceError("ODP endpoint base must be an origin-relative absolute path")
    path = operation_path(operation, resource_id)
    return resolve_resource_reference(f"{endpoint_base.rstrip('/')}{path}", service_origin)


def operation_path(operation: Operation, resource_id: str | None = None) -> str:
    if operation in _RESOURCE_OPERATIONS:
        if resource_id is None or not is_local_resource_identifier(resource_id):
            raise ReferenceError(f"{operation.value} requires a valid local resource identifier")
    elif resource_id is not None:
        raise ReferenceError(f"{operation.value} does not accept a resource identifier")

    if operation is Operation.GET_COLLECTION:
        return f"/collections/{resource_id}"
    if operation is Operation.LIST_COLLECTION_OFFERINGS:
        return f"/collections/{resource_id}/offerings"
    if operation is Operation.GET_OFFERING:
        return f"/offerings/{resource_id}"
    return _OPERATION_PATHS[operation]


def create_resource_identity(
    service_document_url: str, resource_type: ResourceType, resource_id: str
) -> ResourceIdentity:
    if not is_local_resource_identifier(resource_id):
        raise ReferenceError(f"{resource_type.value} requires a valid local resource identifier")
    return ResourceIdentity(
        id=resource_id,
        service=derive_service_origin(service_document_url),
        type=resource_type,
    )


def _parse_secure_url(value: str) -> SplitResult:
    parsed = urlsplit(value)
    if parsed.username is not None or parsed.password is not None:
        raise ReferenceError("ODP URL cannot contain user information")
    hostname = parsed.hostname
    if hostname is None:
        raise ReferenceError("ODP URL must include a host")
    loopback = hostname.lower() == "localhost"
    if not loopback:
        try:
            loopback = ip_address(hostname).is_loopback
        except ValueError:
            loopback = False
    if parsed.scheme != "https" and not (parsed.scheme == "http" and loopback):
        raise ReferenceError("ODP URL must use HTTPS except on loopback hosts")
    return parsed
