"""Tests for birthdays.protobuf -- the dependency-free Snapchat gRPC-web decoder."""

from __future__ import annotations

import base64

import pytest

from birthdays.protobuf import (
    GRPC_FLAG_DATA,
    GRPC_FLAG_TRAILER,
    MAX_USERNAME_LEN,
    MIN_USERNAME_LEN,
    decode_snap_friends,
    encode_snap_friends_fixture,
    parse_fields,
    read_varint,
    strip_grpc_web,
)

# --------------------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------------------


def by_username(friends: list[dict]) -> dict[str, dict]:
    return {f["username"]: f for f in friends}


THREE_CHAR = "abc"                          # exactly MIN_USERNAME_LEN
THIRTY_CHAR = "a" * 30                      # exactly MAX_USERNAME_LEN

SAMPLE = [
    {
        "username": "janedoe22",
        "display_name": "Jane Doe",
        "month": 3,
        "day": 14,
        "added_ts": 1_600_000_000_000,
    },
    # No birthday at all: Snapchat friends can withhold it.
    {"username": "nobday_friend", "display_name": "No Birthday", "added_ts": 1_600_000_001_000},
    # Unicode display name (accents, CJK and an emoji).
    {"username": "josegarcia", "display_name": "José García 🎉 日本語", "month": 12, "day": 25},
    {"username": THREE_CHAR, "display_name": "Min Handle", "month": 1, "day": 1},
    {"username": THIRTY_CHAR, "display_name": "Max Handle", "month": 2, "day": 29},
]


# --------------------------------------------------------------------------------------
# Round trip
# --------------------------------------------------------------------------------------


def test_roundtrip_decodes_every_friend():
    friends = decode_snap_friends(encode_snap_friends_fixture(SAMPLE))

    assert [f["username"] for f in friends] == [f["username"] for f in SAMPLE]
    assert len(friends) == 5


def test_roundtrip_friend_with_birthday():
    got = by_username(decode_snap_friends(encode_snap_friends_fixture(SAMPLE)))["janedoe22"]

    assert got == {
        "username": "janedoe22",
        "display_name": "Jane Doe",
        "month": 3,
        "day": 14,
        "added_ts": 1_600_000_000_000,
    }


def test_roundtrip_friend_without_birthday():
    got = by_username(decode_snap_friends(encode_snap_friends_fixture(SAMPLE)))["nobday_friend"]

    assert got["display_name"] == "No Birthday"
    assert got["month"] is None
    assert got["day"] is None


def test_roundtrip_unicode_display_name():
    got = by_username(decode_snap_friends(encode_snap_friends_fixture(SAMPLE)))["josegarcia"]

    assert got["display_name"] == "José García 🎉 日本語"
    assert (got["month"], got["day"]) == (12, 25)


@pytest.mark.parametrize("username", [THREE_CHAR, THIRTY_CHAR])
def test_username_length_boundaries_are_inclusive(username):
    """3 and 30 chars are both *accepted* (the bounds are <= / >=, not < / >)."""
    assert len(THREE_CHAR) == MIN_USERNAME_LEN
    assert len(THIRTY_CHAR) == MAX_USERNAME_LEN

    friends = decode_snap_friends(encode_snap_friends_fixture(SAMPLE))
    got = by_username(friends)[username]
    assert got["username"] == username


@pytest.mark.parametrize("username", ["ab", "z" * 31])
def test_usernames_outside_the_bounds_are_rejected(username):
    """Just outside the boundary the record is dropped rather than mis-parsed."""
    payload = encode_snap_friends_fixture([{"username": username, "display_name": "Nope"}])

    assert decode_snap_friends(payload) == []


def test_second_precision_timestamps_are_scaled_to_milliseconds():
    payload = encode_snap_friends_fixture(
        [{"username": "secs_user", "display_name": "Secs", "added_ts": 1_600_000_000}]
    )

    assert decode_snap_friends(payload)[0]["added_ts"] == 1_600_000_000_000


def test_duplicate_usernames_keep_the_first_occurrence():
    payload = encode_snap_friends_fixture(
        [
            {"username": "twinuser", "display_name": "First", "month": 5, "day": 5},
            {"username": "twinuser", "display_name": "Second", "month": 6, "day": 6},
        ]
    )

    friends = decode_snap_friends(payload)
    assert len(friends) == 1
    assert friends[0]["display_name"] == "First"


def test_out_of_range_birthday_is_dropped_but_friend_is_kept():
    payload = encode_snap_friends_fixture(
        [{"username": "badbday", "display_name": "Bad Bday", "month": 13, "day": 40}]
    )

    friend = decode_snap_friends(payload)[0]
    assert friend["username"] == "badbday"
    assert (friend["month"], friend["day"]) == (None, None)


def test_empty_friend_list_roundtrips_to_nothing():
    assert decode_snap_friends(encode_snap_friends_fixture([])) == []


# --------------------------------------------------------------------------------------
# gRPC-web framing
# --------------------------------------------------------------------------------------


def test_strip_grpc_web_removes_header_and_trailing_trailer_frame():
    body = encode_snap_friends_fixture(SAMPLE, include_trailer=True)

    assert body[0] == GRPC_FLAG_DATA
    message = strip_grpc_web(body)

    # The DATA frame length prefix tells us exactly how long the message is.
    declared = int.from_bytes(body[1:5], "big")
    assert len(message) == declared
    # The trailer frame sat after the message and must be gone.
    assert len(body) > 5 + declared
    assert body[5 + declared] == GRPC_FLAG_TRAILER
    assert b"grpc-status" not in message


def test_strip_grpc_web_without_trailer():
    body = encode_snap_friends_fixture(SAMPLE, include_trailer=False)

    assert strip_grpc_web(body) == body[5:]


def test_trailer_frame_does_not_confuse_the_decoder():
    with_trailer = decode_snap_friends(encode_snap_friends_fixture(SAMPLE, include_trailer=True))
    without = decode_snap_friends(encode_snap_friends_fixture(SAMPLE, include_trailer=False))

    assert with_trailer == without


def test_strip_grpc_web_passes_through_unframed_payload():
    raw = b"\x12\x34" * 40  # no plausible gRPC-web header

    assert strip_grpc_web(raw) == raw


def test_strip_grpc_web_passes_through_a_lying_length_prefix():
    """A 0x00 first byte alone is not enough: the declared length must fit."""
    body = b"\x00" + (10_000).to_bytes(4, "big") + b"short"

    assert strip_grpc_web(body) == body


def test_decoder_accepts_an_already_stripped_message():
    body = encode_snap_friends_fixture(SAMPLE)
    inner = strip_grpc_web(body)

    assert decode_snap_friends(inner) == decode_snap_friends(body)


# --------------------------------------------------------------------------------------
# base64 text transport
# --------------------------------------------------------------------------------------


def test_base64_text_wrapped_payload_bytes():
    body = encode_snap_friends_fixture(SAMPLE)
    wrapped = base64.b64encode(body)

    assert len(wrapped) > 100  # the decoder ignores short base64-looking blobs
    assert decode_snap_friends(wrapped) == decode_snap_friends(body)


def test_base64_text_wrapped_payload_str():
    body = encode_snap_friends_fixture(SAMPLE)
    wrapped = base64.b64encode(body).decode("ascii")

    assert decode_snap_friends(wrapped) == decode_snap_friends(body)


def test_base64_text_with_newlines_is_accepted():
    body = encode_snap_friends_fixture(SAMPLE)
    raw = base64.b64encode(body).decode("ascii")
    wrapped = "\n".join(raw[i : i + 76] for i in range(0, len(raw), 76))

    assert decode_snap_friends(wrapped) == decode_snap_friends(body)


def test_base64_of_an_unframed_message_also_decodes():
    inner = strip_grpc_web(encode_snap_friends_fixture(SAMPLE))
    wrapped = base64.b64encode(inner)

    assert decode_snap_friends(wrapped) == decode_snap_friends(inner)


# --------------------------------------------------------------------------------------
# Garbage in, empty list out
# --------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "garbage",
    [
        b"",
        b"\x00",
        b"\xff" * 500,
        b"not a protobuf payload, just prose. " * 20,
        b"<html><body>Please log in</body></html>",
        b"\x00\x00\x00\x00\x00",
        b"\x12",
        b"\x12\xff\xff\xff\xff\xff\xff\xff\xff\xff\xff\xff\xff",  # runaway varint length
        b"{" + b'"error": "unauthorised"' * 10 + b"}",
        base64.b64encode(b"\xff" * 400),  # valid base64 text wrapping junk
        bytearray(b"\x07" * 300),
        memoryview(b"\x09" * 300),
        None,
        "",
    ],
)
def test_garbage_returns_empty_list_without_raising(garbage):
    assert decode_snap_friends(garbage) == []


def test_truncated_body_does_not_raise():
    body = encode_snap_friends_fixture(SAMPLE)
    for cut in range(0, len(body), 7):
        assert isinstance(decode_snap_friends(body[:cut]), list)


# --------------------------------------------------------------------------------------
# Primitives
# --------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "encoded,expected",
    [
        (b"\x00", 0),
        (b"\x01", 1),
        (b"\x7f", 127),
        (b"\x80\x01", 128),
        (b"\xac\x02", 300),
        (b"\xff\xff\xff\xff\x07", 2**31 - 1),
    ],
)
def test_read_varint(encoded, expected):
    value, pos = read_varint(encoded, 0)

    assert value == expected
    assert pos == len(encoded)


def test_read_varint_never_spins_on_a_corrupt_stream():
    value, pos = read_varint(b"\xff" * 64, 0)

    assert isinstance(value, int)
    assert 0 < pos <= 10  # capped at MAX_VARINT_BYTES


def test_parse_fields_handles_truncation_gracefully():
    # field 1, wire type 2 (bytes), declared length 100, but nothing follows.
    assert parse_fields(b"\x0a\x64") == {}
    assert parse_fields(b"") == {}


def test_parse_fields_reads_each_wire_type():
    message = (
        b"\x08\x96\x01"  # field 1, varint 150
        + b"\x12\x03abc"  # field 2, bytes "abc"
        + b"\x25\x01\x00\x00\x00"  # field 4, 32-bit 1
        + b"\x29" + (7).to_bytes(8, "little")  # field 5, 64-bit 7
    )

    fields = parse_fields(message)

    assert fields[1] == [(0, 150)]
    assert fields[2] == [(2, b"abc")]
    assert fields[4] == [(5, 1)]
    assert fields[5] == [(1, 7)]
