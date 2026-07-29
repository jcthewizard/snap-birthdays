"""Tests for snapbirthdays.

Run with: python3 -m pytest test_snapbirthdays.py

No network and no browser: the protobuf decoder is exercised against payloads built by
the tiny encoder below, and the ICS writer against hand-built friend dicts.
"""

from __future__ import annotations

import re
from datetime import date

import pytest

from snapbirthdays import (
    _escape,
    _fold,
    _uid,
    build_ics,
    decode_friends,
    parse_fields,
    read_varint,
    strip_grpc_web,
)

# --------------------------------------------------------------------------------------
# A minimal protobuf encoder, so the tests build payloads the way Snapchat does rather
# than only the way our decoder happens to read.
# --------------------------------------------------------------------------------------


def varint(value: int) -> bytes:
    out = bytearray()
    while True:
        byte, value = value & 0x7F, value >> 7
        out.append(byte | (0x80 if value else 0))
        if not value:
            return bytes(out)


def field(num: int, payload: bytes) -> bytes:
    return varint(num << 3 | 2) + varint(len(payload)) + payload


def uint(num: int, value: int) -> bytes:
    return varint(num << 3 | 0) + varint(value)


def friend_record(username: str, name: str, month=None, day=None, pad=True) -> bytes:
    body = field(2, username.encode()) + field(3, name.encode())
    if month and day:
        body += field(5, uint(2, month) + uint(3, day))
    if pad:
        # Real records carry avatar ids, streak counts and so on. Our scanner requires a
        # record longer than 50 bytes, so pad to a realistic size.
        body += field(15, b"\x00" * 60)
    return field(2, body)


def grpc_frame(message: bytes, trailer: bool = True) -> bytes:
    out = b"\x00" + len(message).to_bytes(4, "big") + message
    if trailer:
        # gRPC-web appends a trailer frame; it must not be mistaken for friend data.
        trailer_bytes = b"grpc-status:0\r\n"
        out += b"\x80" + len(trailer_bytes).to_bytes(4, "big") + trailer_bytes
    return out


def sync_response(friends: list[tuple], **kw) -> bytes:
    return grpc_frame(b"".join(friend_record(*f) for f in friends), **kw)


# --------------------------------------------------------------------------------------
# Protobuf primitives
# --------------------------------------------------------------------------------------


@pytest.mark.parametrize("value", [0, 1, 127, 128, 300, 2**31, 2**62])
def test_read_varint_round_trip(value):
    assert read_varint(varint(value), 0) == (value, len(varint(value)))


def test_read_varint_stops_at_buffer_end():
    value, pos = read_varint(b"\x80", 0)  # continuation bit set but nothing follows
    assert pos == 1 and isinstance(value, int)


def test_read_varint_caps_runaway_input():
    """A wall of 0x80 must not spin or produce an unbounded shift."""
    _, pos = read_varint(b"\x80" * 200, 0)
    assert pos <= 10


def test_parse_fields_stops_on_truncated_length():
    assert parse_fields(varint(2 << 3 | 2) + varint(99) + b"short") == {}


# --------------------------------------------------------------------------------------
# gRPC-web framing
# --------------------------------------------------------------------------------------


def test_strip_grpc_web_unwraps_data_frame():
    assert strip_grpc_web(grpc_frame(b"hello", trailer=False)) == b"hello"


def test_strip_grpc_web_drops_trailer_frame():
    assert strip_grpc_web(grpc_frame(b"hello")) == b"hello"


def test_strip_grpc_web_passes_through_unframed_bytes():
    assert strip_grpc_web(b"\x12not a frame") == b"\x12not a frame"


# --------------------------------------------------------------------------------------
# Friend decoding
# --------------------------------------------------------------------------------------


def test_decodes_friends_with_and_without_birthdays():
    friends = decode_friends(sync_response([
        ("janedoe22", "Jane Doe", 3, 14),
        ("sk8rboi", "", None, None),
        ("chris_a", "Chris Adams", 12, 25),
    ]))
    by_user = {f["username"]: f for f in friends}
    assert set(by_user) == {"janedoe22", "sk8rboi", "chris_a"}
    assert (by_user["janedoe22"]["month"], by_user["janedoe22"]["day"]) == (3, 14)
    assert by_user["sk8rboi"]["month"] is None
    assert by_user["sk8rboi"]["name"] == "sk8rboi", "blank display name falls back to handle"


def test_decodes_unicode_display_names():
    friends = decode_friends(sync_response([("jose", "José García 🎉", 2, 29)]))
    assert friends[0]["name"] == "José García 🎉"


@pytest.mark.parametrize("username", ["abc", "a" * 30])
def test_username_length_boundaries_are_accepted(username):
    assert decode_friends(sync_response([(username, "N", 1, 1)]))[0]["username"] == username


def test_usernames_are_deduped():
    dupe = sync_response([("janedoe22", "Jane", 3, 14), ("janedoe22", "Jane", 3, 14)])
    assert len(decode_friends(dupe)) == 1


@pytest.mark.parametrize("month,day", [(0, 14), (13, 1), (3, 0), (3, 32)])
def test_out_of_range_dates_are_rejected_not_emitted(month, day):
    """An impossible date must decode as "no birthday", never reach the calendar."""
    friend = decode_friends(sync_response([("janedoe22", "Jane", month, day)]))[0]
    assert friend["month"] is None and friend["day"] is None


@pytest.mark.parametrize("garbage", [
    b"", b"\x00", b"\x12", b"\x12\xff\xff\xff\xff", b"\x80" * 500,
    bytes(range(256)) * 40, b"<!DOCTYPE html><html>not protobuf</html>",
])
def test_garbage_returns_empty_without_raising(garbage):
    assert decode_friends(garbage) == []


def test_trailer_frame_is_not_decoded_as_a_friend():
    """Only the DATA frame holds friends; a trailer must not add a phantom record."""
    one = sync_response([("janedoe22", "Jane Doe", 3, 14)])
    assert len(decode_friends(one)) == 1


def test_decodes_base64_wrapped_payload():
    import base64
    inner = sync_response([("janedoe22", "Jane Doe", 3, 14)], trailer=False)
    assert decode_friends(base64.b64encode(inner))[0]["username"] == "janedoe22"


# --------------------------------------------------------------------------------------
# UID stability -- this is what stops re-imports duplicating events
# --------------------------------------------------------------------------------------


def test_uid_is_deterministic():
    assert _uid("janedoe22") == _uid("janedoe22")


def test_uid_ignores_display_name_and_birthday_changes():
    """A friend may rename themselves or fix their birthday; the event must still update."""
    before = build_ics([{"username": "j22", "name": "Jane", "month": 3, "day": 14}])
    after = build_ics([{"username": "j22", "name": "Jane Marie Doe 🎈", "month": 7, "day": 1}])
    uid = lambda ics: re.search(r"UID:(\S+)", ics).group(1)
    assert uid(before) == uid(after)
    assert "Jane Marie Doe" in after


def test_distinct_usernames_get_distinct_uids():
    assert _uid("janedoe22") != _uid("janedoe23")


def test_repeated_builds_are_byte_identical():
    friends = [{"username": "j22", "name": "Jane", "month": 3, "day": 14}]
    today = date(2026, 7, 29)
    a = build_ics(friends, today=today)
    b = build_ics(friends, today=today)
    # DTSTAMP is wall-clock, so compare everything else.
    strip = lambda s: re.sub(r"DTSTAMP:\S+", "", s)
    assert strip(a) == strip(b)


# --------------------------------------------------------------------------------------
# ICS conformance
# --------------------------------------------------------------------------------------


def unfold(ics: str) -> list[str]:
    return ics.replace("\r\n ", "").rstrip("\r\n").split("\r\n")


def test_ics_structure_and_line_endings():
    ics = build_ics([{"username": "j22", "name": "Jane", "month": 3, "day": 14}])
    assert ics.startswith("BEGIN:VCALENDAR\r\n")
    assert ics.endswith("END:VCALENDAR\r\n")
    assert "\n" not in ics.replace("\r\n", ""), "no bare LF anywhere"
    assert "VERSION:2.0" in unfold(ics)


def test_all_lines_within_75_octets_even_with_emoji():
    long_name = "Ünïcödé " + "Ǎ" * 60 + " 🎉🎂🎈"
    ics = build_ics([{"username": "u", "name": long_name, "month": 5, "day": 5}])
    for line in ics.split("\r\n"):
        assert len(line.encode("utf-8")) <= 75, repr(line)
    # ...and the value survives being folded and unfolded.
    assert f"SUMMARY:{long_name}'s Birthday" in unfold(ics)


def test_fold_never_splits_a_multibyte_character():
    folded = _fold("SUMMARY:" + "🎂" * 40)
    for physical in folded.split("\r\n"):
        physical.encode("utf-8").decode("utf-8")  # raises if a codepoint was cut
        assert len(physical.encode("utf-8")) <= 75


def test_dtend_is_the_exclusive_next_day():
    ics = unfold(build_ics([{"username": "u", "name": "N", "month": 3, "day": 14}],
                           today=date(2026, 1, 1)))
    assert "DTSTART;VALUE=DATE:20260314" in ics
    assert "DTEND;VALUE=DATE:20260315" in ics


def test_yearly_recurrence_on_every_event():
    ics = unfold(build_ics([
        {"username": "a", "name": "A", "month": 1, "day": 1},
        {"username": "b", "name": "B", "month": 2, "day": 2},
    ]))
    assert ics.count("RRULE:FREQ=YEARLY") == 2


def test_leap_day_anchors_to_feb_28_so_it_recurs_annually():
    ics = unfold(build_ics([{"username": "u", "name": "N", "month": 2, "day": 29}],
                           today=date(2026, 1, 1)))  # 2026 is not a leap year
    assert "DTSTART;VALUE=DATE:20260228" in ics
    assert "RRULE:FREQ=YEARLY" in ics


def test_possessive_for_name_ending_in_s():
    ics = unfold(build_ics([{"username": "u", "name": "Chris", "month": 1, "day": 1}]))
    assert "SUMMARY:Chris' Birthday" in ics


def test_possessive_for_normal_name():
    ics = unfold(build_ics([{"username": "u", "name": "Jane", "month": 1, "day": 1}]))
    assert "SUMMARY:Jane's Birthday" in ics


def test_events_sorted_by_date():
    ics = unfold(build_ics([
        {"username": "c", "name": "C", "month": 12, "day": 1},
        {"username": "a", "name": "A", "month": 1, "day": 5},
        {"username": "b", "name": "B", "month": 1, "day": 2},
    ]))
    order = [l for l in ics if l.startswith("SUMMARY:")]
    assert order == ["SUMMARY:B's Birthday", "SUMMARY:A's Birthday", "SUMMARY:C's Birthday"]


# --------------------------------------------------------------------------------------
# Escaping
# --------------------------------------------------------------------------------------


def test_escape_handles_backslash_first():
    assert _escape("a\\b") == "a\\\\b"
    assert _escape("a\\,b") == "a\\\\\\,b", "backslash escaped before the comma"


@pytest.mark.parametrize("raw,expected", [
    ("a,b", "a\\,b"), ("a;b", "a\\;b"), ("a\nb", "a\\nb"), ("a\r\nb", "a\\nb"),
])
def test_escape_special_characters(raw, expected):
    assert _escape(raw) == expected


def test_special_characters_in_a_name_do_not_break_the_file():
    ics = build_ics([{"username": "u", "name": "Doe, Jane; \\the 3rd\\", "month": 1, "day": 1}])
    summaries = [l for l in unfold(ics) if l.startswith("SUMMARY:")]
    assert len(summaries) == 1
    assert summaries[0] == "SUMMARY:Doe\\, Jane\\; \\\\the 3rd\\\\'s Birthday"
    # A stray unescaped comma would split the value into two -- make sure it did not.
    assert unfold(ics).count("BEGIN:VEVENT") == 1
