"""Minimal, dependency-free protobuf / gRPC-web decoding for Snapchat friend data.

This module is a Python port of the TypeScript decoder shipped with
Snap2Calendar-Birthday-Export (https://github.com/J4A-Industries/Snap2Calendar-Birthday-Export,
MIT licensed) -- specifically ``src/background/decoder.ts`` and
``src/background/messages/processFriendData.ts``.

The wider ``birthdays`` project also derives from fb2cal (https://github.com/mobeigi/fb2cal),
which is GPL-3.0, so this file is distributed under the GNU General Public License v3.0.

Wire format notes (verified against the reference implementation)
----------------------------------------------------------------
Endpoint::

    https://(web|www).snapchat.com/com.snapchat.atlas.gw.AtlasGw/SyncFriendData

The HTTP body is a gRPC-web stream of frames.  Each frame is::

    1 byte flags | 4 bytes big-endian length | <length> bytes payload

The first frame (flags ``0x00``) holds the protobuf ``SyncFriendData`` response.  A
second TRAILER frame (flags ``0x80``, ASCII ``grpc-status: 0`` etc.) usually follows and
is ignored.  Some transports hand back the payload as *base64 text* instead of raw
bytes, so both are accepted.

There is no ``.proto`` available, so friend records are located heuristically: scan for
the tag byte ``0x12`` (field 2, wire type 2 == length-delimited) followed by a varint
length in ``(50, 2000)``, then parse that slice as a friend submessage.

Friend submessage fields::

    2 -> username        (utf-8, 3..30 chars, no spaces)
    3 -> display name    (utf-8)
    5 -> birthday submessage (4..8 bytes): field 2 = month, field 3 = day
    6 -> "added" timestamp (varint)

Snapchat exposes **month + day only** -- never a birth year.

Every public function here is defensive: malformed input yields empty/neutral results
rather than an exception.
"""

from __future__ import annotations

import base64
import re
import struct
from typing import Any

__all__ = [
    "read_varint",
    "parse_fields",
    "strip_grpc_web",
    "decode_snap_friends",
    "encode_snap_friends_fixture",
]

# --------------------------------------------------------------------------------------
# Tunables (kept identical to the reference implementation's magic numbers)
# --------------------------------------------------------------------------------------

#: gRPC-web frame header: 1 byte flags + 4 bytes big-endian length.
GRPC_HEADER_LEN = 5

#: Flag byte for a DATA frame (uncompressed).
GRPC_FLAG_DATA = 0x00
#: Flag byte for a TRAILER frame -- present at the end of most responses, always ignored.
GRPC_FLAG_TRAILER = 0x80

#: A candidate friend record must be strictly longer/shorter than these.
MIN_FRIEND_RECORD_LEN = 50
MAX_FRIEND_RECORD_LEN = 2000

#: Username sanity bounds used to reject stray length-delimited fields.
MIN_USERNAME_LEN = 3
MAX_USERNAME_LEN = 30

#: Birthday submessage sanity bounds.
MIN_BIRTHDAY_LEN = 4
MAX_BIRTHDAY_LEN = 8

#: Protobuf varints are at most 10 bytes (64-bit value + continuation bits).
MAX_VARINT_BYTES = 10

#: Field numbers inside the friend submessage.
_F_USERNAME = 2
_F_DISPLAY_NAME = 3
_F_BIRTHDAY = 5
_F_ADDED_TS = 6
#: Field numbers inside the birthday submessage.
_F_BDAY_MONTH = 2
_F_BDAY_DAY = 3
#: Field number of a repeated friend inside the top-level response.
_F_FRIEND = 2
#: Unused field we stuff filler bytes into when building test fixtures.
_F_FIXTURE_PADDING = 15

_BASE64_TEXT_RE = re.compile(rb"^[A-Za-z0-9+/=\s]+$")
#: The reference only treats a body as base64 text when it is comfortably long.
_MIN_BASE64_TEXT_LEN = 100

#: Timestamps below this are assumed to be seconds and are scaled to milliseconds.
_TS_SECONDS_CEILING = 100_000_000_000
#: Anything below this (year 2001 in seconds) is not a plausible "friend added" stamp.
_TS_FLOOR = 1_000_000_000


# --------------------------------------------------------------------------------------
# Low-level protobuf primitives
# --------------------------------------------------------------------------------------


def read_varint(data: bytes, pos: int) -> tuple[int, int]:
    """Read a base-128 varint from ``data`` starting at ``pos``.

    Returns ``(value, new_pos)``.  ``new_pos`` is always ``>= pos`` and advances by at
    least one byte whenever ``pos`` is in range, so callers can loop safely.

    Unlike the JavaScript original -- which had to use multiplication to dodge 32-bit
    bit-shift overflow -- Python ints are arbitrary precision, so plain shifts are fine.
    Reading is capped at :data:`MAX_VARINT_BYTES` bytes so a corrupt stream of ``0xFF``
    can never spin forever or produce an astronomically large shift.
    """
    if pos < 0:
        pos = 0
    length = len(data)
    result = 0
    shift = 0
    consumed = 0
    while pos < length and consumed < MAX_VARINT_BYTES:
        byte = data[pos]
        result |= (byte & 0x7F) << shift
        pos += 1
        consumed += 1
        if not byte & 0x80:
            break
        shift += 7
    return result, pos


def parse_fields(data: bytes) -> dict[int, list[tuple[int, Any]]]:
    """Parse a protobuf message into ``{field_number: [(wire_type, value), ...]}``.

    Values are:

    * wire type 0 (varint)  -> ``int``
    * wire type 1 (64-bit)  -> ``int`` (little-endian, unsigned)
    * wire type 2 (bytes)   -> ``bytes``
    * wire type 5 (32-bit)  -> ``int`` (little-endian, unsigned)

    Unknown/legacy wire types (3 and 4, deprecated groups) terminate parsing and the
    fields decoded so far are returned.  The same is true for any truncation: this is a
    best-effort scanner over data whose schema we do not have, so it never raises.
    """
    fields: dict[int, list[tuple[int, Any]]] = {}
    if not data:
        return fields

    view = bytes(data)
    length = len(view)
    pos = 0
    while pos < length:
        tag, pos = read_varint(view, pos)
        field_num = tag >> 3
        wire_type = tag & 0x07
        if field_num == 0:
            # Field 0 is illegal; we are almost certainly out of sync with the stream.
            return fields

        value: Any
        if wire_type == 0:
            value, pos = read_varint(view, pos)
        elif wire_type == 1:
            if pos + 8 > length:
                return fields
            value = struct.unpack_from("<Q", view, pos)[0]
            pos += 8
        elif wire_type == 2:
            size, pos = read_varint(view, pos)
            if size < 0 or pos + size > length:
                return fields
            value = view[pos : pos + size]
            pos += size
        elif wire_type == 5:
            if pos + 4 > length:
                return fields
            value = struct.unpack_from("<I", view, pos)[0]
            pos += 4
        else:
            return fields

        fields.setdefault(field_num, []).append((wire_type, value))

    return fields


# --------------------------------------------------------------------------------------
# gRPC-web framing
# --------------------------------------------------------------------------------------


def strip_grpc_web(body: bytes) -> bytes:
    """Return the inner protobuf message of a gRPC-web response body.

    ``body`` is expected to start with a DATA frame (``0x00`` + 4-byte big-endian
    length).  Any following TRAILER frame (``0x80``) is discarded.  If ``body`` does not
    carry a plausible gRPC-web header it is returned unchanged.
    """
    data = _as_bytes(body)
    if len(data) <= GRPC_HEADER_LEN:
        return data
    if data[0] != GRPC_FLAG_DATA:
        return data
    message_len = int.from_bytes(data[1:5], "big")
    if message_len <= 0 or message_len > len(data) - GRPC_HEADER_LEN:
        return data
    return data[GRPC_HEADER_LEN : GRPC_HEADER_LEN + message_len]


# --------------------------------------------------------------------------------------
# Friend decoding
# --------------------------------------------------------------------------------------


def decode_snap_friends(payload: bytes) -> list[dict]:
    """Decode Snapchat ``SyncFriendData`` bytes into friend dictionaries.

    ``payload`` may be any of:

    * the raw gRPC-web HTTP body (DATA frame + optional TRAILER frame),
    * the already-stripped protobuf message,
    * base64 *text* (bytes or ``str``) wrapping either of the above.

    Each returned dict has the shape::

        {"username": str, "display_name": str,
         "month": int | None, "day": int | None, "added_ts": int | None}

    ``added_ts`` is normalised to **milliseconds** since the epoch (matching the
    reference implementation, which scales second-precision values by 1000).  Records
    without a username are skipped and usernames are deduped, first occurrence winning.

    Malformed input returns ``[]`` rather than raising.
    """
    try:
        data = _as_bytes(payload)
    except Exception:
        return []
    if not data:
        return []

    try:
        stripped = strip_grpc_web(data)

        # 1. Some transports hand back base64 *text* inside the frame.
        inner = _try_base64_text(stripped)
        if inner is not None:
            friends = _scan_friends(strip_grpc_web(inner))
            if friends:
                return friends
            friends = _scan_friends(inner)
            if friends:
                return friends

        # 2. Normal case: raw protobuf.
        friends = _scan_friends(stripped)
        if friends:
            return friends

        # 3. Last resort: maybe our header detection was a false positive.
        if stripped != data:
            return _scan_friends(data)
        return []
    except Exception:
        return []


def _scan_friends(data: bytes) -> list[dict]:
    """Scan ``data`` for ``0x12``-tagged friend submessages."""
    friends: list[dict] = []
    seen: set[str] = set()
    length = len(data)
    pos = 0
    # ``length - 10`` mirrors the reference: a record needs a tag, a length and a body.
    while pos < length - 10:
        if data[pos] == 0x12:
            try:
                size, after_len = read_varint(data, pos + 1)
                if (
                    MIN_FRIEND_RECORD_LEN < size < MAX_FRIEND_RECORD_LEN
                    and after_len + size <= length
                ):
                    friend = _parse_friend(data[after_len : after_len + size])
                    if friend is not None and friend["username"] not in seen:
                        seen.add(friend["username"])
                        friends.append(friend)
            except Exception:
                pass
        pos += 1
    return friends


def _parse_friend(data: bytes) -> dict | None:
    """Parse one friend submessage; ``None`` when no usable username is present."""
    fields = parse_fields(data)

    username = ""
    for wire_type, value in fields.get(_F_USERNAME, ()):
        if wire_type != 2:
            continue
        text = _try_utf8(value)
        if (
            text
            and MIN_USERNAME_LEN <= len(text) <= MAX_USERNAME_LEN
            and " " not in text
        ):
            username = text
            break
    if not username:
        return None

    display_name = ""
    for wire_type, value in fields.get(_F_DISPLAY_NAME, ()):
        if wire_type != 2:
            continue
        text = _try_utf8(value)
        if text:
            display_name = text
            break

    month: int | None = None
    day: int | None = None
    for wire_type, value in fields.get(_F_BIRTHDAY, ()):
        if wire_type != 2 or not (MIN_BIRTHDAY_LEN <= len(value) <= MAX_BIRTHDAY_LEN):
            continue
        parsed_month, parsed_day = _parse_birthday(value)
        if parsed_month is not None and parsed_day is not None:
            month, day = parsed_month, parsed_day
            break

    added_ts: int | None = None
    for wire_type, value in fields.get(_F_ADDED_TS, ()):
        if wire_type == 0 and isinstance(value, int) and value > _TS_FLOOR:
            added_ts = value * 1000 if value < _TS_SECONDS_CEILING else value
            break

    return {
        "username": username,
        "display_name": display_name,
        "month": month,
        "day": day,
        "added_ts": added_ts,
    }


def _parse_birthday(data: bytes) -> tuple[int | None, int | None]:
    """Parse the birthday submessage -> ``(month, day)``; ``(None, None)`` if invalid."""
    fields = parse_fields(data)
    month: int | None = None
    day: int | None = None

    for wire_type, value in fields.get(_F_BDAY_MONTH, ()):
        if wire_type == 0:
            month = value
            break
    for wire_type, value in fields.get(_F_BDAY_DAY, ()):
        if wire_type == 0:
            day = value
            break

    if month is None or day is None:
        return None, None
    if not (1 <= month <= 12) or not (1 <= day <= 31):
        return None, None
    return month, day


# --------------------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------------------


def _as_bytes(payload: object) -> bytes:
    """Coerce str/bytes/bytearray/memoryview/iterable-of-int to ``bytes``."""
    if isinstance(payload, bytes):
        return payload
    if isinstance(payload, (bytearray, memoryview)):
        return bytes(payload)
    if isinstance(payload, str):
        return payload.encode("utf-8", errors="surrogateescape")
    if payload is None:
        return b""
    return bytes(payload)  # type: ignore[arg-type]


def _try_utf8(value: object) -> str | None:
    """Strict utf-8 decode; ``None`` when the bytes are not valid text."""
    if not isinstance(value, (bytes, bytearray, memoryview)):
        return None
    try:
        return bytes(value).decode("utf-8")
    except UnicodeDecodeError:
        return None


def _try_base64_text(data: bytes) -> bytes | None:
    """If ``data`` looks like base64 *text*, return the decoded bytes, else ``None``."""
    if len(data) <= _MIN_BASE64_TEXT_LEN:
        return None
    if not _BASE64_TEXT_RE.match(data):
        return None
    compact = re.sub(rb"\s", b"", data)
    if len(compact) <= _MIN_BASE64_TEXT_LEN:
        return None
    compact += b"=" * (-len(compact) % 4)
    try:
        decoded = base64.b64decode(compact, validate=False)
    except Exception:
        return None
    return decoded or None


def _encode_varint(value: int) -> bytes:
    """Encode a non-negative int as a base-128 varint."""
    if value < 0:
        raise ValueError("varint values must be non-negative")
    out = bytearray()
    while True:
        byte = value & 0x7F
        value >>= 7
        if value:
            out.append(byte | 0x80)
        else:
            out.append(byte)
            return bytes(out)


def _encode_tag(field_num: int, wire_type: int) -> bytes:
    return _encode_varint((field_num << 3) | wire_type)


def _encode_bytes_field(field_num: int, value: bytes) -> bytes:
    return _encode_tag(field_num, 2) + _encode_varint(len(value)) + value


def _encode_varint_field(field_num: int, value: int) -> bytes:
    return _encode_tag(field_num, 0) + _encode_varint(value)


# --------------------------------------------------------------------------------------
# Test fixture builder (lives here so it stays in sync with the decoder)
# --------------------------------------------------------------------------------------


def encode_snap_friends_fixture(
    friends: list[dict],
    *,
    include_trailer: bool = True,
) -> bytes:
    """Build a synthetic gRPC-web ``SyncFriendData``-shaped body for tests.

    ``friends`` is a list of ``{"username", "display_name", "month", "day", "added_ts"}``
    dicts (all keys optional except ``username``).  The result round-trips through
    :func:`decode_snap_friends`, so tests never need a real Snapchat account.

    Caveats that mirror real decoding behaviour:

    * ``username`` must be 3..30 chars with no spaces or the decoder will drop the record.
    * ``month``/``day`` are omitted entirely when either is falsy (the "no birthday" case).
    * ``added_ts`` is written verbatim; pass **milliseconds** for an exact round-trip
      (second-precision values get scaled by 1000 on the way out, like the real API).
    * Records are padded with filler bytes so their length clears the decoder's
      ``> 50 byte`` heuristic, exactly as real records (bitmoji ids, streaks, ...) do.
    """
    message = bytearray()
    for friend in friends or []:
        message += _encode_bytes_field(_F_FRIEND, _encode_friend_record(friend))

    body = bytearray()
    body.append(GRPC_FLAG_DATA)
    body += len(message).to_bytes(4, "big")
    body += message

    if include_trailer:
        trailer = b"grpc-status:0\r\ngrpc-message:\r\n"
        body.append(GRPC_FLAG_TRAILER)
        body += len(trailer).to_bytes(4, "big")
        body += trailer

    return bytes(body)


def _encode_friend_record(friend: dict) -> bytes:
    username = str(friend.get("username") or "")
    display_name = str(friend.get("display_name") or "")
    month = friend.get("month")
    day = friend.get("day")
    added_ts = friend.get("added_ts")

    record = bytearray()
    record += _encode_bytes_field(_F_USERNAME, username.encode("utf-8"))
    if display_name:
        record += _encode_bytes_field(_F_DISPLAY_NAME, display_name.encode("utf-8"))
    if month and day:
        birthday = _encode_varint_field(_F_BDAY_MONTH, int(month)) + _encode_varint_field(
            _F_BDAY_DAY, int(day)
        )
        record += _encode_bytes_field(_F_BIRTHDAY, birthday)
    if added_ts:
        record += _encode_varint_field(_F_ADDED_TS, int(added_ts))

    # Pad past MIN_FRIEND_RECORD_LEN with an inert field the decoder ignores.  Filler
    # bytes are '.' (0x2e) so they can never be mistaken for a 0x12 record tag.
    target = MIN_FRIEND_RECORD_LEN + 6
    if len(record) < target:
        filler_len = max(0, target - len(record) - 2)
        record += _encode_bytes_field(_F_FIXTURE_PADDING, b"." * filler_len)

    return bytes(record)
