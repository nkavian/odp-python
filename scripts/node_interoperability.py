#!/usr/bin/env python3
from __future__ import annotations

import asyncio
import sys

from offering_protocol.agent import ServiceClient
from offering_protocol.core import OfferingSearchRequest, PriceType


async def run(service_url: str) -> None:
    async with ServiceClient(service_url, allow_local_network=True) as client:
        inspection = await client.inspect()
        if inspection.document.name != "Small Example Store":
            raise RuntimeError(f"unexpected Service {inspection.document.name!r}")
        page = await client.list_offerings()
        identifiers = {offering.id for offering in page.items}
        expected = {"architecture-review", "incident-plan"}
        if not expected <= identifiers:
            raise RuntimeError(f"Offering list omitted {', '.join(sorted(expected - identifiers))}")
        details = await client.get_offering_details("incident-plan")
        if (
            details.offering.name != "Incident Response Plan"
            or details.offering.price is None
            or details.offering.price.price_type is not PriceType.FREE
        ):
            raise RuntimeError("full Offering did not match the Node.js example")
        resolved = await client.resolve_action("incident-plan", "download")
        expected_url = f"{service_url}/downloads/incident-plan.txt"
        if resolved.action.http is None or resolved.action.http.url != expected_url:
            raise RuntimeError("download Action did not resolve to the Node.js Service")


async def search(service_url: str) -> None:
    async with ServiceClient(service_url, allow_local_network=True) as client:
        page = await client.search_offerings(OfferingSearchRequest(query="gpu", limit=2))
        if [item.id for item in page.items] != ["gpu-00000000", "gpu-00000001"]:
            raise RuntimeError("Offering search did not match the Node.js marketplace")
        if not page.next:
            raise RuntimeError("Offering search did not return a continuation")
        next_page = await client.continue_offerings(page.next)
        if [item.id for item in next_page.items] != ["gpu-00000002", "gpu-00000003"]:
            raise RuntimeError("Offering search continuation did not retain its query and limit")
        if (await client.search_offerings(OfferingSearchRequest(query="plants"))).items:
            raise RuntimeError("Offering search ignored the query")


def main() -> None:
    if len(sys.argv) != 3 or sys.argv[2] not in {"catalog", "search"}:
        raise SystemExit("usage: node_interoperability.py SERVICE_URL catalog|search")
    asyncio.run((run if sys.argv[2] == "catalog" else search)(sys.argv[1]))
    print("Python Agent interoperates with the Node.js example Service")


if __name__ == "__main__":
    main()
