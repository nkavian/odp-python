"""Discover mixed Directory results and retrieve anonymous Collection details."""

from __future__ import annotations

import argparse
import asyncio

from offering_protocol.agent import ServiceClient
from offering_protocol.core import AuthenticationRequirement, Operation
from offering_protocol.directory import (
    CollectionResult,
    DirectoryClient,
    Environment,
    ResourceSearchRequest,
    ServiceResult,
    UnknownResult,
)


async def discover(environment: Environment, query: str) -> None:
    async with DirectoryClient(environment) as directory:
        response = await directory.search(ResourceSearchRequest(query=query, limit=5))
    for issue in response.issues:
        print(f"Skipped result {issue.index}: {issue.message}")
    for item in response.items:
        if isinstance(item, ServiceResult):
            print(f"Service: {item.service.name} ({item.service.service_origin})")
        elif isinstance(item, CollectionResult):
            print(
                f"Collection: {item.collection.name} "
                f"({item.collection.id}, through {item.service.service_origin})"
            )
            async with ServiceClient(item.service.service_origin) as service:
                inspection = await service.inspect()
                if any(
                    operation.name == Operation.GET_COLLECTION
                    and operation.authentication != AuthenticationRequirement.REQUIRED
                    for operation in inspection.document.operations
                ):
                    collection = await service.get_collection(item.collection.id)
                    print(collection.model_dump_json(indent=2))
                else:
                    print("The Service does not advertise anonymous Collection retrieval.")
        elif isinstance(item, UnknownResult):
            print(f"Unsupported result type: {item.type}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("environment", choices=["production", "sandbox"])
    parser.add_argument("query", nargs="*", help="Omit to browse indexed results")
    args = parser.parse_args()
    asyncio.run(discover(Environment(args.environment), " ".join(args.query)))
