#!/usr/bin/env python3
"""snap-birthdays - put your Snapchat friends' birthdays in your calendar.

Snapchat has no API for this. It does have a "Birthdays" feature though, and the web
app fetches every friend's birthday in one protobuf response when it loads. So we open
a real browser, let you log in by hand, read that one response as it goes past, and turn
it into an .ics file.

Usage:
    python snapbirthdays.py                 # fetch, write .ics, open it in Calendar
    python snapbirthdays.py --to google     # ...or open Google Calendar's import page
    python snapbirthdays.py --to file       # ...or just write the file and stop

The protobuf decoding is adapted from Snap2Calendar-Birthday-Export
(MIT, Copyright (c) 2023 James Arnott)
https://github.com/J4A-Industries/Snap2Calendar-Birthday-Export
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import hashlib
import os
import re
import subprocess
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

HOME = Path(os.environ.get("SNAP_BIRTHDAYS_HOME", Path.home() / ".snap-birthdays"))
PROFILE_DIR = HOME / "chrome-profile"
DEFAULT_ICS = HOME / "snapchat-birthdays.ics"

SNAPCHAT_URL = "https://web.snapchat.com/"
#: The gRPC call the web app makes on load, carrying every friend and their birthday.
FRIEND_SYNC_MARKER = "com.snapchat.atlas.gw.AtlasGw/SyncFriendData"

GOOGLE_IMPORT_URL = "https://calendar.google.com/calendar/u/0/r/settings/export"


# --------------------------------------------------------------------------------------
# Protobuf decoding
# --------------------------------------------------------------------------------------
# Snapchat does not publish this schema, so we do not have a .proto to compile against.
# Instead we scan the payload for things shaped like a friend record. Field numbers were
# established by Snap2Calendar and confirmed against a live 220KB response (564 friends).

_F_FRIEND_TAG = 0x12  # field 2, wire type 2 -> the repeated "friends" field
_F_USERNAME, _F_DISPLAY_NAME, _F_BIRTHDAY = 2, 3, 5
_F_MONTH, _F_DAY = 2, 3

# A friend record is bigger than a stray coincidental 0x12 byte and smaller than a photo.
_MIN_RECORD, _MAX_RECORD = 50, 2000
_MIN_USERNAME, _MAX_USERNAME = 3, 30
_MIN_BIRTHDAY, _MAX_BIRTHDAY = 4, 8
_MAX_VARINT_BYTES = 10  # a 64-bit varint cannot be longer; guards against garbage

_BASE64_TEXT_RE = re.compile(rb"^[A-Za-z0-9+/=\s]+$")


def read_varint(data: bytes, pos: int) -> tuple[int, int]:
    """Read a base-128 varint at ``pos``; returns ``(value, next_pos)``."""
    result = shift = 0
    for _ in range(_MAX_VARINT_BYTES):
        if pos >= len(data):
            break
        byte = data[pos]
        result |= (byte & 0x7F) << shift
        pos += 1
        if not byte & 0x80:
            break
        shift += 7
    return result, pos


def parse_fields(data: bytes) -> dict[int, list[tuple[int, object]]]:
    """Parse a protobuf message into ``{field_number: [(wire_type, value), ...]}``.

    Stops at the first byte it cannot make sense of rather than raising: we are parsing
    a slice guessed by a byte scan, so hitting nonsense is expected and not an error.
    """
    fields: dict[int, list[tuple[int, object]]] = {}
    pos = 0
    while pos < len(data):
        tag, pos = read_varint(data, pos)
        field_num, wire_type = tag >> 3, tag & 0x7
        if wire_type == 0:
            value, pos = read_varint(data, pos)
        elif wire_type == 2:
            size, pos = read_varint(data, pos)
            if pos + size > len(data):
                break
            value, pos = data[pos : pos + size], pos + size
        elif wire_type == 1:
            value, pos = None, pos + 8
        elif wire_type == 5:
            value, pos = None, pos + 4
        else:
            break
        fields.setdefault(field_num, []).append((wire_type, value))
    return fields


def strip_grpc_web(body: bytes) -> bytes:
    """Unwrap a gRPC-web frame: 1 flag byte + 4 length bytes + message.

    Returns ``body`` unchanged if it is not a well-formed DATA frame. The TRAILER frame
    that follows the message (flag 0x80) is left off, since it holds no friend data.
    """
    if len(body) > 5 and body[0] == 0x00:
        size = int.from_bytes(body[1:5], "big")
        if 0 < size <= len(body) - 5:
            return body[5 : 5 + size]
    return body


def _decode_text(value: object) -> str:
    if not isinstance(value, (bytes, bytearray)):
        return ""
    try:
        return bytes(value).decode("utf-8")
    except UnicodeDecodeError:
        return ""


def _parse_birthday(data: bytes) -> tuple[int, int] | None:
    """Pull ``(month, day)`` out of a birthday submessage."""
    fields = parse_fields(data)
    month = next((v for wt, v in fields.get(_F_MONTH, ()) if wt == 0), None)
    day = next((v for wt, v in fields.get(_F_DAY, ()) if wt == 0), None)
    if isinstance(month, int) and isinstance(day, int):
        if 1 <= month <= 12 and 1 <= day <= 31:
            return month, day
    return None


def _parse_friend(data: bytes) -> dict | None:
    """Parse one friend record. ``None`` if it does not look like a friend after all."""
    fields = parse_fields(data)

    username = ""
    for wire_type, value in fields.get(_F_USERNAME, ()):
        text = _decode_text(value) if wire_type == 2 else ""
        # Usernames have no spaces; the length bounds reject prose that landed here.
        if text and _MIN_USERNAME <= len(text) <= _MAX_USERNAME and " " not in text:
            username = text
            break
    if not username:
        return None

    display_name = ""
    for wire_type, value in fields.get(_F_DISPLAY_NAME, ()):
        if wire_type == 2 and (text := _decode_text(value)):
            display_name = text
            break

    birthday = None
    for wire_type, value in fields.get(_F_BIRTHDAY, ()):
        if wire_type == 2 and _MIN_BIRTHDAY <= len(value) <= _MAX_BIRTHDAY:
            if found := _parse_birthday(value):
                birthday = found
                break

    return {
        "username": username,
        "name": display_name or username,
        "month": birthday[0] if birthday else None,
        "day": birthday[1] if birthday else None,
    }


def decode_friends(body: bytes) -> list[dict]:
    """Decode a SyncFriendData response body into friend dicts.

    Accepts the raw HTTP body, an already-unwrapped message, or base64 text wrapping
    either. Malformed input yields ``[]`` rather than raising - a bad decode should
    produce a clear "no friends found" message, not a traceback.
    """
    if not body:
        return []
    data = strip_grpc_web(body)

    # Some transports hand the payload back as base64 text rather than bytes.
    if len(data) > 100 and _BASE64_TEXT_RE.match(data):
        try:
            compact = re.sub(rb"\s", b"", data)
            decoded = base64.b64decode(compact + b"=" * (-len(compact) % 4))
            if friends := _scan(strip_grpc_web(decoded)):
                return friends
        except Exception:
            pass

    return _scan(data) or _scan(body)


def _scan(data: bytes) -> list[dict]:
    """Walk the payload looking for length-prefixed friend records."""
    friends: list[dict] = []
    seen: set[str] = set()
    pos, end = 0, len(data) - 10  # a record needs at least a tag, a length and a body
    while pos < end:
        if data[pos] == _F_FRIEND_TAG:
            size, after = read_varint(data, pos + 1)
            if _MIN_RECORD < size < _MAX_RECORD and after + size <= len(data):
                friend = _parse_friend(data[after : after + size])
                if friend and friend["username"] not in seen:
                    seen.add(friend["username"])
                    friends.append(friend)
        pos += 1
    return friends


# --------------------------------------------------------------------------------------
# Fetching
# --------------------------------------------------------------------------------------


def _log(message: str) -> None:
    print(f"  {message}", file=sys.stderr, flush=True)


def _clear_stale_locks() -> None:
    """Remove a Chrome singleton lock left behind by a crashed run.

    Chrome refuses to reuse a profile whose ``SingletonLock`` points at a live process.
    If a previous run was killed the lock survives and every later run fails, so drop it
    once we have confirmed the process it names is gone.
    """
    lock = PROFILE_DIR / "SingletonLock"
    try:
        target = os.readlink(lock)  # "hostname-pid"
        pid = int(target.rsplit("-", 1)[1])
    except (OSError, ValueError, IndexError):
        return
    try:
        os.kill(pid, 0)  # signal 0 only checks existence
    except ProcessLookupError:
        _log(f"clearing stale profile lock from dead pid {pid}")
        for name in ("SingletonLock", "SingletonSocket", "SingletonCookie"):
            (PROFILE_DIR / name).unlink(missing_ok=True)
    except PermissionError:
        pass  # alive but not ours; leave it alone


async def _launch(playwright):
    """Open a persistent browser profile so the hand-done login survives across runs.

    Always headed: Snapchat's web app does not issue the friend sync in a headless
    browser, so there is nothing to capture. Verified - headless waits forever.
    """
    PROFILE_DIR.mkdir(parents=True, exist_ok=True)
    _clear_stale_locks()
    kwargs = {
        "user_data_dir": str(PROFILE_DIR),
        "headless": False,
        "locale": "en-US",
        "viewport": {"width": 1280, "height": 900},
        "args": ["--disable-blink-features=AutomationControlled"],
    }
    # Snapchat's web app is fussy about the browser, so prefer real Chrome. It can refuse
    # to start when your everyday Chrome is already open, hence the Chromium fallback.
    try:
        context = await playwright.chromium.launch_persistent_context(channel="chrome", **kwargs)
        _log("using installed Google Chrome")
    except Exception:
        context = await playwright.chromium.launch_persistent_context(**kwargs)
        _log("using bundled Chromium")
    return context


async def fetch_friends(timeout: float = 300.0) -> list[dict]:
    """Open Snapchat, wait for the friend sync response, and decode it."""
    from playwright.async_api import async_playwright

    bodies: list[bytes] = []

    async def on_response(response) -> None:
        if FRIEND_SYNC_MARKER not in response.url:
            return
        try:
            body = await response.body()
        except Exception:
            return  # body already evicted by the browser; another sync will come
        if body:
            bodies.append(body)
            _log(f"captured friend data ({len(body):,} bytes)")

    async with async_playwright() as playwright:
        context = await _launch(playwright)
        try:
            page = context.pages[0] if context.pages else await context.new_page()
            # Listen before navigating: the sync fires during page load and a listener
            # attached afterwards would miss it entirely.
            page.on("response", lambda r: asyncio.ensure_future(on_response(r)))

            _log(f"opening {SNAPCHAT_URL}")
            await page.goto(SNAPCHAT_URL, wait_until="domcontentloaded")

            print(
                "\n  A browser window is open. Log in to Snapchat there if asked,\n"
                "  then leave it alone - this picks up your friends automatically.\n",
                file=sys.stderr,
                flush=True,
            )

            deadline = asyncio.get_event_loop().time() + timeout
            while not bodies and asyncio.get_event_loop().time() < deadline:
                await page.wait_for_timeout(2000)

            if not bodies:
                raise SystemExit(
                    f"No friend data seen in {timeout:.0f}s.\n"
                    "Log in, open the Chat tab so Snapchat loads your friends, then retry."
                )
        finally:
            await context.close()

    friends = [f for body in bodies for f in decode_friends(body)]
    unique = {f["username"]: f for f in reversed(friends)}  # first capture wins
    return list(unique.values())


# --------------------------------------------------------------------------------------
# ICS output
# --------------------------------------------------------------------------------------


def _escape(text: str) -> str:
    """Escape a value per RFC 5545. Backslash first, or the others get double-escaped."""
    return (
        text.replace("\\", "\\\\")
        .replace(";", "\\;")
        .replace(",", "\\,")
        .replace("\r\n", "\\n")
        .replace("\n", "\\n")
        .replace("\r", "\\n")
    )


def _fold(line: str) -> str:
    """Fold a content line to 75 octets, continuing with a leading space.

    The limit is in octets, not characters, so an accented or emoji-laden name has to be
    measured encoded - and a fold must never land inside a multi-byte character.
    """
    if len(line.encode("utf-8")) <= 75:
        return line
    out: list[str] = []
    chunk, size, limit = "", 0, 75
    for char in line:
        width = len(char.encode("utf-8"))
        if size + width > limit:
            out.append(chunk)
            chunk, size, limit = "", 1, 75  # continuation lines start with a space
        chunk += char
        size += width
    out.append(chunk)
    return "\r\n ".join(out)


def _uid(username: str) -> str:
    """Stable per-friend UID.

    Keyed on the Snapchat username, which never changes, so re-importing updates the
    existing event instead of adding a second one. Nothing here depends on the display
    name or the birthday, both of which a friend can edit.
    """
    return f"snap-{hashlib.sha1(username.encode()).hexdigest()[:20]}@snap-birthdays"


def build_ics(friends: list[dict], calname: str = "Snapchat Birthdays",
              today: date | None = None) -> str:
    """Render friends with birthdays as an iCalendar document."""
    today = today or date.today()
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")

    lines = [
        "BEGIN:VCALENDAR",
        "VERSION:2.0",
        "PRODID:-//snap-birthdays//EN",
        "CALSCALE:GREGORIAN",
        "METHOD:PUBLISH",
        f"X-WR-CALNAME:{_escape(calname)}",
        "X-PUBLISHED-TTL:PT12H",
    ]

    for friend in sorted(friends, key=lambda f: (f["month"], f["day"], f["name"].lower())):
        month, day = friend["month"], friend["day"]
        # Snapchat never shares the birth year, so anchor to this year. A Feb 29 birthday
        # anchored on a leap day would only recur every four years in most clients, so
        # shift it to Feb 28 and let it fire annually.
        if (month, day) == (2, 29):
            day = 28
        start = date(today.year, month, day)
        name = friend["name"]
        possessive = f"{name}'" if name.endswith("s") else f"{name}'s"
        detail = f"Snapchat: {friend['username']}"
        if name != friend["username"]:
            detail = f"Snapchat: {friend['username']} ({name})"

        lines += [
            "BEGIN:VEVENT",
            f"UID:{_uid(friend['username'])}",
            f"DTSTAMP:{stamp}",
            f"SUMMARY:{_escape(possessive)} Birthday",
            f"DTSTART;VALUE=DATE:{start:%Y%m%d}",
            f"DTEND;VALUE=DATE:{start + timedelta(days=1):%Y%m%d}",  # DTEND is exclusive
            "RRULE:FREQ=YEARLY",
            f"DESCRIPTION:{_escape(detail)}",
            "TRANSP:TRANSPARENT",
            "END:VEVENT",
        ]

    lines.append("END:VCALENDAR")
    return "".join(_fold(line) + "\r\n" for line in lines)


# --------------------------------------------------------------------------------------
# Delivery
# --------------------------------------------------------------------------------------


def deliver(path: Path, target: str) -> None:
    """Hand the .ics file off to the user's calendar."""
    if target == "apple":
        if sys.platform != "darwin":
            print("--to apple only works on macOS; the file is written above.")
            return
        subprocess.run(["open", "-a", "Calendar", str(path)], check=True)
        print("\nCalendar will ask which calendar to add these to.")
        print("Pick or create a dedicated one - then you can hide or delete it in one go.")
    elif target == "google":
        subprocess.run(["open", GOOGLE_IMPORT_URL], check=False)
        subprocess.run(["open", "-R", str(path)], check=False)  # reveal in Finder
        print("\nGoogle Calendar's import page is open, and the .ics is selected in Finder.")
        print(f'Drag it into "Import" (or browse to {path}), pick a calendar, hit Import.')
    print("\nRe-run this any time - events are keyed to usernames, so an import updates")
    print("existing birthdays instead of duplicating them.")


# --------------------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Export your Snapchat friends' birthdays to a calendar.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument(
        "--to",
        choices=("apple", "google", "file"),
        default="apple" if sys.platform == "darwin" else "file",
        help="apple: open in Calendar. google: open Google's import page. file: just write it.",
    )
    parser.add_argument("--out", type=Path, default=DEFAULT_ICS, help="where to write the .ics")
    parser.add_argument("--calname", default="Snapchat Birthdays", help="calendar name")
    parser.add_argument("--timeout", type=float, default=300.0,
                        help="seconds to wait for login plus friend data")
    parser.add_argument("--from-file", type=Path, metavar="PATH",
                        help="decode a saved response instead of opening a browser (debugging)")
    args = parser.parse_args(argv)

    if args.from_file:
        friends = decode_friends(args.from_file.read_bytes())
    else:
        try:
            friends = asyncio.run(fetch_friends(args.timeout))
        except KeyboardInterrupt:
            print("\nCancelled.", file=sys.stderr)
            return 130

    with_birthdays = [f for f in friends if f["month"] and f["day"]]
    if not with_birthdays:
        print(f"Found {len(friends)} friends but none had a birthday set.", file=sys.stderr)
        return 1

    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(build_ics(with_birthdays, args.calname), encoding="utf-8")

    hidden = len(friends) - len(with_birthdays)
    print(f"\n{len(with_birthdays)} birthdays from {len(friends)} friends"
          f"{f' ({hidden} keep theirs private)' if hidden else ''}")
    print(f"Wrote {args.out}")

    if args.to != "file":
        deliver(args.out, args.to)
    return 0


if __name__ == "__main__":
    sys.exit(main())
