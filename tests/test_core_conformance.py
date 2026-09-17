"""Core conformance: the document rules a JSON Schema cannot state.

The schemas carry every rule about one member in isolation. What is left to code is the rules that
compare one member against another -- a repeated identifier, a range that runs backwards, a unit on
something that has no dimension. Each test below states one of those, with a control alongside it so
a rejection can be read as a consequence of the one thing that changed.
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from offering_protocol.core import (
    OdpValidationError,
    parse_agent_collection,
    parse_agent_offering,
    parse_agent_offering_page,
    parse_collection,
    parse_collection_search_request,
    parse_filter_definition,
    parse_offering,
    parse_offering_page,
    parse_sort_definition,
)

OFFERING: dict[str, Any] = {"id": "plant-1", "name": "Monstera", "odp_version": "1.0"}
COLLECTION: dict[str, Any] = {"id": "plants", "name": "Plants", "odp_version": "1.0"}


def amend(base: dict[str, Any], **changes: Any) -> str:
    return json.dumps({**base, **changes})


def action(identifier: str) -> dict[str, Any]:
    return {
        "authentication": "not-required",
        "id": identifier,
        "rel": "purchase",
        "http": {"href": "https://plants.example/checkout", "method": "POST"},
    }


def keywords_of(error: OdpValidationError) -> list[str]:
    return [issue.keyword for issue in error.issues]


def assert_rejected_for(body: str, parse: Any, keyword: str) -> None:
    with pytest.raises(OdpValidationError) as raised:
        parse(body)
    assert keyword in keywords_of(raised.value), keywords_of(raised.value)


# -- Offerings ------------------------------------------------------------------


def test_refuses_a_repeated_action_identifier() -> None:
    """OFR-57: a repeat leaves a caller unable to say which Action it meant."""
    assert parse_offering(amend(OFFERING, actions=[action("buy"), action("rent")])).actions
    assert_rejected_for(
        amend(OFFERING, actions=[action("buy"), action("buy")]), parse_offering, "unique-action-id"
    )


def _range(minimum: str, maximum: str) -> str:
    return amend(
        OFFERING,
        price={"type": "range", "currency": "USD", "minimum": minimum, "maximum": maximum},
    )


def test_refuses_an_inverted_price_range() -> None:
    """OFR-49: a range whose minimum is above its maximum describes no price at all."""
    assert parse_offering(_range("5.00", "99.00")).price is not None
    assert_rejected_for(_range("99.00", "5.00"), parse_offering, "price-range")
    assert_rejected_for(_range("10", "9.99"), parse_offering, "price-range")


def test_orders_a_price_range_numerically() -> None:
    """OFR-48: a price is a decimal string, so its bounds order numerically, not lexically."""
    # Lexically "9.00" sorts after "10.00"; numerically it does not.
    assert parse_offering(_range("9.00", "10.00"))
    # Trailing and leading zeros do not change a value, so these bounds are equal.
    # A leading zero is not an ODP decimal at all, so the schema refuses it before any comparison.
    with pytest.raises(OdpValidationError):
        parse_offering(_range("07", "7"))
    for minimum, maximum in (("5.0", "5.00"), ("0", "0.000"), ("1.5", "1.50")):
        assert parse_offering(_range(minimum, maximum)), (minimum, maximum)
    assert_rejected_for(_range("1.51", "1.5"), parse_offering, "price-range")


def test_leaves_a_price_without_bounds_alone() -> None:
    for price in (
        {"type": "free"},
        {"type": "quote"},
        {"type": "fixed", "amount": "39.00", "currency": "USD"},
        {"type": "metered", "amount": "0.10", "currency": "USD", "unit": "litre"},
    ):
        assert parse_offering(amend(OFFERING, price=price)), price


# -- Collections ----------------------------------------------------------------


def test_refuses_a_collection_that_parents_itself() -> None:
    """COL-20: a one-node cycle, which nothing walking the hierarchy upwards escapes."""
    assert parse_collection(amend(COLLECTION, parent_ids=["garden"])).parent_ids
    for parents in (["plants"], ["garden", "plants"]):
        assert_rejected_for(amend(COLLECTION, parent_ids=parents), parse_collection, "self-parent")


def test_refuses_a_repeated_parent() -> None:
    """COL-19: a Collection names each parent once."""
    with pytest.raises(OdpValidationError):
        parse_collection(amend(COLLECTION, parent_ids=["garden", "garden"]))


# -- Filter Definitions -----------------------------------------------------------


def _filter(filter_type: str, **changes: Any) -> str:
    return json.dumps(
        {
            "id": "weight",
            "title": "Weight",
            "description": "How heavy the plant is.",
            "type": filter_type,
            "operators": ["eq"],
            **changes,
        }
    )


def test_refuses_a_unit_on_a_filter_that_measures_nothing() -> None:
    """FLT-10: only a numeric Filter has a dimension for a unit to name."""
    unit = {"system": "ucum", "code": "kg"}
    for filter_type in ("boolean", "date", "date-time", "string"):
        assert_rejected_for(_filter(filter_type, unit=unit), parse_filter_definition, "unit-type")
    for filter_type in ("decimal", "integer", "number"):
        assert parse_filter_definition(_filter(filter_type, unit=unit)), filter_type


def test_accepts_every_filter_type_without_a_unit() -> None:
    for filter_type in (
        "boolean",
        "date",
        "date-time",
        "decimal",
        "integer",
        "number",
        "string",
    ):
        assert parse_filter_definition(_filter(filter_type)), filter_type


def test_refuses_an_ordering_operator_on_an_unordered_type() -> None:
    """FLT-11: an ordering operator on a type with no order cannot be evaluated."""
    for filter_type in ("boolean", "string"):
        for operator in ("gt", "gte", "lt", "lte"):
            assert_rejected_for(
                _filter(filter_type, operators=["eq", operator]),
                parse_filter_definition,
                "operator-type",
            )
    for filter_type in ("date", "date-time", "decimal", "integer", "number"):
        assert parse_filter_definition(_filter(filter_type, operators=["gt", "lte"])), filter_type


# -- Sort Definitions -------------------------------------------------------------


def _sort(*filter_ids: str) -> str:
    return json.dumps(
        {
            "id": "cheapest",
            "title": "Cheapest first",
            "description": "Orders plants by price.",
            "keys": [
                {"filter_id": filter_id, "direction": "ascending", "missing": "last"}
                for filter_id in filter_ids
            ],
        }
    )


def test_refuses_a_recipe_that_orders_by_one_filter_twice() -> None:
    """FLT-41: ordering by one Filter twice cannot change the order, so a repeat means nothing."""
    assert parse_sort_definition(_sort("price", "weight")).keys
    assert_rejected_for(_sort("price", "price"), parse_sort_definition, "unique-filter-id")
    assert_rejected_for(
        _sort("price", "weight", "price"), parse_sort_definition, "unique-filter-id"
    )


# -- Refinements -------------------------------------------------------------------


def _page(*groups: dict[str, Any]) -> str:
    document: dict[str, Any] = {"odp_version": "1.0", "items": []}
    if groups:
        document["refinements"] = list(groups)
    return json.dumps(document)


def _group(filter_id: str, *values: dict[str, Any]) -> dict[str, Any]:
    return {"filter_id": filter_id, "values": list(values)}


def test_refuses_two_refinement_groups_for_one_filter() -> None:
    """FLT-30: a repeat leaves an Agent unable to say which group belongs to that definition."""
    assert parse_offering_page(
        _page(
            _group("colour", {"value": "red", "count": 1}),
            _group("size", {"value": "l", "count": 2}),
        )
    )
    assert_rejected_for(
        _page(
            _group("colour", {"value": "red", "count": 1}),
            _group("colour", {"value": "blue", "count": 2}),
        ),
        parse_offering_page,
        "unique-filter-id",
    )


def test_refuses_a_repeated_bucket_value() -> None:
    """FLT-32: `uniqueItems` compares whole buckets, so it passes one value with two counts."""
    for values in (
        ({"value": "green", "count": 4}, {"value": "green", "count": 2}),
        ({"value": True, "count": 4}, {"value": True, "count": 2}),
        ({"value": 3, "count": 4}, {"value": 3.0, "count": 2}),
    ):
        assert_rejected_for(
            _page(_group("colour", *values)), parse_offering_page, "unique-bucket-value"
        )


def test_reads_two_spellings_of_one_decimal_as_one_bucket_value() -> None:
    """FLT-32: decimal equality is numeric rather than lexical.

    So `1.0` and `1.00` name one value, and a group offering both hands a caller two counts for one
    candidate with no way to choose between them.
    """
    for values in (
        ({"value": "1.0", "count": 4}, {"value": "1.00", "count": 2}),
        ({"value": "0", "count": 4}, {"value": "0.0", "count": 2}),
        ({"value": "12", "count": 4}, {"value": "12.000", "count": 2}),
        ({"value": "-1.5", "count": 4}, {"value": "-1.50", "count": 2}),
    ):
        assert_rejected_for(
            _page(_group("weight", *values)), parse_offering_page, "unique-bucket-value"
        )


def test_keeps_bucket_values_that_differ_apart() -> None:
    for values in (
        ({"value": "1.0", "count": 4}, {"value": "2.0", "count": 2}),
        ({"value": "1.01", "count": 4}, {"value": "1.1", "count": 2}),
        ({"value": "10", "count": 4}, {"value": "1.0", "count": 2}),
        ({"value": "-1.0", "count": 4}, {"value": "1.0", "count": 2}),
        # Neither is an ODP decimal -- a leading zero and a trailing period are not -- so both are
        # compared as the strings they are.
        ({"value": "01", "count": 4}, {"value": "1", "count": 2}),
        ({"value": "1.", "count": 4}, {"value": "1", "count": 2}),
        ({"value": True, "count": 4}, {"value": False, "count": 2}),
        ({"value": 1, "count": 4}, {"value": "1", "count": 2}),
    ):
        assert parse_offering_page(_page(_group("weight", *values))), values


def test_reports_the_group_a_repeated_bucket_value_was_in() -> None:
    with pytest.raises(OdpValidationError) as raised:
        parse_offering_page(
            _page(
                _group("colour", {"value": "red", "count": 1}),
                _group("size", {"value": "l", "count": 1}, {"value": "l", "count": 2}),
            )
        )

    assert [issue.path for issue in raised.value.issues] == ["/refinements/1/values"]


# -- what an Agent tolerates ---------------------------------------------------------


def test_hands_an_agent_a_document_a_service_must_not_publish() -> None:
    """ROLE-03: an Agent describes a defect to its caller rather than discarding what it can use.

    The invariants above are what a Service must satisfy before publishing. On the Agent side they
    are reported against the Action, hierarchy or group they concern, so the document survives.
    """
    offering = amend(OFFERING, actions=[action("buy"), action("buy")])
    with pytest.raises(OdpValidationError):
        parse_offering(offering)
    assert len(parse_agent_offering(offering).actions) == 2

    collection = amend(COLLECTION, parent_ids=["plants"])
    with pytest.raises(OdpValidationError):
        parse_collection(collection)
    assert parse_agent_collection(collection).parent_ids == ["plants"]

    page = _page(
        _group("colour", {"value": "red", "count": 1}),
        _group("colour", {"value": "blue", "count": 2}),
    )
    with pytest.raises(OdpValidationError):
        parse_offering_page(page)
    assert len(parse_agent_offering_page(page).refinements) == 2


def test_still_refuses_an_agent_document_it_cannot_read_at_all() -> None:
    """Tolerance stops at defects that leave nothing to use."""
    for parse in (parse_agent_offering, parse_agent_collection):
        with pytest.raises(OdpValidationError):
            parse("not json")
        with pytest.raises(OdpValidationError):
            parse("{}")
    with pytest.raises(OdpValidationError):
        parse_agent_offering(amend(OFFERING, language="not a tag", localizations=["not a tag"]))


def test_filters_what_a_later_odp_version_added_before_reading_it() -> None:
    """The Agent entry points normalize first, so a member ODP does not define never reaches the
    schema."""
    unknown = amend(OFFERING, price={"type": "auction", "reserve": "10.00"})

    with pytest.raises(OdpValidationError):
        parse_offering(unknown)
    assert parse_agent_offering(unknown).price is None


# -- a null parent asks a question an absent one does not -------------------------------


def test_tells_a_null_parent_apart_from_an_absent_one() -> None:
    """COL-06: a null `parent_id` selects root Collections; an absent one applies no constraint."""
    rooted = parse_collection_search_request('{"odp_version":"1.0","query":"x","parent_id":null}')
    anywhere = parse_collection_search_request('{"odp_version":"1.0","query":"x"}')
    named = parse_collection_search_request('{"odp_version":"1.0","parent_id":"garden"}')

    assert rooted.parent_id is None and "parent_id" in rooted.model_fields_set
    assert anywhere.parent_id is None and "parent_id" not in anywhere.model_fields_set
    assert named.parent_id == "garden"

    # And the difference survives the round trip, which is what a Service reads it back from.
    assert rooted.to_dict()["parent_id"] is None
    assert "parent_id" not in anywhere.to_dict()


def test_refuses_a_collection_search_that_asks_nothing() -> None:
    with pytest.raises(OdpValidationError):
        parse_collection_search_request('{"odp_version":"1.0"}')


def test_compares_any_bucket_value_the_model_can_hold() -> None:
    """`RefinementBucket.value` is typed as any JSON value, so the comparison is total over one.

    The schema narrows what actually arrives to a scalar, and this keeps the two from drifting: if
    that narrowing were ever relaxed, a composite value would still compare by what it contains
    rather than by object identity.
    """
    from offering_protocol.core.validation import _bucket_key

    assert _bucket_key({"a": 1, "b": 2}) == _bucket_key({"b": 2, "a": 1})
    assert _bucket_key([1, 2]) != _bucket_key([2, 1])
    assert _bucket_key(None) != _bucket_key("null")
    assert _bucket_key(True) != _bucket_key(1)
    assert _bucket_key("1.0") == _bucket_key("1.00")
