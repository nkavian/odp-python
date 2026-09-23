"""Source-aware metadata validation for mixed Directory results."""

from __future__ import annotations

from ipaddress import ip_address
from urllib.parse import urlsplit

from pydantic import JsonValue, TypeAdapter

from offering_protocol.core import derive_service_origin, validate_value
from offering_protocol.directory.addresses import is_public
from offering_protocol.directory.models import DirectorySource

_OBJECT = TypeAdapter(dict[str, JsonValue])


def read_source(value: JsonValue) -> DirectorySource:
    source = DirectorySource.model_validate(value, strict=True)
    if not source.type.strip() or len(source.type) > 128:
        raise ValueError("source.type must be a nonempty string of at most 128 characters")
    url = source.url
    if (
        len(url) > 2048
        or not url.lower().startswith("https://")
        or any(character.isspace() for character in url)
        or "#" in url
    ):
        raise ValueError("source.url must be an HTTPS document URL without a fragment")
    origin = derive_service_origin(url)
    host = urlsplit(origin).hostname or ""
    if host.rstrip(".") == "localhost" or host.rstrip(".").endswith(".localhost"):
        raise ValueError("source.url must have a public host")
    try:
        address = ip_address(host)
    except ValueError:
        return source
    if not is_public(address):
        raise ValueError("source.url must have a public host")
    return source


def validate_imported_service(service: dict[str, JsonValue]) -> None:
    name = service.get("name")
    if not isinstance(name, str) or not name.strip() or len(name) > 128:
        raise ValueError(
            "Imported Service name must be a nonempty string of at most 128 characters"
        )
    for field in (
        "description",
        "documentation_url",
        "language",
        "status_url",
        "support_url",
        "website_url",
    ):
        if field in service and not isinstance(service[field], str):
            raise ValueError(f"{field} must be a string")
    service.pop("operations", None)
    if "protocols" not in service:
        return
    protocols = _OBJECT.validate_python(service["protocols"])
    retained: dict[str, JsonValue] = {}
    for category, known, schema in (
        ("enrollment", {"aep"}, "enrollment-protocol.schema.json"),
        ("payments", {"mpp", "x402"}, "payment-protocol.schema.json"),
        ("trust", {"tap"}, "trust-protocol.schema.json"),
    ):
        if category not in protocols:
            continue
        values = protocols[category]
        if not isinstance(values, list) or not values:
            raise ValueError(f"protocols.{category} must be a nonempty array")
        selected: list[JsonValue] = []
        names: set[str] = set()
        for value in values:
            descriptor = _OBJECT.validate_python(value)
            name = descriptor.get("name")
            if not isinstance(name, str) or not name.strip() or len(name) > 128:
                raise ValueError(
                    "Protocol name must be a nonempty string of at most 128 characters"
                )
            if name not in known:
                continue
            if name in names:
                raise ValueError(f"Duplicate {category} descriptor")
            names.add(name)
            validate_value(descriptor, schema, category)
            selected.append(descriptor)
        if selected:
            retained[category] = selected
    service["protocols"] = retained
