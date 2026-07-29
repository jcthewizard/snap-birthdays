"""
    birthdays - friend birthdays from Facebook/Snapchat to an ICS calendar.

    Data models shared by every part of the package.

    Derived from:
      * fb2cal   (https://github.com/mobeigi/fb2cal)                  GPL-3.0
      * Snap2Calendar-Birthday-Export
        (https://github.com/J4A-Industries/Snap2Calendar-Birthday-Export)  MIT

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

from dataclasses import dataclass, field
from typing import Any, Iterable

__all__ = ["Person", "MergedPerson", "SOURCE_LABELS", "source_label"]

#: Pretty names for the sources we know about. Anything else is title-cased.
SOURCE_LABELS: dict[str, str] = {
    "facebook": "Facebook",
    "snapchat": "Snapchat",
}


def source_label(source: str) -> str:
    """Human readable name for a source key (``facebook`` -> ``Facebook``)."""
    key = (source or "").strip().lower()
    return SOURCE_LABELS.get(key, key.title() or "Unknown")


def _clean(value: Any) -> str:
    """Coerce to a stripped string ("" for None)."""
    if value is None:
        return ""
    return str(value).strip()


def _clean_optional(value: Any) -> str | None:
    """Coerce to a stripped string, mapping empty/blank values to None."""
    cleaned = _clean(value)
    return cleaned or None


def _as_int(value: Any, field_name: str) -> int:
    """Coerce a JSON-ish value to int, raising ValueError with a clear message."""
    if isinstance(value, bool) or value is None:
        raise ValueError(f"{field_name} must be an integer, got {value!r}")
    if isinstance(value, int):
        return value
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        raise ValueError(f"{field_name} must be an integer, got {value!r}") from None


def _as_optional_int(value: Any, field_name: str) -> int | None:
    """Like :func:`_as_int` but passes None/blank straight through."""
    if value is None:
        return None
    if isinstance(value, str) and not value.strip():
        return None
    return _as_int(value, field_name)


@dataclass(frozen=True)
class Person:
    """One friend's birthday as reported by a single source.

    Frozen (and therefore hashable) so people can live in sets/dict keys while
    dedupe runs. String fields are stripped and ``source`` is lower-cased on
    construction so equality behaves predictably across sources.
    """

    source: str
    source_id: str
    name: str
    month: int
    day: int
    year: int | None = None
    profile_url: str | None = None
    username: str | None = None

    def __post_init__(self) -> None:
        # Frozen dataclass: normalise via object.__setattr__.
        object.__setattr__(self, "source", _clean(self.source).lower())
        object.__setattr__(self, "source_id", _clean(self.source_id))
        object.__setattr__(self, "name", _clean(self.name))
        object.__setattr__(self, "profile_url", _clean_optional(self.profile_url))
        object.__setattr__(self, "username", _clean_optional(self.username))

        if not self.source:
            raise ValueError("source must be a non-empty string")

        month = _as_int(self.month, "month")
        if not 1 <= month <= 12:
            raise ValueError(f"month must be between 1 and 12, got {month}")
        object.__setattr__(self, "month", month)

        day = _as_int(self.day, "day")
        if not 1 <= day <= 31:
            raise ValueError(f"day must be between 1 and 31, got {day}")
        object.__setattr__(self, "day", day)

        year = _as_optional_int(self.year, "year")
        if year is not None and year <= 0:
            raise ValueError(f"year must be a positive integer, got {year}")
        object.__setattr__(self, "year", year)

    @property
    def label(self) -> str:
        """Best short handle for this person (name, falling back to username)."""
        return self.name or self.username or self.source_id

    def to_dict(self) -> dict:
        """JSON-serialisable representation (round-trips via :meth:`from_dict`)."""
        return {
            "source": self.source,
            "source_id": self.source_id,
            "name": self.name,
            "month": self.month,
            "day": self.day,
            "year": self.year,
            "profile_url": self.profile_url,
            "username": self.username,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "Person":
        """Rebuild a Person from :meth:`to_dict` output.

        Tolerant of missing optional keys and of stringified integers, so it can
        read files written by older versions or by hand.
        """
        if not isinstance(d, dict):
            raise ValueError(f"expected a dict, got {type(d).__name__}")
        return cls(
            source=_clean(d.get("source")),
            source_id=_clean(d.get("source_id")),
            name=_clean(d.get("name")),
            month=_as_int(d.get("month"), "month"),
            day=_as_int(d.get("day"), "day"),
            year=_as_optional_int(d.get("year"), "year"),
            profile_url=_clean_optional(d.get("profile_url")),
            username=_clean_optional(d.get("username")),
        )


@dataclass
class MergedPerson:
    """One calendar event: a person after cross-source dedupe.

    ``uid`` must be deterministic for a given person so re-running ``sync``
    updates the existing event instead of creating a duplicate.
    """

    uid: str
    name: str
    month: int
    day: int
    year: int | None = None
    sources: list[str] = field(default_factory=list)
    members: list[Person] = field(default_factory=list)

    def __post_init__(self) -> None:
        self.uid = _clean(self.uid)
        self.name = _clean(self.name)
        self.month = _as_int(self.month, "month")
        self.day = _as_int(self.day, "day")
        self.year = _as_optional_int(self.year, "year")
        self.members = list(self.members)
        self.sources = list(self.sources) or _unique(m.source for m in self.members)

    @property
    def description(self) -> str:
        """Human-readable provenance block, one line per contributing source.

        e.g.::

            Facebook: Jane Doe (https://facebook.com/123)
            Snapchat: janed (Jane D)
        """
        lines: list[str] = []
        for member in self.members:
            if member.profile_url:
                # The URL is the useful detail, so lead with the human name.
                primary = member.name or member.username or member.source_id
                detail: str | None = member.profile_url
            else:
                # No URL: lead with the handle and clarify it with the name.
                primary = member.username or member.name or member.source_id
                detail = member.name if member.name != primary else None
            line = f"{source_label(member.source)}: {primary}"
            if detail:
                line += f" ({detail})"
            lines.append(line)
        return "\n".join(lines)


def _unique(values: Iterable[str]) -> list[str]:
    """Order-preserving de-duplication of a string iterable."""
    seen: set[str] = set()
    out: list[str] = []
    for value in values:
        if value and value not in seen:
            seen.add(value)
            out.append(value)
    return out
