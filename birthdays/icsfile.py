#!/usr/bin/env python3
"""
    birthdays - cross-source friend birthday sync
    icsfile.py - hand rolled RFC 5545 (iCalendar) writer.

    We do not use the `ics` PyPI package (which fb2cal depends on): it is unmaintained
    against modern Python and does not give us byte-exact control over UIDs, folding or
    line endings -- all three of which are load bearing here.

    Derived from / inspired by:
      * fb2cal   (GPL-3.0, Copyright (C) mobeigi)            https://github.com/mobeigi/fb2cal
      * Snap2Calendar-Birthday-Export (MIT, (c) James Arnott) https://github.com/J4A-Industries/Snap2Calendar-Birthday-Export

    This program is free software: you can redistribute it and/or modify it under
    the terms of the GNU General Public License as published by the Free Software
    Foundation, either version 3 of the License, or (at your option) any later
    version.

    This program is distributed in the hope that it will be useful, but WITHOUT
    ANY WARRANTY; without even the implied warranty of MERCHANTABILITY or FITNESS
    FOR A PARTICULAR PURPOSE. See the GNU General Public License for more details.
    You should have received a copy of the GNU General Public License along with
    this program. If not, see <http://www.gnu.org/licenses/>.
"""

from __future__ import annotations

import calendar
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from .models import MergedPerson

__all__ = ["write_ics", "build_ics", "escape_text", "fold_line", "PRODID"]

PRODID = "-//birthdays//Friend Birthday Sync//EN"

CRLF = b"\r\n"

#: RFC 5545 section 3.1: lines are folded so no line is longer than 75 *octets*
#: (excluding the CRLF).  Continuation lines start with a single space, which eats one
#: of the 75, hence 74 bytes of payload after the first chunk.
_FOLD_LIMIT = 75
_FOLD_LIMIT_CONT = _FOLD_LIMIT - 1


# --------------------------------------------------------------------------------------
# Low level RFC 5545 plumbing
# --------------------------------------------------------------------------------------


def escape_text(value: str) -> str:
    r"""Escape a TEXT value per RFC 5545 section 3.3.11.

    Backslash first (otherwise we would escape our own escapes), then semicolon, comma
    and newlines.  Colons are *not* escaped in TEXT values.
    """
    return (
        value.replace("\\", "\\\\")
        .replace(";", "\\;")
        .replace(",", "\\,")
        .replace("\r\n", "\\n")
        .replace("\r", "\\n")
        .replace("\n", "\\n")
    )


def fold_line(line: str) -> bytes:
    """Encode one content line as UTF-8 and fold it to <= 75 octets per physical line.

    Folding is byte based, not character based, but must never split a multi-byte UTF-8
    sequence -- so we walk back off any continuation byte (0b10xxxxxx) before cutting.
    """
    data = line.encode("utf-8")
    if len(data) <= _FOLD_LIMIT:
        return data

    chunks: list[bytes] = []
    start = 0
    limit = _FOLD_LIMIT
    n = len(data)
    while start < n:
        end = min(start + limit, n)
        if end < n:
            # 0b10xxxxxx is a UTF-8 continuation byte: back up to a character boundary.
            while end > start + 1 and (data[end] & 0xC0) == 0x80:
                end -= 1
        chunks.append(data[start:end])
        start = end
        limit = _FOLD_LIMIT_CONT
    return (CRLF + b" ").join(chunks)


def _line(out: list[bytes], name: str, value: str, *, escape: bool = False) -> None:
    """Append one folded content line. ``name`` may carry parameters (``DTSTART;...``)."""
    out.append(fold_line(f"{name}:{escape_text(value) if escape else value}"))


# --------------------------------------------------------------------------------------
# Formatting helpers
# --------------------------------------------------------------------------------------


def _fmt_date(d: date) -> str:
    return d.strftime("%Y%m%d")


def _fmt_stamp(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def _possessive(name: str) -> str:
    """``"Jane"`` -> ``"Jane's"``; ``"Chris"`` -> ``"Chris'"``."""
    n = name.strip()
    if not n:
        return "Friend's"
    return f"{n}'" if n[-1] in "sS" else f"{n}'s"


def _anchor_date(person: MergedPerson, today: date) -> date:
    """First occurrence of the yearly recurrence.

    If we know the birth year we anchor on the birth year (clients such as Apple Calendar
    can then show "turns 36"); otherwise we anchor on the current year.

    Feb 29 is never used as the anchor, whether or not the birth year is known and
    whether or not it is genuine.  RFC 5545 section 3.3.10 derives BYMONTH/BYMONTHDAY for
    FREQ=YEARLY from DTSTART and says instances with an invalid date "MUST be ignored",
    so an honest Feb 29 anchor produces an event that exists only in leap years: Apple
    Calendar and Google Calendar both implement that literally and the birthday is
    invisible three years out of four.  Silently hiding a birthday is worse than showing
    it a day early, so a Feb 29 birthday recurs on Feb 28.  The real birth year is still
    reported, so the "turns N" line in the description stays truthful.
    """
    month, day = person.month, person.day
    if not 1 <= month <= 12:
        raise ValueError(f"invalid birthday month {month!r} for {person.name!r} ({person.uid})")
    known_year = person.year is not None
    year = person.year if known_year else today.year
    # Clamp into the month; this stops one malformed record (e.g. Apr 31) from blowing up
    # the whole export, and covers Feb 29 against a non-leap birth year.
    day = max(1, min(day, calendar.monthrange(year, month)[1]))
    if (month, day) == (2, 29):
        day = 28  # a yearly recurrence anchored on a leap day only fires every 4 years
    return date(year, month, day)


def _description(person: MergedPerson, today: date) -> str:
    """Provenance block from the model, plus an age line when the birth year is known."""
    parts = [person.description.rstrip()]
    if person.year is not None:
        # Age they turn on their *next* birthday relative to `today`.
        upcoming = today.year
        if (person.month, person.day) < (today.month, today.day):
            upcoming += 1
        parts.append(f"Born {person.year} (turns {upcoming - person.year} in {upcoming})")
    return "\n".join(p for p in parts if p)


# --------------------------------------------------------------------------------------
# Calendar building
# --------------------------------------------------------------------------------------


def build_ics(
    people: list[MergedPerson],
    calname: str = "Friend Birthdays",
    today: date | None = None,
    now: datetime | None = None,
) -> bytes:
    """Render the full VCALENDAR as CRLF separated UTF-8 bytes (no BOM)."""
    today = today or date.today()
    now = now or datetime.now(timezone.utc)
    stamp = _fmt_stamp(now)

    out: list[bytes] = []
    _line(out, "BEGIN", "VCALENDAR")
    _line(out, "VERSION", "2.0")
    _line(out, "PRODID", PRODID)
    _line(out, "CALSCALE", "GREGORIAN")
    _line(out, "METHOD", "PUBLISH")
    _line(out, "X-WR-CALNAME", calname, escape=True)
    _line(
        out,
        "X-WR-CALDESC",
        "Friend birthdays collected and de-duplicated by birthdays.",
        escape=True,
    )
    _line(out, "X-PUBLISHED-TTL", "PT12H")

    for person in people:
        start = _anchor_date(person, today)
        end = start + timedelta(days=1)  # DTEND is exclusive for all-day events

        _line(out, "BEGIN", "VEVENT")
        # Stable UID: re-importing this file updates the existing event in place
        # instead of creating a second copy of every birthday.
        _line(out, "UID", person.uid)
        _line(out, "DTSTAMP", stamp)
        _line(out, "SUMMARY", f"{_possessive(person.name)} Birthday", escape=True)
        _line(out, "DTSTART;VALUE=DATE", _fmt_date(start))
        _line(out, "DTEND;VALUE=DATE", _fmt_date(end))
        _line(out, "RRULE", "FREQ=YEARLY")
        _line(out, "DESCRIPTION", _description(person, today), escape=True)
        _line(out, "CLASS", "PUBLIC")
        _line(out, "TRANSP", "TRANSPARENT")
        _line(out, "CREATED", stamp)
        _line(out, "LAST-MODIFIED", stamp)
        _line(out, "END", "VEVENT")

    _line(out, "END", "VCALENDAR")
    return CRLF.join(out) + CRLF


def write_ics(
    people: list[MergedPerson],
    path: Path,
    calname: str = "Friend Birthdays",
    today: date | None = None,
    now: datetime | None = None,
) -> Path:
    """Write ``people`` to ``path`` as an RFC 5545 calendar and return the path written.

    A leading ``~`` is expanded (and the expanded path returned) so that the file lands
    where the user meant, where the calendar import will look for it, and where the
    message we print says it is.  A shell does not always do this for us: ``--out
    "$OUT"``, a quoted argument and a config value all arrive with the tilde intact, and
    writing it verbatim creates a literal ``~`` directory in the working directory.

    ``today`` (and ``now``) are injectable so tests are deterministic.
    """
    path = Path(path).expanduser()
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = build_ics(people, calname=calname, today=today, now=now)
    # Binary mode: we control the line endings (CRLF) and refuse a BOM.
    with open(path, "wb") as fh:
        fh.write(payload)
    return path
