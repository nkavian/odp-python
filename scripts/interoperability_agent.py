#!/usr/bin/env python3
from __future__ import annotations

import asyncio
import sys

from offering_protocol.agent import ServiceClient


async def run(service_url: str) -> None:
    async with ServiceClient(service_url, allow_local_network=True) as client:
        inspection = await client.inspect()
        if not inspection.document.name:
            raise RuntimeError("Service name is empty")
        page = await client.list_offerings()
        if not page.items:
            raise RuntimeError("Service returned no Offerings")
        first = page.items[0]
        details = await client.get_offering_details(first.id)
        if details.offering.id != first.id or details.offering.name != first.name:
            raise RuntimeError("full Offering does not match its listed summary")
        if details.actions:
            resolved = await client.resolve_action(first.id, details.actions[0].id)
            if resolved.action.id != details.actions[0].id:
                raise RuntimeError("resolved Action identifier changed")


def main() -> None:
    if len(sys.argv) != 2:
        raise SystemExit("usage: interoperability_agent.py SERVICE_URL")
    asyncio.run(run(sys.argv[1]))
    print("Python Agent interoperates with the ODP Service")


if __name__ == "__main__":
    main()
