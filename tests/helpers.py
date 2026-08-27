from __future__ import annotations

from collections import deque

from offering_protocol.directory.transport import HttpRequest, HttpResponse


class QueueTransport:
    def __init__(self, *responses: HttpResponse) -> None:
        self.responses = deque(responses)
        self.requests: list[HttpRequest] = []

    async def send(self, request: HttpRequest) -> HttpResponse:
        self.requests.append(request)
        return self.responses.popleft()

    async def aclose(self) -> None:
        return None


def response(
    body: bytes | str,
    *,
    content_type: str = "application/odp+json",
    headers: dict[str, str] | None = None,
    status: int = 200,
) -> HttpResponse:
    values = {"content-type": content_type, **(headers or {})}
    return HttpResponse(
        status=status, headers=values, body=body.encode() if isinstance(body, str) else body
    )


SERVICE_DOCUMENT = """{
  "description":"An AI-enabled plant store.",
  "http":{"endpoint_base":"/odp"},
  "language":"en",
  "localizations":["en"],
  "name":"Indica Flowers",
  "odp_version":"1.0",
  "operations":[
    {"authentication":"not-required","name":"get-offering"},
    {"authentication":"not-required","name":"list-offerings"}
  ],
  "protocols":{"trust":[{"name":"tap"}]}
}"""

OFFERING = """{
  "description":"A resilient indoor plant.",
  "id":"rubber-plant",
  "name":"Rubber Plant",
  "odp_version":"1.0"
}"""

OFFERING_PAGE = f'{{"items":[{OFFERING}],"odp_version":"1.0"}}'
