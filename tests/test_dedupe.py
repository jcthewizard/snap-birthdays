"""Tests for birthdays.dedupe -- cross-source matching, merging and UID stability.

Two failure modes matter here and they are not symmetric:

* a **false split** costs the user two calendar entries for one friend;
* a **false merge** silently deletes a friend's birthday.

so the "must not merge" half of this file is at least as important as the other.
On top of that, ``uid`` is the anti-duplicate contract with the calendar client: if a
UID moves between runs the client inserts a second event instead of updating the first.
"""

from __future__ import annotations

import itertools

import pytest

from birthdays.dedupe import merge, normalize_name
from birthdays.models import MergedPerson, Person

# --------------------------------------------------------------------------------------
# Builders
# --------------------------------------------------------------------------------------


def fb(name: str, month: int = 3, day: int = 14, *, id: str = "", year: int | None = None) -> Person:
    return Person(
        source="facebook",
        source_id=id or f"fb-{abs(hash(name)) % 10**9}",
        name=name,
        month=month,
        day=day,
        year=year,
        profile_url="https://www.facebook.com/" + (id or "profile"),
    )


def snap(
    name: str,
    month: int = 3,
    day: int = 14,
    *,
    username: str | None = None,
    id: str = "",
) -> Person:
    handle = username if username is not None else (name.lower().replace(" ", "") or "handle")
    return Person(
        source="snapchat",
        source_id=id or handle,
        name=name,
        month=month,
        day=day,
        username=handle,
    )


def groups(people: list[Person]) -> list[set[str]]:
    """merge() output as a list of member-key sets, for readable assertions."""
    return [{f"{m.source}:{m.source_id}" for m in mp.members} for mp in merge(people)]


def uid_of(people: list[Person], *, contains: str) -> str:
    """UID of the single merged row that has a member mentioning ``contains``."""
    hits = [
        mp
        for mp in merge(people)
        if any(
            contains in field
            for m in mp.members
            for field in (m.source_id, m.name, m.username or "")
        )
    ]
    assert len(hits) == 1, f"expected exactly one row containing {contains!r}, got {hits}"
    return hits[0].uid


def merged_one(people: list[Person]) -> MergedPerson:
    result = merge(people)
    assert len(result) == 1, f"expected a single merged person, got {[m.name for m in result]}"
    return result[0]


# --------------------------------------------------------------------------------------
# MUST merge
# --------------------------------------------------------------------------------------


MUST_MERGE = [
    pytest.param(fb("Jane Doe"), snap("Jane Doe", username="janedoe"), id="identical-names"),
    pytest.param(fb("Jane Doe"), snap("jane doe", username="jd_2001"), id="case-difference"),
    pytest.param(fb("Jane Marie Doe"), snap("Jane Doe", username="janed"), id="middle-name"),
    pytest.param(fb("José García"), snap("Jose Garcia", username="jgarcia"), id="diacritics"),
    pytest.param(fb("Doe, Jane"), snap("Jane Doe", username="janedoe"), id="token-reorder"),
    pytest.param(fb("Jane  Doe"), snap("Jane Doe", username="janedoe"), id="extra-whitespace"),
    pytest.param(fb("Anne-Marie Doe"), snap("Anne Marie Doe", username="am"), id="hyphenated"),
    pytest.param(fb("Jane Doe"), snap("", username="jane_doe"), id="underscore-handle-only"),
    pytest.param(fb("Jane Doe"), snap("Jane Doe 🎂", username="jd"), id="emoji-suffix"),
    pytest.param(fb("Jane O'Doe"), snap("Jane ODoe", username="jodoe"), id="apostrophe"),
]


@pytest.mark.parametrize("a,b", MUST_MERGE)
def test_must_merge(a: Person, b: Person):
    result = merge([a, b])

    assert len(result) == 1, f"{a.name!r} and {b.name!r} should be one person"
    assert sorted(result[0].sources) == ["facebook", "snapchat"]
    assert len(result[0].members) == 2


@pytest.mark.parametrize("a,b", MUST_MERGE)
def test_must_merge_is_order_independent(a: Person, b: Person):
    assert len(merge([b, a])) == 1


# --------------------------------------------------------------------------------------
# MUST NOT merge
# --------------------------------------------------------------------------------------


MUST_NOT_MERGE = [
    pytest.param(
        fb("Jane Doe", 3, 14), snap("Jane Doe", 3, 15, username="janedoe2"), id="different-day"
    ),
    pytest.param(
        fb("Jane Doe", 3, 14), snap("Jane Doe", 4, 14, username="janedoe3"), id="different-month"
    ),
    pytest.param(fb("Jane Doe"), snap("Jane Smith", username="janes"), id="different-surname"),
    pytest.param(fb("Jane Doe"), snap("Jane", username="janey"), id="shared-first-name-only"),
    pytest.param(fb("Jane Doe"), fb("John Doe", id="fb-john"), id="shared-surname-only"),
    pytest.param(
        snap("", username="sk8rboi"), snap("", username="mmiller", id="s2"), id="two-handles"
    ),
    pytest.param(
        snap("Alex Kim", username="akim"),
        snap("Alexandra Nguyen", username="anguyen"),
        id="two-snap-friends-same-birthday",
    ),
    pytest.param(fb("Jane Doe"), snap("Doe", username="doedoe"), id="shared-surname-token-only"),
    pytest.param(
        fb("Michael Smith"), snap("Michelle Smith", username="mish"), id="similar-first-names"
    ),
]


@pytest.mark.parametrize("a,b", MUST_NOT_MERGE)
def test_must_not_merge(a: Person, b: Person):
    result = merge([a, b])

    assert len(result) == 2, f"{a.name or a.username!r} and {b.name or b.username!r} are different people"
    assert len({m.uid for m in result}) == 2, "distinct people must never share a UID"


@pytest.mark.parametrize("a,b", MUST_NOT_MERGE)
def test_must_not_merge_is_order_independent(a: Person, b: Person):
    assert len(merge([b, a])) == 2


def test_a_shared_birthday_alone_never_merges():
    """Ten unrelated friends born on the same day stay ten calendar events."""
    people = [
        fb("Alice Alpha"),
        fb("Bob Beta", id="fb-bob"),
        fb("Carol Gamma", id="fb-carol"),
        snap("Dan Delta", username="dandelta"),
        snap("Erin Epsilon", username="erine"),
        snap("", username="zzz_frank"),
        snap("", username="grace_h"),
        fb("Heidi Iota", id="fb-heidi"),
        snap("Ivan Kappa", username="ivank"),
        fb("Judy Lambda", id="fb-judy"),
    ]

    result = merge(people)

    assert len(result) == 10
    assert len({m.uid for m in result}) == 10


# --------------------------------------------------------------------------------------
# Transitivity
# --------------------------------------------------------------------------------------


def test_transitive_grouping():
    """A~B and B~C puts all three in one group even if A and C never matched directly."""
    a = fb("Jane Doe")
    b = snap("Jane Marie Doe", username="jmd")
    c = snap("Jane M Doe", username="janemdoe", id="s3")

    for ordering in itertools.permutations([a, b, c]):
        result = merge(list(ordering))

        assert len(result) == 1, [m.name for m in result]
        assert len(result[0].members) == 3
        assert sorted(result[0].sources) == ["facebook", "snapchat"]


def test_transitivity_does_not_leak_across_buckets():
    a = fb("Jane Doe", 3, 14)
    b = snap("Jane Marie Doe", 3, 14, username="jmd")
    c = fb("Jane Marie Doe", 6, 1, id="fb-other")

    result = merge([a, b, c])

    assert sorted(len(m.members) for m in result) == [1, 2]


# --------------------------------------------------------------------------------------
# UID stability -- THE anti-duplicate invariant
# --------------------------------------------------------------------------------------


def test_merge_is_idempotent_across_runs():
    people = [
        fb("Jane Doe", 3, 14, year=1990),
        snap("Jane Doe", 3, 14, username="janedoe"),
        fb("Bob Beta", 7, 4, id="fb-bob"),
        snap("", 12, 25, username="sk8rboi"),
    ]

    first = merge(people)
    second = merge(list(people))

    assert [m.uid for m in first] == [m.uid for m in second]
    assert [m.name for m in first] == [m.name for m in second]


def test_uid_is_independent_of_input_order():
    people = [
        fb("Jane Doe", 3, 14),
        snap("Jane Doe", 3, 14, username="janedoe"),
        fb("Bob Beta", 7, 4, id="fb-bob"),
    ]

    baseline = {m.uid for m in merge(people)}

    for ordering in itertools.permutations(people):
        assert {m.uid for m in merge(list(ordering))} == baseline


def test_uid_survives_gaining_a_second_source():
    """Sync 1: Facebook only.  Sync 2: Snapchat connected too.  Same UID -> no duplicate."""
    facebook_only = [fb("Jane Doe", 3, 14, year=1990)]
    both = facebook_only + [snap("Jane Doe", 3, 14, username="janedoe")]

    assert uid_of(both, contains="Jane Doe") == uid_of(facebook_only, contains="Jane Doe")


def test_uid_survives_gaining_a_source_that_supplies_a_longer_name():
    """The new source knows a middle name; the event must still be an update, not an insert."""
    facebook_only = [fb("Jane Doe", 3, 14)]
    both = facebook_only + [snap("Jane Marie Doe", 3, 14, username="jmd")]

    assert uid_of(both, contains="Jane") == uid_of(facebook_only, contains="Jane")


def test_uid_survives_gaining_a_source_that_reorders_the_name():
    facebook_only = [fb("Jane Doe", 3, 14)]
    both = facebook_only + [snap("Doe Jane", 3, 14, username="djane")]

    assert uid_of(both, contains="Jane") == uid_of(facebook_only, contains="Jane")


def test_uid_survives_losing_a_source():
    """Un-friending on Snapchat must not orphan (and then duplicate) the event."""
    both = [fb("Jane Doe", 3, 14), snap("Jane Doe", 3, 14, username="janedoe")]
    facebook_only = [both[0]]

    assert uid_of(facebook_only, contains="Jane Doe") == uid_of(both, contains="Jane Doe")


def test_uid_survives_a_snapchat_only_friend_being_re_fetched():
    once = [snap("", 12, 25, username="sk8rboi")]
    twice = [snap("", 12, 25, username="sk8rboi", id="different-internal-id")]

    assert uid_of(twice, contains="sk8rboi") == uid_of(once, contains="sk8rboi")


def test_uid_ignores_source_ids():
    """Facebook rotating an internal id must not create a second event."""
    before = [fb("Jane Doe", 3, 14, id="100")]
    after = [fb("Jane Doe", 3, 14, id="200")]

    assert uid_of(after, contains="Jane Doe") == uid_of(before, contains="Jane Doe")


def test_uid_changes_when_the_birthday_changes():
    march = merged_one([fb("Jane Doe", 3, 14)])
    april = merged_one([fb("Jane Doe", 4, 14)])

    assert march.uid != april.uid


def test_uid_is_a_plausible_ics_uid():
    uid = merged_one([fb("Jane Doe")]).uid

    assert "@" in uid
    assert uid == uid.strip()
    assert " " not in uid
    assert len(uid) <= 75


def test_uids_are_unique_within_one_run():
    people = [
        fb("Jane Doe", 3, 14),
        fb("Jane Smith", 3, 14, id="fb-2"),
        fb("John Doe", 3, 14, id="fb-3"),
        snap("", 3, 14, username="jane_m_doe"),
        snap("", 3, 14, username="janedoe_2", id="s2"),
        snap("Jane Doe", 5, 5, username="otherjane"),
    ]

    result = merge(people)

    assert len({m.uid for m in result}) == len(result), "UID collision loses a birthday"


# --------------------------------------------------------------------------------------
# Merged content
# --------------------------------------------------------------------------------------


def test_name_selection_prefers_the_full_name_over_the_handle():
    m = merged_one([snap("", 3, 14, username="janedoe22"), fb("Jane Doe", 3, 14)])

    assert m.name == "Jane Doe"


def test_name_selection_prefers_the_longer_full_name():
    m = merged_one([fb("Jane Doe", 3, 14), snap("Jane Marie Doe", 3, 14, username="jmd")])

    assert m.name == "Jane Marie Doe"


def test_name_falls_back_to_the_handle_when_no_real_name_exists():
    m = merged_one([snap("", 3, 14, username="sk8rboi")])

    assert m.name == "sk8rboi"


def test_year_is_taken_from_whichever_source_knows_it():
    m = merged_one(
        [snap("Jane Doe", 3, 14, username="janedoe"), fb("Jane Doe", 3, 14, year=1990)]
    )

    assert m.year == 1990


def test_sources_are_sorted_and_deduped():
    m = merged_one(
        [
            fb("Jane Doe", 3, 14),
            snap("Jane Doe", 3, 14, username="janedoe"),
            snap("Jane Doe", 3, 14, username="janedoe_alt", id="s2"),
        ]
    )

    assert m.sources == ["facebook", "snapchat"]
    assert len(m.members) == 3


def test_description_names_every_source():
    m = merged_one([fb("Jane Doe", 3, 14), snap("Jane Doe", 3, 14, username="janedoe")])

    assert "Facebook" in m.description
    assert "Snapchat" in m.description
    assert "janedoe" in m.description


def test_result_is_sorted_by_date():
    people = [
        fb("Zoe Zeta", 12, 31, id="a"),
        fb("Amy Alpha", 1, 1, id="b"),
        fb("Bob Beta", 6, 15, id="c"),
    ]

    result = merge(people)

    assert [(m.month, m.day) for m in result] == [(1, 1), (6, 15), (12, 31)]


def test_empty_input():
    assert merge([]) == []


# --------------------------------------------------------------------------------------
# normalize_name
# --------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("Jane Doe", "jane doe"),
        ("  Jane   Doe  ", "jane doe"),
        ("JANE DOE", "jane doe"),
        ("José García-López!", "jose garcia lopez"),
        ("Doe, Jane", "doe jane"),
        ("jane_doe", "jane doe"),
        ("Anne-Marie", "anne marie"),
        ("Jane Doe 🎂", "jane doe"),
        ("O'Doe", "o doe"),
        ("", ""),
        (None, ""),
        ("🎂🎉", ""),
        ("Ünïcödé", "unicode"),
    ],
)
def test_normalize_name(raw, expected):
    assert normalize_name(raw) == expected


def test_emoji_only_names_do_not_collapse_into_one_person():
    a = snap("🎂", 3, 14, username="cakeface")
    b = snap("🎉", 3, 14, username="partyhat", id="s2")

    result = merge([a, b])

    assert len(result) == 2
    assert len({m.uid for m in result}) == 2
