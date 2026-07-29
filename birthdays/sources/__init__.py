"""
    birthdays.sources - one module per site we scrape.

    Every source module exposes the same coroutine::

        async def fetch(store: Store, headless: bool = False,
                        timeout: float = 300.0) -> list[Person]

    It opens a real browser window backed by a persistent profile under
    ``<store.root>/profiles/<source>/``, waits until the user is logged in, and
    returns the birthdays it could read.  A source raises ``RuntimeError`` when it
    finds nothing, so the CLI can report a useful message per source and carry on
    with the others.

    This program is free software: you can redistribute it and/or modify it under
    the terms of the GNU General Public License as published by the Free Software
    Foundation, either version 3 of the License, or (at your option) any later
    version.  See <http://www.gnu.org/licenses/>.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Awaitable, Callable

if TYPE_CHECKING:  # pragma: no cover - typing only, keeps import cost at zero
    from ..models import Person

Fetcher = Callable[..., "Awaitable[list[Person]]"]

__all__ = ["AVAILABLE_SOURCES", "get_fetcher", "Fetcher"]

#: Sources the CLI knows how to drive, in the order they are tried.
AVAILABLE_SOURCES: tuple[str, ...] = ("facebook", "snapchat")


def get_fetcher(source: str) -> Any:
    """Return the ``fetch`` coroutine function for ``source``.

    Imported lazily: pulling in a source module drags in its browser plumbing, and
    ``birthdays list`` / ``birthdays status`` need none of it.
    """
    key = (source or "").strip().lower()
    if key == "facebook":
        from .facebook import fetch as facebook_fetch

        return facebook_fetch
    if key == "snapchat":
        from .snapchat import fetch as snapchat_fetch

        return snapchat_fetch
    raise ValueError(
        f"unknown source {source!r}; expected one of {', '.join(AVAILABLE_SOURCES)}"
    )
