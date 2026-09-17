"""Service conformance: what a Service puts on the wire, beyond the documents it serves.

The document rules live in core. What a Service owns is the exchange around them -- which variant it
selected and how it said so, the validator it issued, the methods each resource answers, and what it
refuses before a Catalog is ever asked. Each test states one of those rules.
"""

from __future__ import annotations

import json

import pytest

from offering_protocol.core import (
    Collection,
    CollectionSearchRequest,
    Offering,
    OfferingPage,
    OfferingSearchRequest,
    Operation,
    Page,
)
from offering_protocol.service import (
    MEDIA_TYPE,
    PROBLEM_MEDIA_TYPE,
    CatalogRequest,
    Request,
    Response,
    Service,
    ServiceBuilder,
    StaticCatalog,
    StaticCatalogOptions,
)

TAGS = ["en", "en-GB", "fr", "de-CH", "zh-Hant"]


def _offering(identifier: str, *, collected: bool = False) -> Offering:
    document: dict[str, object] = {
        "id": identifier,
        "name": identifier.title(),
        "odp_version": "1.0",
        "description": f"A plant called {identifier}.",
    }
    if collected:
        document["collection_ids"] = ["plants"]
    return Offering.model_validate(document)


def _catalog(count: int = 3) -> StaticCatalog:
    return StaticCatalog(
        StaticCatalogOptions(
            collections=(
                Collection.model_validate({"id": "plants", "name": "Plants", "odp_version": "1.0"}),
            ),
            offerings=tuple(_offering(f"p{index}", collected=True) for index in range(count)),
        )
    )


def service(*, localizations: list[str] | None = None, count: int = 3) -> Service:
    builder = ServiceBuilder("Plants", "A plant store.", "en", "/odp")
    if localizations is not None:
        builder = builder.localizations(localizations)
    return builder.build(_catalog(count))


async def call(method: str, path: str, **kwargs: object) -> Response:
    return await service().handle(Request(method=method, path=path, **kwargs))  # type: ignore[arg-type]


async def localized(accept_language: str, path: str = "/.well-known/odp") -> Response:
    return await service(localizations=TAGS).handle(
        Request(method="GET", path=path, headers={"accept-language": accept_language})
    )


EVERY_PATH = (
    "/.well-known/odp",
    "/odp/offerings",
    "/odp/offerings/p0",
    "/odp/collections",
    "/odp/collections/plants",
    "/odp/collections/plants/offerings",
)


# -- which variant it served ---------------------------------------------------


@pytest.mark.asyncio
async def test_selects_a_language_by_rfc_4647_lookup() -> None:
    """SVC-58: Lookup walks a range down its own subtags and never sideways."""
    for accept, expected in (
        ("fr", "fr"),
        ("FR", "fr"),
        ("en-GB", "en-GB"),
        # Lookup truncates: en-GB-oed has no match, en-GB does.
        ("en-GB-oed", "en-GB"),
        # de-CH-1901 falls back to de-CH, never sideways to another de-* tag.
        ("de-CH-1901", "de-CH"),
        ("zh-Hant-TW", "zh-Hant"),
        # A single-character subtag is an extension singleton, dropped with its parent.
        ("en-GB-a-bbb", "en-GB"),
    ):
        assert (await localized(accept)).headers["content-language"] == expected, accept


@pytest.mark.asyncio
async def test_never_refuses_a_request_over_language() -> None:
    """SVC-59: nothing matching is not a reason to refuse, only to serve the default."""
    for accept in ("de-DE", "ja", "*", "ja, ko;q=0.5"):
        reply = await localized(accept, "/odp/offerings")
        assert reply.status == 200, accept
        assert reply.headers["content-language"] == "en", accept


@pytest.mark.asyncio
async def test_honours_the_order_the_agent_asked_in() -> None:
    for accept, expected in (
        ("fr;q=0.5, de-CH;q=0.9", "de-CH"),
        ("de-CH;q=0.1, fr", "fr"),
        ("fr, de-CH", "fr"),
        # RFC 9110: a range weighted zero is not wanted at all.
        ("fr;q=0, de-CH", "de-CH"),
        ("fr;q=0, de-CH;q=0", "en"),
        # `*` is the residual, so it cannot outrank a range the caller named.
        ("fr, *;q=0.9", "fr"),
        # A weight outside the grammar leaves the entry with nothing to honour.
        ("fr;q=nonsense", "en"),
    ):
        assert (await localized(accept)).headers["content-language"] == expected, accept


@pytest.mark.asyncio
async def test_describes_the_variant_it_served_on_every_representation() -> None:
    """SVC-60: `Vary` is what stops a shared cache handing one variant to another request."""
    for path in EVERY_PATH:
        reply = await call("GET", path)
        assert reply.headers["content-language"] == "en", path
        assert reply.headers["vary"] == "Accept, Accept-Language", path
        assert reply.headers["content-type"] == MEDIA_TYPE, path


@pytest.mark.asyncio
async def test_tells_the_catalog_which_variant_to_answer_in() -> None:
    """A Catalog is handed the selected tag rather than being left to parse the field itself."""
    seen: list[CatalogRequest] = []

    class Recording(StaticCatalog):
        async def list_offerings(self, request: CatalogRequest) -> OfferingPage[Offering]:
            seen.append(request)
            return await super().list_offerings(request)

    built = ServiceBuilder("Plants", "A plant store.", "en", "/odp")
    built = built.localizations(TAGS)
    await built.build(Recording(StaticCatalogOptions(offerings=(_offering("p0"),)))).handle(
        Request(method="GET", path="/odp/offerings", headers={"accept-language": "de-CH-1901, fr"})
    )

    assert seen[0].language == "de-CH"
    assert seen[0].accept_language == "de-CH-1901, fr"


# -- the validator it issued ---------------------------------------------------


@pytest.mark.asyncio
async def test_tags_every_representation_it_serves() -> None:
    """SVC-61: a representation without a validator cannot be revalidated, only re-fetched."""
    for path in EVERY_PATH:
        etag = (await call("GET", path)).headers["etag"]
        assert etag.startswith('"') and etag.endswith('"'), path
        assert len(etag) > 2, path


@pytest.mark.asyncio
async def test_gives_each_variant_a_tag_of_its_own() -> None:
    """SVC-61 again: two languages sharing one tag is the one thing a validator must not do."""
    english = await localized("en")
    french = await localized("fr")

    assert english.headers["etag"] != french.headers["etag"]


@pytest.mark.asyncio
async def test_gives_one_representation_one_tag() -> None:
    first = await call("GET", "/odp/offerings")
    second = await call("GET", "/odp/offerings")

    assert first.headers["etag"] == second.headers["etag"]


@pytest.mark.asyncio
async def test_distinguishes_terse_from_full() -> None:
    """Two representations of one resource are two variants, and carry two validators.

    A Terse Offering omits the Actions a Full one carries, so the two differ by exactly the members
    the representation rules say they should.
    """
    actionable = Offering.model_validate(
        {
            "id": "p0",
            "name": "P0",
            "odp_version": "1.0",
            "actions": [
                {
                    "authentication": "not-required",
                    "id": "buy",
                    "rel": "purchase",
                    "http": {"href": "https://plants.example/checkout", "method": "POST"},
                }
            ],
        }
    )
    built = ServiceBuilder("Plants", "A plant store.", "en", "/odp").build(
        StaticCatalog(StaticCatalogOptions(offerings=(actionable,)))
    )
    terse = await built.handle(Request(method="GET", path="/odp/offerings/p0"))
    full = await built.handle(
        Request(method="GET", path="/odp/offerings/p0", query="representation=full")
    )

    assert b"actions" not in terse.body
    assert b"actions" in full.body
    assert terse.headers["etag"] != full.headers["etag"]


@pytest.mark.asyncio
async def test_answers_a_conditional_request_that_still_matches() -> None:
    """PAG-31: a validator that still matches means the Agent already holds this representation."""
    built = service()
    first = await built.handle(Request(method="GET", path="/odp/offerings"))
    etag = first.headers["etag"]

    second = await built.handle(
        Request(method="GET", path="/odp/offerings", headers={"if-none-match": etag})
    )

    assert second.status == 304
    assert second.body == b""
    assert second.headers["etag"] == etag
    assert "content-type" not in second.headers


@pytest.mark.asyncio
async def test_reads_every_form_a_conditional_field_takes() -> None:
    """RFC 9110 compares `If-None-Match` weakly, so `W/"x"` matches the strong `"x"` served here."""
    built = service()
    etag = (await built.handle(Request(method="GET", path="/odp/offerings"))).headers["etag"]

    for value in (etag, f"W/{etag}", f'"other", {etag}', "*"):
        reply = await built.handle(
            Request(method="GET", path="/odp/offerings", headers={"if-none-match": value})
        )
        assert reply.status == 304, value


@pytest.mark.asyncio
async def test_serves_a_conditional_request_that_no_longer_matches() -> None:
    reply = await call("GET", "/odp/offerings", headers={"if-none-match": '"something-else"'})

    assert reply.status == 200
    assert reply.body


@pytest.mark.asyncio
async def test_refuses_a_matched_precondition_on_a_method_that_is_not_a_retrieval() -> None:
    """RFC 9110 13.1.2: a matched `If-None-Match` is 304 for GET and HEAD, 412 for anything else."""

    class Searchable(StaticCatalog):
        def operations(self) -> list[Operation]:
            return [*super().operations(), Operation.SEARCH_OFFERINGS]

        async def search_offerings(
            self, query: OfferingSearchRequest, request: CatalogRequest
        ) -> OfferingPage[Offering]:
            del query
            return await self.list_offerings(request)

    built = ServiceBuilder("Plants", "A plant store.", "en", "/odp").build(
        Searchable(StaticCatalogOptions(offerings=(_offering("p0"),)))
    )
    reply = await built.handle(
        Request(
            method="POST",
            path="/odp/offerings/search",
            body=b'{"odp_version":"1.0","query":"plant"}',
            headers={"content-type": MEDIA_TYPE, "if-none-match": "*"},
        )
    )

    assert reply.status == 412


# -- the methods each resource answers -----------------------------------------


@pytest.mark.asyncio
async def test_answers_head_wherever_it_answers_get() -> None:
    """RFC 9110 9.3.2: HEAD is GET without the body, so refusing it breaks every cache probe."""
    for path in EVERY_PATH:
        get = await call("GET", path)
        head = await call("HEAD", path)

        assert head.status == 200, path
        assert head.body == b"", path
        assert head.headers["etag"] == get.headers["etag"], path


@pytest.mark.asyncio
async def test_answers_a_conditional_head() -> None:
    built = service()
    etag = (await built.handle(Request(method="GET", path="/odp/offerings"))).headers["etag"]
    reply = await built.handle(
        Request(method="HEAD", path="/odp/offerings", headers={"if-none-match": etag})
    )

    assert reply.status == 304
    assert reply.body == b""


@pytest.mark.asyncio
async def test_names_the_methods_a_refused_one_should_have_been() -> None:
    """RFC 9110 15.5.6: a 405 that does not say what is allowed leaves the caller guessing."""
    for method, path, allow in (
        ("POST", "/.well-known/odp", "GET, HEAD"),
        ("DELETE", "/odp/offerings", "GET, HEAD"),
        ("PUT", "/odp/collections", "GET, HEAD"),
    ):
        reply = await call(method, path)
        assert reply.status == 405, path
        assert reply.headers["allow"] == allow, path


@pytest.mark.asyncio
async def test_says_a_search_path_wants_post_rather_than_that_it_does_not_exist() -> None:
    """A reserved path names an operation, not a resource.

    Reading `/offerings/search` as a request for an Offering called "search" answers 404, which
    tells an Agent the operation is unavailable rather than that it uses a different method.
    """
    for path in ("/odp/offerings/search", "/odp/collections/search"):
        reply = await call("GET", path)
        assert reply.status == 405, path
        assert reply.headers["allow"] == "POST", path


# -- what it refuses before a Catalog is asked ----------------------------------


@pytest.mark.asyncio
async def test_refuses_a_repeated_representation() -> None:
    """SVC-73: collapsing repeats honoured whichever copy came last.

    `representation=terse&representation=full` then served a Full Representation to a request that
    had also asked for a Terse one.
    """
    for query in (
        "representation=terse&representation=full",
        "representation=full&representation=full",
        "limit=1&limit=2",
        "cursor=a&cursor=b",
    ):
        reply = await call("GET", "/odp/offerings", query=query)
        assert reply.status == 400, query
        assert b"must not be repeated" in reply.body, query


@pytest.mark.asyncio
async def test_refuses_a_representation_or_limit_it_cannot_honour() -> None:
    for query in ("representation=sideways", "limit=101", "limit=-1", "limit=many"):
        assert (await call("GET", "/odp/offerings", query=query)).status == 400, query

    for query in ("representation=full", "representation=terse", "limit=100", "limit=0"):
        assert (await call("GET", "/odp/offerings", query=query)).status == 200, query


@pytest.mark.asyncio
async def test_refuses_an_identifier_that_names_no_resource_it_could_hold() -> None:
    """SVC-66 substitutes an identifier verbatim, so a segment that is not an LRI names nothing.

    Letting one through makes the Catalog decide what `..` means, which is not a question a Catalog
    should have to answer.
    """
    for path in (
        "/odp/offerings/..",
        "/odp/offerings/a%2Fb",
        "/odp/collections/..",
        "/odp/collections/.",
        "/odp/collections//offerings",
        "/odp/collections/a b/offerings",
    ):
        reply = await call("GET", path)
        assert reply.status == 400, path
        assert b"identifier is invalid" in reply.body, path


@pytest.mark.asyncio
async def test_refuses_an_accept_that_excludes_odp() -> None:
    """MED-04, and RFC 9110 12.4.2: naming ODP with zero weight excludes it just as surely."""
    for accept in ("text/html", "application/json", "application/odp+json;q=0", "*/*;q=0"):
        reply = await call("GET", "/odp/offerings", headers={"accept": accept})
        assert reply.status == 406, accept
        assert reply.headers["content-type"] == PROBLEM_MEDIA_TYPE, accept


@pytest.mark.asyncio
async def test_serves_an_accept_that_allows_odp() -> None:
    """MED-03: a wildcard media range covers this media type, and `application/*` is one."""
    for accept in (
        "application/odp+json",
        "application/*",
        "*/*",
        "text/html;q=0.9, application/odp+json",
        "application/odp+json;q=0.5",
    ):
        assert (await call("GET", "/odp/offerings", headers={"accept": accept})).status == 200, (
            accept
        )


@pytest.mark.asyncio
async def test_refuses_a_search_body_it_cannot_read() -> None:
    """MED-06: a request body for an ODP operation carries the ODP media type."""

    class Searchable(StaticCatalog):
        def operations(self) -> list[Operation]:
            return [*super().operations(), Operation.SEARCH_COLLECTIONS]

        async def search_collections(
            self, query: CollectionSearchRequest, request: CatalogRequest
        ) -> Page[Collection]:
            del query
            return await self.list_collections(request)

    built = ServiceBuilder("Plants", "A plant store.", "en", "/odp").build(
        Searchable(
            StaticCatalogOptions(
                collections=(
                    Collection.model_validate(
                        {"id": "plants", "name": "Plants", "odp_version": "1.0"}
                    ),
                ),
                offerings=(_offering("p0"),),
            )
        )
    )
    body = b'{"odp_version":"1.0","query":"plants"}'

    for content_type, status in (
        (MEDIA_TYPE, 200),
        ("application/json", 415),
        ("", 415),
        ("text/plain", 415),
    ):
        reply = await built.handle(
            Request(
                method="POST",
                path="/odp/collections/search",
                body=body,
                headers={"content-type": content_type},
            )
        )
        assert reply.status == status, content_type

    oversized = await built.handle(
        Request(
            method="POST",
            path="/odp/collections/search",
            body=b" " * 70_000,
            headers={"content-type": MEDIA_TYPE},
        )
    )
    assert oversized.status == 413


@pytest.mark.asyncio
async def test_refuses_an_operation_it_does_not_advertise() -> None:
    """ROLE-01: an unadvertised operation does not exist, whatever the path looks like."""
    reply = await call("GET", "/odp/collections")
    assert reply.status == 200

    without = ServiceBuilder("Plants", "A plant store.", "en", "/odp").build(
        StaticCatalog(StaticCatalogOptions(offerings=(_offering("p0"),)))
    )
    assert (await without.handle(Request(method="GET", path="/odp/collections"))).status == 404


@pytest.mark.asyncio
async def test_refuses_a_path_outside_its_endpoint_base() -> None:
    for path in ("/elsewhere", "/odp/baskets", "/odp"):
        assert (await call("GET", path)).status == 404, path


# -- the problems it reports -----------------------------------------------------


@pytest.mark.asyncio
async def test_reports_a_problem_whose_title_names_the_type_not_the_occurrence() -> None:
    """RFC 9457: a title summarises the problem *type* and does not vary between occurrences.

    Setting it to the detail made every 404 a different "type" to anything grouping by title.
    """
    missing = json.loads((await call("GET", "/odp/offerings/absent")).body)
    invalid = json.loads((await call("GET", "/odp/offerings/..")).body)

    assert missing["title"] == "Not found"
    assert missing["detail"] == "Offering not found"
    assert missing["type"] == "https://offeringprotocol.org/problems/not-found"
    assert invalid["title"] == "Invalid request"
    assert invalid["title"] != invalid["detail"]


@pytest.mark.asyncio
async def test_reports_every_problem_with_the_problem_media_type() -> None:
    for method, path in (("GET", "/odp/offerings/absent"), ("DELETE", "/odp/offerings")):
        reply = await call(method, path)
        assert reply.headers["content-type"] == PROBLEM_MEDIA_TYPE, path
        assert json.loads(reply.body)["status"] == reply.status, path


# -- pagination --------------------------------------------------------------------


@pytest.mark.asyncio
async def test_keeps_a_continuation_in_the_variant_that_produced_it() -> None:
    """A page served in one language is a different page from the same offsets served in another.

    The cursor carries the selected language for the same reason it carries the representation: a
    continuation must not quietly change variant part-way through a sequence.
    """
    built = service(localizations=TAGS, count=5)
    first = await built.handle(
        Request(
            method="GET",
            path="/odp/offerings",
            query="limit=2",
            headers={"accept-language": "fr"},
        )
    )
    query = json.loads(first.body)["next"].split("?", 1)[1]

    same = await built.handle(
        Request(method="GET", path="/odp/offerings", query=query, headers={"accept-language": "fr"})
    )
    other = await built.handle(
        Request(method="GET", path="/odp/offerings", query=query, headers={"accept-language": "en"})
    )

    assert same.status == 200
    assert other.status == 410


@pytest.mark.asyncio
async def test_walks_a_sequence_to_its_end() -> None:
    built = service(count=5)
    seen: list[str] = []
    query = "limit=2"
    while True:
        reply = await built.handle(Request(method="GET", path="/odp/offerings", query=query))
        page = json.loads(reply.body)
        seen.extend(item["id"] for item in page["items"])
        if not page.get("next"):
            break
        query = page["next"].split("?", 1)[1]

    assert seen == ["p0", "p1", "p2", "p3", "p4"]


@pytest.mark.asyncio
async def test_refuses_a_continuation_it_did_not_issue() -> None:
    for cursor in ("nonsense", "a.b", "YQ.Yg"):
        reply = await call("GET", "/odp/offerings", query=f"cursor={cursor}")
        assert reply.status == 410, cursor
        assert json.loads(reply.body)["code"] == "CONTINUATION_UNAVAILABLE", cursor


@pytest.mark.asyncio
async def test_reads_a_media_range_carrying_parameters_other_than_weight() -> None:
    """A parameter that is not `q` says nothing about whether the range is acceptable."""
    for accept in ("application/odp+json;charset=utf-8", "application/odp+json;version=1;q=0.8"):
        assert (await call("GET", "/odp/offerings", headers={"accept": accept})).status == 200, (
            accept
        )


@pytest.mark.asyncio
async def test_serves_the_first_variant_the_residual_range_does_not_exclude() -> None:
    """RFC 9110 12.5.4: `*` matches every tag no other range matched, minus the ones refused.

    So `*` beside `en;q=0` asks for anything except English, and the Service answers with the first
    localization that survives rather than with its default. A refused range covers its subtags the
    way a basic range does, so refusing `en` refuses `en-GB` with it.
    """
    assert (await localized("*, en;q=0")).headers["content-language"] == "fr"
    assert (await localized("*, fr;q=0, de-CH;q=0")).headers["content-language"] == "en"
    assert (await localized("*, en;q=0, fr;q=0")).headers["content-language"] == "de-CH"


@pytest.mark.asyncio
async def test_falls_back_when_the_residual_range_excludes_everything() -> None:
    """Refusing every variant still is not a reason to refuse the request (SVC-59)."""
    refused = ", ".join(f"{tag};q=0" for tag in TAGS)
    reply = await localized(f"*, {refused}")

    assert reply.status == 200
    assert reply.headers["content-language"] == "en"
