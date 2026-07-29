"""Tests for :mod:`birthdays.icsfile` -- the hand rolled RFC 5545 writer.

The ICS file is the product's only output, so it gets checked at the byte level:
RFC 5545 line endings (CRLF, section 3.1), 75-*octet* folding (section 3.1), TEXT
escaping (section 3.3.11) and, above all, verbatim UID passthrough -- a mangled UID
turns every re-import into a fresh insert, which is exactly the duplicate the whole
tool exists to prevent.

The unfolding helpers below are deliberately dependency-free: pulling in an ics
parser would test that parser's tolerance, not our conformance.
"""

from __future__ import annotations

import re
from datetime import date, datetime, timezone

import pytest

from birthdays.dedupe import identity_key, make_uid, merge
from birthdays.icsfile import PRODID, build_ics, escape_text, fold_line, write_ics
from birthdays.models import MergedPerson, Person

TODAY = date(2026, 7, 29)
NOW = datetime(2026, 7, 29, 12, 34, 56, tzinfo=timezone.utc)

CRLF = b"\r\n"


# --------------------------------------------------------------------------------------
# helpers (no third-party ICS parser on purpose)
# --------------------------------------------------------------------------------------


def physical_lines(payload: bytes) -> list[bytes]:
    """Every physical line of the file, as bytes, without the trailing CRLF."""
    assert payload.endswith(CRLF), "file must end with CRLF"
    return payload[: -len(CRLF)].split(CRLF)


def unfold(payload: bytes) -> list[str]:
    """Reverse RFC 5545 folding: CRLF + a single leading WSP is not a line break."""
    out: list[bytes] = []
    for line in physical_lines(payload):
        if line[:1] in (b" ", b"\t") and out:
            out[-1] += line[1:]
        else:
            out.append(line)
    return [line.decode("utf-8") for line in out]


def content_props(payload: bytes) -> list[tuple[str, str]]:
    """Unfolded lines split into ``(name-with-params, value)`` pairs."""
    props: list[tuple[str, str]] = []
    for line in unfold(payload):
        name, sep, value = line.partition(":")
        assert sep, f"content line without a value: {line!r}"
        props.append((name, value))
    return props


def prop(payload: bytes, name: str) -> str:
    """The single value of property ``name`` (raises unless it appears exactly once)."""
    values = [v for n, v in content_props(payload) if n == name or n.startswith(f"{name};")]
    assert len(values) == 1, f"expected exactly one {name}, got {values!r}"
    return values[0]


_UNESCAPE = re.compile(r"\\(.)")


def unescape_text(value: str) -> str:
    """Inverse of :func:`birthdays.icsfile.escape_text` (RFC 5545 section 3.3.11)."""

    def sub(match: re.Match[str]) -> str:
        ch = match.group(1)
        return "\n" if ch in "nN" else ch

    return _UNESCAPE.sub(sub, value)


def vevents(payload: bytes) -> list[list[tuple[str, str]]]:
    """The property list of each VEVENT, in file order."""
    events: list[list[tuple[str, str]]] = []
    current: list[tuple[str, str]] | None = None
    for name, value in content_props(payload):
        if (name, value) == ("BEGIN", "VEVENT"):
            assert current is None, "nested VEVENT"
            current = []
            continue
        if (name, value) == ("END", "VEVENT"):
            assert current is not None, "END:VEVENT without BEGIN"
            events.append(current)
            current = None
            continue
        if current is not None:
            current.append((name, value))
    assert current is None, "unterminated VEVENT"
    return events


def event_value(event: list[tuple[str, str]], name: str) -> str:
    values = [v for n, v in event if n == name or n.startswith(f"{name};")]
    assert len(values) == 1, f"expected exactly one {name} in event, got {values!r}"
    return values[0]


def person(
    name: str = "Jane Doe",
    month: int = 3,
    day: int = 14,
    year: int | None = None,
    *,
    uid: str | None = None,
    members: list[Person] | None = None,
) -> MergedPerson:
    """A MergedPerson with a realistic (dedupe-generated) UID unless told otherwise."""
    if members is None:
        members = [
            Person(
                source="facebook",
                source_id="100",
                name=name,
                month=month,
                day=day,
                year=year,
                profile_url="https://www.facebook.com/100",
            )
        ]
    return MergedPerson(
        uid=uid if uid is not None else make_uid(month, day, identity_key(name)),
        name=name,
        month=month,
        day=day,
        year=year,
        members=members,
    )


def build(people: list[MergedPerson], **kwargs) -> bytes:
    kwargs.setdefault("today", TODAY)
    kwargs.setdefault("now", NOW)
    return build_ics(people, **kwargs)


# --------------------------------------------------------------------------------------
# structure
# --------------------------------------------------------------------------------------


def test_calendar_wrapper_and_required_properties():
    payload = build([person()])
    props = content_props(payload)

    assert props[0] == ("BEGIN", "VCALENDAR")
    assert props[-1] == ("END", "VCALENDAR")
    assert props.count(("BEGIN", "VCALENDAR")) == 1
    assert props.count(("END", "VCALENDAR")) == 1

    names = [n for n, _ in props]
    assert names.count("VERSION") == 1
    assert names.count("PRODID") == 1
    assert prop(payload, "VERSION") == "2.0"
    assert prop(payload, "PRODID") == PRODID
    assert prop(payload, "CALSCALE") == "GREGORIAN"


def test_begin_end_pairing_is_balanced():
    payload = build([person("Jane Doe"), person("Chris Ames", month=1, day=2)])
    depth = 0
    stack: list[str] = []
    for name, value in content_props(payload):
        if name == "BEGIN":
            stack.append(value)
            depth += 1
        elif name == "END":
            assert stack and stack[-1] == value, f"END:{value} does not close BEGIN:{stack[-1:]}"
            stack.pop()
            depth -= 1
        assert depth >= 0
    assert stack == []


def test_one_vevent_per_merged_person_in_input_order():
    people = [
        person("Jane Doe", month=3, day=14),
        person("Chris Ames", month=1, day=2),
        person("Ada Lovelace", month=12, day=10),
    ]
    payload = build(people)
    events = vevents(payload)
    assert len(events) == len(people)
    summaries = [event_value(e, "SUMMARY") for e in events]
    assert summaries == ["Jane Doe's Birthday", "Chris Ames' Birthday", "Ada Lovelace's Birthday"]


def test_every_event_carries_the_mandatory_properties():
    payload = build([person("Jane Doe", year=1990)])
    (event,) = vevents(payload)
    names = [n.split(";")[0] for n, _ in event]
    for required in ("UID", "DTSTAMP", "DTSTART", "SUMMARY", "RRULE"):
        assert names.count(required) == 1, f"{required} missing or duplicated: {names}"


def test_dtstamp_is_a_utc_timestamp():
    payload = build([person()])
    stamp = prop(payload, "DTSTAMP")
    assert stamp == "20260729T123456Z"
    assert re.fullmatch(r"\d{8}T\d{6}Z", stamp)


def test_empty_input_still_produces_a_well_formed_wrapper():
    payload = build([])
    props = content_props(payload)
    assert props[0] == ("BEGIN", "VCALENDAR")
    assert props[-1] == ("END", "VCALENDAR")
    assert vevents(payload) == []


# --------------------------------------------------------------------------------------
# line endings
# --------------------------------------------------------------------------------------


def test_file_uses_crlf_everywhere_including_the_final_line():
    payload = build([person("Jane Doe"), person("Chris Ames", month=1, day=2)])

    assert payload.endswith(CRLF)
    # A bare LF is only ever legal as the second half of a CRLF.
    assert payload.replace(CRLF, b"").count(b"\n") == 0
    assert payload.replace(CRLF, b"").count(b"\r") == 0
    # ...and every LF in the file is preceded by a CR.
    for index, byte in enumerate(payload):
        if byte == 0x0A:
            assert index > 0 and payload[index - 1] == 0x0D


def test_no_empty_lines_and_no_bom():
    payload = build([person()])
    assert not payload.startswith(b"\xef\xbb\xbf")
    assert all(line for line in physical_lines(payload)), "blank line in ICS output"


def test_written_file_is_byte_identical_to_build_ics(tmp_path):
    people = [person("Jane Doe", year=1990)]
    path = tmp_path / "nested" / "dir" / "birthdays.ics"
    returned = write_ics(people, path, today=TODAY, now=NOW)

    assert returned == path
    assert path.read_bytes() == build(people)
    # Text mode would translate CRLF on some platforms; make sure it did not.
    assert b"\r\n" in path.read_bytes()


# --------------------------------------------------------------------------------------
# folding (RFC 5545 section 3.1) -- octets, not characters
# --------------------------------------------------------------------------------------

# 60 accented characters plus astral-plane emoji: every codepoint here is 2-4 UTF-8
# bytes, so a folder that counts characters instead of octets blows the 75-octet limit,
# and a folder that slices bytes blindly splits a codepoint in half.
LONG_UNICODE_NAME = "Éloïse-Ünnüßlich Ñañá Çøéür" * 3 + " 😀🎂🎉"


def test_no_physical_line_exceeds_75_octets():
    people = [
        person(LONG_UNICODE_NAME, month=6, day=1),
        person("Jane Doe"),
        person("x" * 200, month=2, day=2),
    ]
    payload = build(people, calname="A very long calendar name " * 5)
    for line in physical_lines(payload):
        assert len(line) <= 75, f"{len(line)} octets: {line!r}"


def test_folding_never_splits_a_utf8_sequence():
    payload = build([person(LONG_UNICODE_NAME, month=6, day=1)])
    for line in physical_lines(payload):
        # Each physical line must decode on its own -- a split codepoint raises here.
        line.decode("utf-8")


def test_continuation_lines_start_with_exactly_one_space():
    payload = build([person(LONG_UNICODE_NAME, month=6, day=1)])
    lines = physical_lines(payload)
    assert any(line.startswith(b" ") for line in lines), "test input was not long enough to fold"
    for line in lines:
        if line.startswith(b" "):
            assert not line.startswith(b"  "), "second space would be swallowed by unfolding"


def test_unfolded_summary_round_trips_the_long_unicode_name():
    payload = build([person(LONG_UNICODE_NAME, month=6, day=1)])
    (event,) = vevents(payload)
    assert unescape_text(event_value(event, "SUMMARY")) == f"{LONG_UNICODE_NAME}'s Birthday"


@pytest.mark.parametrize("length", list(range(1, 130)))
def test_fold_line_is_correct_at_every_length(length):
    """Fold boundaries land at every possible offset inside a 4-byte codepoint."""
    line = "SUMMARY:" + "😀" * length
    folded = fold_line(line)
    parts = folded.split(CRLF)
    assert all(len(part) <= 75 for part in parts)
    assert all(part.startswith(b" ") for part in parts[1:])
    rebuilt = parts[0] + b"".join(part[1:] for part in parts[1:])
    assert rebuilt.decode("utf-8") == line


def test_short_lines_are_not_folded():
    assert fold_line("SUMMARY:Jane's Birthday") == b"SUMMARY:Jane's Birthday"
    exactly_75 = "X" * 75
    assert fold_line(exactly_75) == exactly_75.encode()
    assert CRLF not in fold_line(exactly_75)


# --------------------------------------------------------------------------------------
# escaping (RFC 5545 section 3.3.11)
# --------------------------------------------------------------------------------------


def test_escape_text_handles_backslash_first():
    # If the backslash were escaped last, "a\,b" would become "a\\\\,b" (over-escaped)
    # because the escape inserted for the comma would itself get escaped.
    assert escape_text(r"a\,b") == r"a\\\,b"
    assert unescape_text(escape_text(r"a\,b")) == r"a\,b"


@pytest.mark.parametrize(
    "raw",
    [
        "Comma, Name",
        "Semi;colon",
        r"Back\slash",
        r"a\,b",
        r"\\\;,",
        "Line\nBreak",
        "CRLF\r\nBreak",
        "everything ,;\\\n at once",
    ],
)
def test_text_values_round_trip_through_escaping(raw):
    escaped = escape_text(raw)
    assert ";" not in escaped.replace(r"\;", "")
    assert "," not in escaped.replace(r"\,", "")
    assert "\n" not in escaped and "\r" not in escaped
    # \r\n and \r both normalise to a single \n escape.
    assert unescape_text(escaped) == raw.replace("\r\n", "\n").replace("\r", "\n")


def test_special_characters_survive_the_full_file():
    raw = "Jane, \"J\" Doe-Smith;Jr \\ \n(the 2nd)"
    member = Person(
        source="snapchat",
        source_id="s1",
        name=raw,
        username="janed,;\\",
        month=3,
        day=14,
    )
    payload = build([person(raw, members=[member])])
    (event,) = vevents(payload)

    assert unescape_text(event_value(event, "SUMMARY")) == f"{raw}'s Birthday"
    description = unescape_text(event_value(event, "DESCRIPTION"))
    assert raw in description
    # The raw newline must have travelled as the two-character escape "\n", never as a
    # real line break that would have been (mis)read as a new content line.
    assert r"\n" in event_value(event, "DESCRIPTION")


def test_calname_is_escaped_and_present():
    payload = build([person()], calname="Josh's Friends, Etc;")
    assert unescape_text(prop(payload, "X-WR-CALNAME")) == "Josh's Friends, Etc;"


# --------------------------------------------------------------------------------------
# dates and recurrence
# --------------------------------------------------------------------------------------


def test_dtstart_is_a_date_value_and_dtend_is_the_exclusive_next_day():
    payload = build([person("Jane Doe", month=3, day=14, year=1990)])
    (event,) = vevents(payload)
    names = [n for n, _ in event]

    assert "DTSTART;VALUE=DATE" in names
    assert "DTEND;VALUE=DATE" in names
    assert event_value(event, "DTSTART") == "19900314"
    assert event_value(event, "DTEND") == "19900315"


def test_dtend_rolls_over_month_and_year_boundaries():
    payload = build(
        [
            person("Dec Last", month=12, day=31, year=1999),
            person("Feb End", month=2, day=28, year=2001),
        ]
    )
    ends = [event_value(e, "DTEND") for e in vevents(payload)]
    assert ends == ["20000101", "20010301"]


def test_every_event_recurs_yearly():
    people = [
        person("Jane Doe", month=3, day=14),
        person("Leap Person", month=2, day=29),
        person("Old Timer", month=12, day=31, year=1970),
    ]
    payload = build(people)
    for event in vevents(payload):
        assert event_value(event, "RRULE") == "FREQ=YEARLY"


def test_known_birth_year_anchors_on_the_real_birth_date():
    payload = build([person("Jane Doe", month=3, day=14, year=1990)])
    assert prop(payload, "DTSTART") == "19900314"


def test_unknown_birth_year_anchors_on_the_injected_today_year():
    payload = build([person("Jane Doe", month=3, day=14, year=None)], today=date(2031, 1, 1))
    assert prop(payload, "DTSTART") == "20310314"


def test_leap_day_with_a_known_leap_birth_year_still_recurs_on_feb_28():
    """A genuine Feb 29 birth date must not become a once-every-four-years event.

    RFC 5545 3.3.10 derives BYMONTH/BYMONTHDAY for FREQ=YEARLY from DTSTART and requires
    clients to ignore instances with an invalid date, so DTSTART:20000229 + FREQ=YEARLY
    is *absent* in every non-leap year.  The birth year is still reported (see the
    DESCRIPTION assertion) so nothing is lost by anchoring the recurrence a day earlier.
    """
    payload = build([person("Leap Person", month=2, day=29, year=2000)])
    (event,) = vevents(payload)
    assert event_value(event, "DTSTART") == "20000228"
    assert event_value(event, "DTEND") == "20000229"
    assert event_value(event, "RRULE") == "FREQ=YEARLY"
    assert "Born 2000" in event_value(event, "DESCRIPTION")


def test_leap_day_with_a_non_leap_birth_year_falls_back_to_feb_28():
    payload = build([person("Leap Person", month=2, day=29, year=1999)])
    (event,) = vevents(payload)
    assert event_value(event, "DTSTART") == "19990228"
    assert event_value(event, "DTEND") == "19990301"
    assert event_value(event, "RRULE") == "FREQ=YEARLY"


@pytest.mark.parametrize("today", [date(2026, 1, 1), date(2028, 1, 1)])
def test_leap_day_without_a_birth_year_never_anchors_on_feb_29(today):
    """An invented anchor year must not hide the birthday for three years out of four.

    When the birth year is unknown the anchor year is *our* choice, not a fact about the
    person, so it must not be a leap day: FREQ=YEARLY from a Feb 29 DTSTART is shown once
    every four years by most clients, which loses the birthday exactly as badly as never
    exporting it. (A genuine Feb 29 birth date is still anchored honestly -- see above.)
    """
    payload = build([person("Leap Person", month=2, day=29, year=None)], today=today)
    (event,) = vevents(payload)
    assert event_value(event, "DTSTART") == f"{today.year}0228"
    assert event_value(event, "RRULE") == "FREQ=YEARLY"


def test_impossible_day_is_clamped_into_the_month_rather_than_crashing():
    # One malformed scraped record must not take the whole export down.
    payload = build([person("Odd Record", month=4, day=31, year=None)], today=date(2026, 1, 1))
    assert prop(payload, "DTSTART") == "20260430"


def test_invalid_month_is_rejected_loudly():
    bad = person("Broken", month=3, day=1)
    bad.month = 13
    with pytest.raises(ValueError):
        build([bad])


def test_description_carries_provenance_and_age():
    fb = Person(
        source="facebook",
        source_id="100",
        name="Jane Doe",
        month=3,
        day=14,
        year=1990,
        profile_url="https://www.facebook.com/100",
    )
    snap = Person(source="snapchat", source_id="s1", name="", username="janed", month=3, day=14)
    payload = build([person("Jane Doe", month=3, day=14, year=1990, members=[fb, snap])])
    description = unescape_text(prop(payload, "DESCRIPTION"))

    assert "Facebook: Jane Doe (https://www.facebook.com/100)" in description
    assert "Snapchat: janed" in description
    # TODAY is 2026-07-29, so 1990-03-14 has already passed this year.
    assert "Born 1990 (turns 37 in 2027)" in description


# --------------------------------------------------------------------------------------
# summary wording
# --------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        ("Jane", "Jane's Birthday"),
        ("Jane Doe", "Jane Doe's Birthday"),
        ("Chris", "Chris' Birthday"),
        ("Ames", "Ames' Birthday"),
        ("CHRIS", "CHRIS' Birthday"),
        ("Chris Ames", "Chris Ames' Birthday"),
        ("Zoë", "Zoë's Birthday"),
        ("", "Friend's Birthday"),
    ],
)
def test_possessive_summary(name, expected):
    payload = build([person(name)])
    assert unescape_text(prop(payload, "SUMMARY")) == expected


# --------------------------------------------------------------------------------------
# the anti-duplicate contract
# --------------------------------------------------------------------------------------


def test_uid_is_passed_through_verbatim():
    uid = make_uid(3, 14, identity_key("Jane Doe"))
    payload = build([person("Jane Doe", uid=uid)])

    assert prop(payload, "UID") == uid
    # Byte-exact, unescaped, unfolded, on its own line.
    assert f"UID:{uid}\r\n".encode() in payload


def test_uid_is_not_regenerated_from_the_display_name():
    """icsfile must never invent a UID: upstream owns UID stability.

    "Jane Doe" and "Jane Marie Doe" are the same person once a second source fills in
    the middle name, and dedupe deliberately gives them the same UID so the calendar
    updates the event instead of inserting a second one.
    """
    uid = make_uid(3, 14, identity_key("Jane Doe"))
    a = build([person("Jane Doe", uid=uid)])
    b = build([person("Jane Marie Doe", uid=uid)])

    assert prop(a, "UID") == prop(b, "UID") == uid
    assert prop(a, "SUMMARY") != prop(b, "SUMMARY")


def test_uid_with_awkward_characters_is_not_folded_into_a_different_string():
    uid = "bd-" + "9" * 90 + "@birthdays.local"
    payload = build([person("Jane Doe", uid=uid)])
    assert prop(payload, "UID") == uid  # unfolds back to exactly what we handed in


def test_merged_people_keep_one_event_and_one_uid_each():
    people = merge(
        [
            Person(source="facebook", source_id="1", name="Jane Doe", month=3, day=14, year=1990),
            Person(source="snapchat", source_id="s1", name="Jane Doe", username="janedoe22",
                   month=3, day=14),
            Person(source="snapchat", source_id="s2", name="Chris Ames", month=3, day=14),
        ]
    )
    payload = build(people)
    events = vevents(payload)
    uids = [event_value(e, "UID") for e in events]

    assert len(events) == len(people) == 2
    assert len(set(uids)) == len(uids), "duplicate UID would silently collapse an event"
    assert sorted(uids) == sorted(p.uid for p in people)


# --------------------------------------------------------------------------------------
# determinism
# --------------------------------------------------------------------------------------


def test_same_input_and_clock_produces_byte_identical_output():
    people = [
        person("Jane Doe", month=3, day=14, year=1990),
        person(LONG_UNICODE_NAME, month=6, day=1),
        person("Leap Person", month=2, day=29),
    ]
    assert build(people) == build(people)


def test_written_files_are_byte_identical_across_runs(tmp_path):
    people = [person("Jane Doe", year=1990), person("Chris Ames", month=1, day=2)]
    a = tmp_path / "a.ics"
    b = tmp_path / "b.ics"
    write_ics(people, a, today=TODAY, now=NOW)
    write_ics(people, b, today=TODAY, now=NOW)
    assert a.read_bytes() == b.read_bytes()


def test_only_the_wall_clock_stamps_move_between_runs():
    """With `today` pinned but `now` left real, everything but the stamps is stable.

    DTSTAMP/CREATED/LAST-MODIFIED are deliberately *not* frozen in production: they say
    when this file was generated, and clients use DTSTAMP to tell a fresh export from a
    stale one. Nothing a calendar keys on (UID, SUMMARY, DTSTART, RRULE) may move.
    """
    people = [person("Jane Doe", month=3, day=14, year=1990), person("Chris Ames", month=1, day=2)]
    volatile = {"DTSTAMP", "CREATED", "LAST-MODIFIED"}

    def stable(payload: bytes) -> list[tuple[str, str]]:
        return [(n, v) for n, v in content_props(payload) if n.split(";")[0] not in volatile]

    first = build_ics(people, today=TODAY)
    second = build_ics(people, today=TODAY)
    assert stable(first) == stable(second)


def test_output_does_not_depend_on_the_ambient_clock():
    people = [person("Jane Doe", month=3, day=14, year=1990)]
    assert build(people, today=TODAY) == build(people, today=TODAY)
    # ...but it does follow the injected `today` for year-less birthdays.
    yearless = [person("Chris Ames", month=1, day=2, year=None)]
    assert build(yearless, today=date(2026, 1, 1)) != build(yearless, today=date(2027, 1, 1))
