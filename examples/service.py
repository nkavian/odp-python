from __future__ import annotations

import asyncio
import os
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit

from offering_protocol.core import Collection, Offering
from offering_protocol.service import Request, ServiceBuilder, StaticCatalog, StaticCatalogOptions

catalog = StaticCatalog(
    StaticCatalogOptions(
        collections=(Collection(id="plants", name="Plants", odp_version="1.0"),),
        offerings=(
            Offering(
                collection_ids=["plants"],
                description="A resilient indoor plant with broad, glossy leaves.",
                id="rubber-plant",
                name="Rubber Plant",
                odp_version="1.0",
            ),
            Offering(
                collection_ids=["plants"],
                description="A low-maintenance plant with upright patterned leaves.",
                id="snake-plant",
                name="Snake Plant",
                odp_version="1.0",
            ),
        ),
    )
)

service = (
    ServiceBuilder(
        "Example Plant Store",
        "An ODP-enabled store for indoor plants.",
        "en",
        "/odp",
    )
    .keywords(["houseplants", "indoor-plants"])
    .build(catalog)
)


class Handler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        self._handle()

    def do_POST(self) -> None:
        self._handle()

    def _handle(self) -> None:
        parsed = urlsplit(self.path)
        length = int(self.headers.get("content-length", "0"))
        response = asyncio.run(
            service.handle(
                Request(
                    body=self.rfile.read(length),
                    headers={name: value for name, value in self.headers.items()},
                    method=self.command,
                    path=parsed.path,
                    query=parsed.query,
                )
            )
        )
        self.send_response(response.status)
        for name, value in response.headers.items():
            self.send_header(name, value)
        self.send_header("content-length", str(len(response.body)))
        self.end_headers()
        self.wfile.write(response.body)

    def log_message(self, format: str, *args: object) -> None:
        print(f"{self.command} {self.path} - {format % args}")


if __name__ == "__main__":
    port = int(os.environ.get("PORT", "4103"))
    server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    print(f"ODP Service listening at http://127.0.0.1:{port}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
