"""Tests for birthdays.sources.facebook.parse_birthday_json.

The realistic input is fb2cal's captured GraphQL response (see
``tests/fixtures/fb_birthday_comet_mocks.py`` for attribution); the synthetic cases
below cover the edges that capture happens not to contain.
"""

from __future__ import annotations

import copy

import pytest
from fb_birthday_comet_mocks import BIRTHDAY_COMET_ROOT_JANUARY_MOCK

from birthdays.models import Person
from birthdays.sources.facebook import parse_birthday_json

# --------------------------------------------------------------------------------------
# Helpers over the real fixture
# --------------------------------------------------------------------------------------


def by_id(people: list[Person]) -> dict[str, Person]:
    return {p.source_id: p for p in people}


def only_viewer_shape() -> dict:
    """``data.viewer.all_friends_by_birthday_month.edges[].node.friends.edges[].node``."""
    data = BIRTHDAY_COMET_ROOT_JANUARY_MOCK["data"]
    return {"data": {"viewer": copy.deepcopy(data["viewer"])}}


def only_today_recent_upcoming_shape() -> dict:
    """``data.{today,recent,upcoming,upcomingAll}.all_friends.edges[].node``."""
    data = BIRTHDAY_COMET_ROOT_JANUARY_MOCK["data"]
    return {
        "data": {
            key: copy.deepcopy(data[key])
            for key in ("today", "recent", "upcoming", "upcomingAll")
        }
    }


# --------------------------------------------------------------------------------------
# Real-world shape 1: viewer.all_friends_by_birthday_month
# --------------------------------------------------------------------------------------


def test_viewer_by_birthday_month_shape():
    people = parse_birthday_json(only_viewer_shape())

    assert {p.source_id for p in people} == {"600009847", "1000023", "198041065"}
    assert all(p.source == "facebook" for p in people)

    pete = by_id(people)["600009847"]
    assert (pete.name, pete.month, pete.day, pete.year) == ("Pirate Pete", 11, 1, 1982)
    assert pete.profile_url == "https://www.facebook.com/pirate.pete"


def test_viewer_shape_handles_null_year():
    santa = by_id(parse_birthday_json(only_viewer_shape()))["1000023"]

    assert (santa.name, santa.month, santa.day) == ("Santa Claus", 12, 25)
    assert santa.year is None


def test_viewer_shape_ignores_implausible_year():
    """Dumbledore's ``year: 1881`` is outside the 1900..2200 sanity window."""
    albus = by_id(parse_birthday_json(only_viewer_shape()))["198041065"]

    assert (albus.name, albus.month, albus.day) == ("Albus Dumbledore", 1, 17)
    assert albus.year is None


def test_context_sentence_entities_are_not_mistaken_for_people():
    """``friends_by_birthday_month_context_sentence`` repeats ids without birthdates."""
    people = parse_birthday_json(only_viewer_shape())

    assert len(people) == 3  # not 3 + the entity mentions


# --------------------------------------------------------------------------------------
# Real-world shape 2: today / recent / upcoming
# --------------------------------------------------------------------------------------


def test_today_recent_upcoming_shape():
    people = parse_birthday_json(only_today_recent_upcoming_shape())

    assert {p.source_id for p in people} == {"100000000000001", "1353772287"}

    test_user = by_id(people)["100000000000001"]
    assert (test_user.name, test_user.month, test_user.day, test_user.year) == (
        "Test User",
        1,
        1,
        2000,
    )
    assert test_user.profile_url == "https://www.facebook.com/test.user"


def test_today_recent_upcoming_shape_handles_null_year():
    captain = by_id(parse_birthday_json(only_today_recent_upcoming_shape()))["1353772287"]

    assert (captain.name, captain.month, captain.day) == ("Crazy Captain", 2, 2)
    assert captain.year is None


def test_empty_buckets_contribute_nothing():
    """``today.all_friends.edges`` is empty and ``upcomingAll``'s edges carry no node."""
    doc = only_today_recent_upcoming_shape()
    assert doc["data"]["today"]["all_friends"]["edges"] == []
    assert all("node" not in e for e in doc["data"]["upcomingAll"]["all_friends"]["edges"])

    assert len(parse_birthday_json(doc)) == 2


# --------------------------------------------------------------------------------------
# Both shapes at once (what the live endpoint actually returns)
# --------------------------------------------------------------------------------------


def test_full_document_finds_every_person_from_both_shapes():
    people = parse_birthday_json(BIRTHDAY_COMET_ROOT_JANUARY_MOCK)

    assert {p.source_id for p in people} == {
        "100000000000001",
        "1353772287",
        "600009847",
        "1000023",
        "198041065",
    }
    assert len(people) == 5


def test_parsing_does_not_mutate_the_input_document():
    before = copy.deepcopy(BIRTHDAY_COMET_ROOT_JANUARY_MOCK)

    parse_birthday_json(BIRTHDAY_COMET_ROOT_JANUARY_MOCK)

    assert BIRTHDAY_COMET_ROOT_JANUARY_MOCK == before


def test_raw_json_string_is_not_accepted_silently():
    """A str is not a document; walking its characters must not invent people."""
    import json

    assert parse_birthday_json(json.dumps(BIRTHDAY_COMET_ROOT_JANUARY_MOCK)) == []


# --------------------------------------------------------------------------------------
# Synthetic edge cases
# --------------------------------------------------------------------------------------


def node(**overrides) -> dict:
    base = {
        "__typename": "User",
        "id": "123",
        "name": "Jane Doe",
        "birthdate": {"day": 14, "month": 3, "year": 1990},
        "profile_url": "https://www.facebook.com/jane.doe",
    }
    base.update(overrides)
    return base


def wrap(*nodes: dict) -> dict:
    return {"data": {"x": {"all_friends": {"edges": [{"node": n} for n in nodes]}}}}


@pytest.mark.parametrize(
    "bad",
    [
        {"id": None},
        {"id": ""},
        {"id": "   "},
        {"name": None},
        {"name": ""},
        {"name": "   "},
    ],
    ids=["id-null", "id-empty", "id-blank", "name-null", "name-empty", "name-blank"],
)
def test_missing_id_or_name_is_skipped(bad):
    assert parse_birthday_json(wrap(node(**bad))) == []


@pytest.mark.parametrize(
    "birthdate",
    [
        None,
        {},
        {"day": 14},
        {"month": 3},
        {"day": None, "month": None},
        {"day": 14, "month": 13},
        {"day": 0, "month": 3},
        {"day": 32, "month": 3},
        {"day": "??", "month": "??"},
        "1 January",
    ],
    ids=[
        "null",
        "empty",
        "day-only",
        "month-only",
        "both-null",
        "month-out-of-range",
        "day-zero",
        "day-too-big",
        "non-numeric",
        "not-a-dict",
    ],
)
def test_unusable_birthdate_is_skipped(birthdate):
    assert parse_birthday_json(wrap(node(birthdate=birthdate))) == []


def test_stringified_month_and_day_are_coerced():
    people = parse_birthday_json(wrap(node(birthdate={"day": "14", "month": "3", "year": "1990"})))

    assert (people[0].month, people[0].day, people[0].year) == (3, 14, 1990)


def test_dedupe_by_source_id_prefers_the_record_with_a_year():
    """The same friend shows up in several buckets; the richest record wins."""
    without_year = node(birthdate={"day": 14, "month": 3, "year": None})
    with_year = node(birthdate={"day": 14, "month": 3, "year": 1990})

    for order in ((without_year, with_year), (with_year, without_year)):
        people = parse_birthday_json(wrap(*order))

        assert len(people) == 1
        assert people[0].year == 1990


def test_dedupe_prefers_a_real_profile_url_over_the_synthesised_one():
    bare = node(id="777", profile_url=None, url=None)
    linked = node(id="777", profile_url="https://www.facebook.com/real.name")

    for order in ((bare, linked), (linked, bare)):
        people = parse_birthday_json(wrap(*order))

        assert len(people) == 1
        assert people[0].profile_url == "https://www.facebook.com/real.name"


def test_profile_url_falls_back_to_url_then_to_the_numeric_permalink():
    from_url = parse_birthday_json(wrap(node(profile_url=None, url="https://fb.com/via-url")))
    assert from_url[0].profile_url == "https://fb.com/via-url"

    synthesised = parse_birthday_json(wrap(node(id="42", profile_url=None, url=None)))
    assert synthesised[0].profile_url == "https://www.facebook.com/42"


def test_numeric_id_is_stringified():
    people = parse_birthday_json(wrap(node(id=100000000000001)))

    assert people[0].source_id == "100000000000001"


@pytest.mark.parametrize("junk", [None, {}, [], "", 0, {"data": None}, [[[]]], {"data": {}}])
def test_junk_documents_return_empty(junk):
    assert parse_birthday_json(junk) == []


def test_deeply_nested_and_unknown_shapes_still_yield_people():
    """Shape-agnostic: a brand new wrapper path must not break extraction."""
    doc = {"data": {"brand": {"new": {"path": [[{"whatever": [node(id="55")]}]]}}}}

    people = parse_birthday_json(doc)

    assert len(people) == 1
    assert people[0].source_id == "55"
