from __future__ import annotations

import asyncio
import sys

from offering_protocol.agent import ServiceClient
from offering_protocol.core import Operation


async def main() -> None:
    origin = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:4103"
    async with ServiceClient(origin, allow_local_network=True) as service:
        inspection = await service.inspect()
        operations = {item.name for item in inspection.document.operations}

        print("Service")
        print(f"  Name: {inspection.document.name}")
        print(f"  Description: {inspection.document.description}")
        print(f"  Origin: {inspection.service_origin}")
        print(f"  Operations: {', '.join(sorted(item.value for item in operations))}")

        if Operation.LIST_COLLECTIONS in operations:
            collections = await service.list_collections()
            print("\nCollections")
            for collection in collections.items:
                print(f"  {collection.id}: {collection.name}")

        offerings = await service.list_offerings()
        print("\nOfferings")
        for offering in offerings.items:
            print(f"  {offering.id}: {offering.name}")

        if offerings.items:
            details = await service.get_offering_details(offerings.items[0].id)
            print("\nFirst Offering")
            print(f"  Name: {details.offering.name}")
            print(f"  Description: {details.offering.description}")
            print(f"  Actions: {', '.join(action.id for action in details.actions) or 'None'}")
            for issue in details.issues:
                print(f"  Issue: {issue.message}")


asyncio.run(main())
