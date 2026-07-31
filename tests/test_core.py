"""Tests for snap_birthdays.core.

Run with: uv run pytest

No network and no browser: the protobuf decoder is exercised against payloads built by
the tiny encoder below, the ICS writer against hand-built friend dicts, and the browser
launch against a stub, since that is the whole surface _launch touches.
"""

from __future__ import annotations

import os
import re
import sys
from datetime import date

import pytest

from snap_birthdays.core import (
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


def test_uid_literal_is_frozen():
    """The one value in this file that must never be "fixed" to match new code.

    Once a release ships, this exact string is what sits in strangers' calendars, and it
    is the only thing that lets an import update an event instead of adding a second one.
    Change the prefix, the hash, the truncation or the suffix and every existing user
    silently gets a duplicate copy of all 400 birthdays on their next import. If this
    test fails, the code is wrong, not the constant.
    """
    assert _uid("janedoe22") == "snap-3dd7ed2fcec6f1a5feb2@snap-birthdays"


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


# --------------------------------------------------------------------------------------
# Cross-platform delivery
#
# These matter because the package is installed by strangers on machines that are not a
# Mac. `open` is macOS-only and raises FileNotFoundError even with check=False, so the
# Google path used to crash outright on Linux and Windows.
# --------------------------------------------------------------------------------------


@pytest.mark.parametrize("platform", ["darwin", "linux", "win32"])
def test_google_delivery_works_on_every_platform(platform, monkeypatch, tmp_path, capsys):
    from snap_birthdays import core

    monkeypatch.setattr(core.sys, "platform", platform)
    monkeypatch.setattr(core.webbrowser, "open", lambda url: True)
    monkeypatch.setattr(core.subprocess, "run", lambda *a, **k: None)

    core.deliver(tmp_path / "b.ics", "google")  # must not raise
    assert "import page is open" in capsys.readouterr().out


def test_reveal_survives_a_missing_file_manager(monkeypatch, tmp_path):
    """A headless Linux box has no xdg-open; that must not take the whole run down."""
    from snap_birthdays import core

    def missing(*args, **kwargs):
        raise FileNotFoundError("xdg-open")

    monkeypatch.setattr(core.sys, "platform", "linux")
    monkeypatch.setattr(core.subprocess, "run", missing)
    core.reveal(tmp_path / "b.ics")  # must not raise


def test_apple_delivery_declines_politely_off_mac(monkeypatch, tmp_path, capsys):
    from snap_birthdays import core

    monkeypatch.setattr(core.sys, "platform", "linux")
    called = []
    monkeypatch.setattr(core.subprocess, "run", lambda *a, **k: called.append(a))

    core.deliver(tmp_path / "b.ics", "apple")
    out = capsys.readouterr().out
    assert "macOS only" in out and "--to google" in out
    assert not called, "must not shell out to a macOS-only binary"


# --------------------------------------------------------------------------------------
# Launching a browser
#
# The failure a stranger actually hits is not a bug in the decoder, it is "the browser
# would not start". None of this needs a browser: _launch only ever touches
# playwright.chromium.launch_persistent_context, so a stub is the whole surface.
# --------------------------------------------------------------------------------------

MISSING_BROWSER = (
    "BrowserType.launch_persistent_context: Executable doesn't exist at "
    "/root/.cache/ms-playwright/chromium-1181/chrome-linux/chrome\n"
    "Looks like Playwright was just installed or updated.\n"
    "Please run the following command to download new browsers:\n"
    "    playwright install\n"
)
PROFILE_IN_USE = (
    "BrowserType.launch_persistent_context: Failed to create a ProcessSingleton for your "
    "profile directory. This usually means that the profile is already in use by another "
    "instance of Chromium.\nCall log:\n  - <launching> chrome\n"
)
NO_DISPLAY = (
    "BrowserType.launch_persistent_context: Target page, context or browser has been "
    "closed\nBrowser logs:\n[pid=4711][err] [ERROR:ozone_platform_x11.cc(245)] Missing X "
    "server or $DISPLAY\nCall log:\n  - <launching> chrome\n"
)


class FakePlaywright:
    """Records every launch and replays a scripted outcome for each."""

    def __init__(self, *outcomes):
        self.outcomes = list(outcomes)
        self.calls = []
        self.chromium = self

    async def launch_persistent_context(self, **kwargs):
        self.calls.append(kwargs.get("channel", "bundled"))
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


@pytest.fixture
def launcher(tmp_path, monkeypatch):
    """Keep _launch off the real ~/.snap-birthdays."""
    from snap_birthdays import core

    monkeypatch.setattr(core, "HOME", tmp_path / "home")
    monkeypatch.setattr(core, "PROFILE_DIR", tmp_path / "home" / "chrome-profile")
    installs = []
    monkeypatch.setattr(core, "install_chromium", lambda: installs.append(1))
    return installs


def launch(playwright):
    import asyncio as _asyncio

    from snap_birthdays import core

    return _asyncio.run(core._launch(playwright))


@pytest.mark.parametrize("message,expected", [
    (MISSING_BROWSER, True),
    (PROFILE_IN_USE, False),
    (NO_DISPLAY, False),
    ("Timeout 1ms exceeded.", False),
])
def test_only_a_missing_browser_looks_like_a_missing_browser(message, expected):
    from snap_birthdays import core

    assert core.is_missing_browser(Exception(message)) is expected


def test_a_missing_browser_is_downloaded_once_then_retried(launcher):
    playwright = FakePlaywright(Exception("no chrome channel"), Exception(MISSING_BROWSER), "ctx")
    assert launch(playwright) == "ctx"
    assert launcher == [1], "the one case worth a 170MB download"
    assert playwright.calls == ["chrome", "bundled", "bundled"]


@pytest.mark.parametrize("message,phrase", [
    (PROFILE_IN_USE, "already open"),
    (NO_DISPLAY, "No screen"),
])
def test_other_launch_failures_do_not_trigger_a_download(launcher, message, phrase):
    """A quarter-gigabyte download cannot fix a locked profile or a missing display."""
    playwright = FakePlaywright(Exception("no chrome channel"), Exception(message))

    with pytest.raises(SystemExit) as caught:
        launch(playwright)

    assert launcher == [], "must not download"
    assert playwright.calls == ["chrome", "bundled"], "must not relaunch"
    assert phrase in str(caught.value)


def test_a_failure_after_the_download_is_still_a_sentence(launcher):
    """No raw Playwright blob escapes, even on the retry."""
    playwright = FakePlaywright(
        Exception("no chrome channel"), Exception(MISSING_BROWSER), Exception(NO_DISPLAY))

    with pytest.raises(SystemExit) as caught:
        launch(playwright)

    assert launcher == [1]
    message = str(caught.value)
    assert message.startswith("No screen")
    assert "Call log" not in message, "the browser log belongs in a bug report, not here"


class _Result:
    def __init__(self, returncode):
        self.returncode = returncode


def test_the_download_skips_the_headless_shell(monkeypatch, capsys):
    """This tool is always headed, so the headless shell is ~100MB it can never use."""
    from snap_birthdays import core

    seen = []
    monkeypatch.setattr(core.subprocess, "run",
                        lambda cmd, **kw: seen.append(cmd) or _Result(0))
    core.install_chromium()

    assert seen[0][-3:] == ["install", "--no-shell", "chromium"]
    assert "150MB" not in capsys.readouterr().err, "the old figure was ~3x low"

def test_a_failed_download_says_what_to_run(monkeypatch):
    from snap_birthdays import core

    monkeypatch.setattr(core.subprocess, "run", lambda cmd, **kw: _Result(1))
    with pytest.raises(SystemExit) as caught:
        core.install_chromium()
    assert "--no-shell chromium" in str(caught.value)


def test_the_cli_reports_a_browser_failure_without_a_traceback(monkeypatch, capsys, tmp_path):
    """`snap-birthdays-cli` is a documented entry point; it must not end in asyncio frames."""
    from snap_birthdays import core

    async def closed(*args, **kwargs):
        raise RuntimeError("Page.wait_for_timeout: Target page has been closed\nCall log:\n  - x")

    monkeypatch.setattr(core, "fetch_friends", closed)
    code = core.main(["--to", "file", "--out", str(tmp_path / "b.ics")])

    err = capsys.readouterr().err
    assert code == 1
    assert err.strip() == "Page.wait_for_timeout: Target page has been closed"


# --------------------------------------------------------------------------------------
# File permissions
#
# The .ics and the friend cache are several hundred other people's names and birthdays.
# At the default umask they would land 0644 in a 0755 directory - readable by every other
# account on the machine.
# --------------------------------------------------------------------------------------


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX modes")
def test_written_files_are_readable_only_by_this_user(tmp_path, monkeypatch):
    from snap_birthdays import core

    monkeypatch.setattr(core, "HOME", tmp_path / "home")
    old = os.umask(0o022)
    try:
        core.write_private(tmp_path / "home" / "sub" / "b.ics", "x")
    finally:
        os.umask(old)

    assert (tmp_path / "home" / "sub" / "b.ics").stat().st_mode & 0o777 == 0o600
    assert (tmp_path / "home").stat().st_mode & 0o777 == 0o700


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX modes")
def test_an_existing_open_home_is_tightened(tmp_path, monkeypatch):
    """An earlier version left it 0755; the next run should not leave it that way."""
    from snap_birthdays import core

    home = tmp_path / "home"
    home.mkdir(mode=0o755)
    monkeypatch.setattr(core, "HOME", home)
    core.write_private(home / "friends.json", "{}")

    assert home.stat().st_mode & 0o777 == 0o700


def test_a_stale_temp_file_does_not_leak_its_mode(tmp_path, monkeypatch):
    from snap_birthdays import core

    monkeypatch.setattr(core, "HOME", tmp_path / "home")
    target = tmp_path / "out.ics"
    stale = tmp_path / "out.ics.tmp"
    stale.write_text("old")
    os.chmod(stale, 0o666)

    core.write_private(target, "new")
    assert target.read_text() == "new"
    if sys.platform != "win32":
        assert target.stat().st_mode & 0o777 == 0o600


def test_the_cli_writes_the_ics_privately(tmp_path, monkeypatch):
    from snap_birthdays import core

    payload = sync_response([("janedoe22", "Jane", 3, 14)])
    source = tmp_path / "payload.bin"
    source.write_bytes(payload)
    out = tmp_path / "nested" / "b.ics"

    assert core.main(["--from-file", str(source), "--to", "file", "--out", str(out)]) == 0
    assert "BEGIN:VEVENT" in out.read_text(encoding="utf-8")
    if sys.platform != "win32":
        assert out.stat().st_mode & 0o777 == 0o600
