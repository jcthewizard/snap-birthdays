"""Regression tests for cross-run UID stability and handle false-merges.

Requirement (3) of this tool is NO DUPLICATES, and the only thing standing between the
user and a duplicated calendar is the UID: a calendar client updates an event when the
UID matches and *inserts a second one* when it does not.  Every test here is a two-run
story -- what the user saw last week vs what they see after connecting another source --
because that is the only way the bug shows up.

Requirement (2) is that ALL birthdays are exported, so the last section covers the
opposite failure: two different friends collapsing into one event.
"""

from __future__ import annotations

from birthdays.dedupe import UidLedger, identity_key, merge
from birthdays.models import Person
from birthdays.store import Store


def fb(name: str, month: int, day: int, *, id: str = "fb1", year: int | None = None) -> Person:
    return Person(
        source="facebook", source_id=id, name=name, month=month, day=day, year=year
    )


def snap(name: str, month: int, day: int, *, username: str, id: str = "") -> Person:
    return Person(
        source="snapchat",
        source_id=id or username,
        name=name,
        month=month,
        day=day,
        username=username,
    )


def uids(people: list[Person], ledger: UidLedger) -> dict[str, str]:
    """``{display name: uid}`` for one run, sharing ``ledger`` across runs."""
    return {m.name: m.uid for m in merge(people, ledger)}


# --------------------------------------------------------------------------------------
# UID stability when a second source arrives (dedupe.py:247 / :232)
# --------------------------------------------------------------------------------------


def test_uid_survives_a_merge_that_lengthens_the_name():
    """Rule (d): "Jane Doe" + "Jane Doe Smith" merge, so the display name grows.

    The group's name -- and therefore any name-derived key -- changes, but the person is
    the same human and their calendar event must be updated, not duplicated.
    """
    ledger = UidLedger()
    facebook = fb("Jane Doe", 3, 4)
    snapchat = snap("Jane Doe Smith", 3, 4, username="janedoesmith")

    week1 = uids([facebook], ledger)
    week2 = uids([facebook, snapchat], ledger)

    assert len(week2) == 1, "these two records describe one person"
    assert identity_key("Jane Doe") != identity_key("Jane Doe Smith"), "premise of the bug"
    assert week2["Jane Doe Smith"] == week1["Jane Doe"]


def test_uid_survives_a_merge_that_reorders_a_three_token_name():
    """Rule (b): "Marie Jane Doe" and "Jane Marie Doe" are one person, spelled two ways."""
    ledger = UidLedger()
    snapchat = snap("Marie Jane Doe", 5, 6, username="mjd")
    facebook = fb("Jane Marie Doe", 5, 6)

    week1 = uids([snapchat], ledger)
    week2 = uids([snapchat, facebook], ledger)

    assert len(week2) == 1
    assert week2["Jane Marie Doe"] == week1["Marie Jane Doe"]


def test_uid_survives_a_handle_only_friend_gaining_a_real_name():
    """The common Snapchat case: a blank display name, so the group is named "janedoe22".

    When Facebook turns up the group is renamed to "Jane Doe" and the squashed-handle
    rule merges them -- a handle is a single token, a full name is two, so their identity
    keys can never agree and the UID would move on the very first cross-source sync.
    """
    ledger = UidLedger()
    snapchat = snap("", 5, 6, username="janedoe22")
    facebook = fb("Jane Doe", 5, 6)

    week1 = uids([snapchat], ledger)
    week2 = uids([snapchat, facebook], ledger)

    assert len(week2) == 1, "the squashed-handle rule should merge these"
    assert week2["Jane Doe"] == week1["janedoe22"]


def test_uid_survives_a_fuzzy_merge_that_changes_the_surname_spelling():
    ledger = UidLedger()
    facebook = fb("Jane Doe", 8, 9)
    snapchat = snap("Jane Doel", 8, 9, username="janedoel")

    week1 = uids([facebook], ledger)
    week2 = uids([facebook, snapchat], ledger)

    assert len(week2) == 1
    assert week2["Jane Doel"] == week1["Jane Doe"]


# --------------------------------------------------------------------------------------
# UID stability when two *unmerged* groups collide (dedupe.py:403)
# --------------------------------------------------------------------------------------


def test_a_colliding_newcomer_does_not_move_the_incumbents_uid():
    """"jane_doe" and "Jane Marie Doe" share an identity key but never merge.

    Salting by list position made the UID a function of who *else* was in the run, so
    connecting Facebook re-salted the Snapchat friend whose event already existed.
    """
    ledger = UidLedger()
    snapchat = snap("", 2, 2, username="jane_doe")
    facebook = fb("Jane Marie Doe", 2, 2)

    assert identity_key("jane_doe") == identity_key("Jane Marie Doe"), "premise of the bug"

    week1 = uids([snapchat], ledger)
    week2 = uids([snapchat, facebook], ledger)

    assert len(week2) == 2, "different people must stay two events"
    assert week2["jane_doe"] == week1["jane_doe"]
    assert len(set(week2.values())) == 2, "a shared UID silently deletes one of them"


def test_losing_the_colliding_partner_does_not_move_the_survivors_uid():
    ledger = UidLedger()
    snapchat = snap("", 2, 2, username="jane_doe")
    facebook = fb("Jane Marie Doe", 2, 2)

    week1 = uids([snapchat, facebook], ledger)
    week2 = uids([snapchat], ledger)  # unfriended on Facebook

    assert week2["jane_doe"] == week1["jane_doe"]


def test_a_salted_uid_is_derived_from_the_group_not_from_the_run():
    """Property check on the salt: same people in, same salted UID out.

    (The discriminating tests for collision handling are the two above -- this one only
    pins the salt down as a function of the group's members.)
    """
    snapchat = snap("", 2, 2, username="jane_doe")
    facebook = fb("Jane Marie Doe", 2, 2)
    unrelated = fb("Someone Else", 2, 2, id="fb-other")

    def salted(people: list[Person]) -> str:
        rows = {m.members[0].source_id: m.uid for m in merge(people)}
        return rows["jane_doe"]

    assert salted([snapchat, facebook]) == salted([facebook, snapchat])
    assert salted([snapchat, facebook]) == salted([snapchat, facebook, unrelated])


# --------------------------------------------------------------------------------------
# The ledger itself
# --------------------------------------------------------------------------------------


def test_ledger_round_trips_through_the_store(tmp_path):
    store = Store(tmp_path / "state")
    people = [fb("Jane Doe", 3, 14), snap("", 12, 25, username="sk8rboi")]

    first = UidLedger(store.load_uid_map())
    before = {m.name: m.uid for m in merge(people, first)}
    assert first.dirty
    store.save_uid_map(first.as_dict())

    second = UidLedger(Store(tmp_path / "state").load_uid_map())
    after = {m.name: m.uid for m in merge(people, second)}

    assert after == before
    assert not second.dirty, "a run that learns nothing new must not rewrite the ledger"


def test_ledger_prefers_the_best_supported_uid_when_two_events_merge():
    ledger = UidLedger({"facebook:fb1": "bd-aaa@birthdays.local", "snapchat:s1": "bd-aaa@birthdays.local", "snapchat:s2": "bd-bbb@birthdays.local"})
    members = [
        fb("Jane Doe", 3, 14),
        snap("Jane Doe", 3, 14, username="jd", id="s1"),
        snap("Jane Doe", 3, 14, username="jd2", id="s2"),
    ]

    assert ledger.lookup(members) == "bd-aaa@birthdays.local"


def test_a_corrupt_ledger_degrades_to_recomputing_uids(tmp_path):
    store = Store(tmp_path / "state")
    store.uid_map_path().write_text("{ not json", encoding="utf-8")

    assert store.load_uid_map() == {}
    assert merge([fb("Jane Doe", 3, 14)], UidLedger(store.load_uid_map()))


# --------------------------------------------------------------------------------------
# Handles must not swallow each other (dedupe.py:180 -> :217)
# --------------------------------------------------------------------------------------


def test_handles_differing_only_in_digits_are_different_people():
    """normalize_name() deletes digits, so "mike_23" and "mike99" both become "mike"."""
    result = merge(
        [
            snap("Mike Adams", 4, 1, username="mike_23"),
            snap("Mike Zhou", 4, 1, username="mike99"),
        ]
    )

    assert sorted(m.name for m in result) == ["Mike Adams", "Mike Zhou"]
    assert len({m.uid for m in result}) == 2


def test_handle_only_records_differing_only_in_digits_are_different_people():
    """Exactly how snapchat.py builds a friend with no display name: name = username."""
    result = merge(
        [
            snap("sarah2004", 6, 9, username="sarah2004"),
            snap("sarah_99", 6, 9, username="sarah_99"),
        ]
    )

    assert len(result) == 2, "one of these two friends would lose their birthday"


def test_a_short_handle_never_merges_on_a_first_name_alone():
    result = merge(
        [
            snap("", 7, 7, username="mike"),
            fb("Mike Adams", 7, 7),
            snap("Mike Zhou", 7, 7, username="mikez1"),
        ]
    )

    assert len(result) == 3
    assert len({m.uid for m in result}) == 3


def test_the_squashed_handle_merge_the_maintainer_added_still_works():
    """The fix above must not undo the "janedoe22" == "Jane Doe" concession."""
    result = merge([snap("", 5, 6, username="janedoe22"), fb("Jane Doe", 5, 6)])

    assert len(result) == 1
    assert result[0].sources == ["facebook", "snapchat"]
