"""Regression tests for the Snapchat source's waiting logic.

Like the Facebook ones, these drive the real :func:`birthdays.sources.snapchat.fetch`
against a stub browser: the two states under test (headless with no session, and a
window the user closed) are both states in which *no* response can ever arrive, so the
only thing that can be measured is how long the code waits before saying so.
"""

from __future__ import annotations

import asyncio
import time

import pytest

from birthdays.sources import snapchat as sc
from birthdays.store import Store

TIMEOUT = 8.0
#: The fake window is closed this long into the run.
CLOSE_AFTER = 1.0
#: Anything past this and we are waiting on a condition that cannot come true.
FAST = 3.0


class FakeLocator:
    def __init__(self, visible: bool) -> None:
        self._visible = visible

    @property
    def first(self) -> "FakeLocator":
        return self

    async def is_visible(self, timeout: float | None = None) -> bool:
        return self._visible

    async def count(self) -> int:
        return 0


class FakePage:
    """A page sitting on Snapchat's login screen.

    ``closes_after`` models the user shutting the window part-way through the run, which
    is the only way ``_wait_for_app`` can report a closed browser: a context with no open
    page at all never gets that far.
    """

    def __init__(self, closes_after: float | None = None) -> None:
        self.closes_at = None if closes_after is None else time.monotonic() + closes_after
        self.reloads = 0

    def is_closed(self) -> bool:
        return self.closes_at is not None and time.monotonic() >= self.closes_at

    def on(self, event: str, handler) -> None:
        pass

    async def goto(self, url: str, **kwargs) -> None:
        pass

    async def reload(self, **kwargs) -> None:
        self.reloads += 1

    def locator(self, selector: str) -> FakeLocator:
        return FakeLocator(selector in sc.LOGIN_SELECTORS)


class FakeContext:
    def __init__(self, page: FakePage) -> None:
        self.pages = [page]
        self.closed = False

    def on(self, event: str, handler) -> None:
        pass

    async def new_page(self):  # pragma: no cover
        raise AssertionError("the fake context always has a page")

    async def close(self) -> None:
        self.closed = True


class FakePlaywright:
    async def stop(self) -> None:
        pass


def _install(monkeypatch, page: FakePage) -> FakeContext:
    context = FakeContext(page)

    async def _start():
        return FakePlaywright()

    class _Factory:
        def start(self):
            return _start()

    async def _launch(playwright, profile_dir, headless):
        _launch.headless = headless
        return context

    monkeypatch.setattr("playwright.async_api.async_playwright", _Factory)
    monkeypatch.setattr(sc, "_launch_context", _launch)
    return context


def test_headless_without_a_session_fails_immediately(tmp_path, monkeypatch):
    """`birthdays sync --headless` on a fresh or expired profile.

    The old code polled the login screen for the whole 300s default budget while
    printing "log in to the window that just opened" -- advice that cannot be followed,
    because headless means there is no window.  Facebook already fails fast here.
    """
    store = Store(tmp_path / "state")
    _install(monkeypatch, FakePage())

    started = time.monotonic()
    with pytest.raises(RuntimeError, match="headless"):
        asyncio.run(sc.fetch(store, headless=True, timeout=TIMEOUT))
    elapsed = time.monotonic() - started

    assert elapsed < FAST, f"burned {elapsed:.1f}s of a {TIMEOUT}s budget on a dead wait"


def test_headless_message_is_actionable(tmp_path, monkeypatch, capsys):
    store = Store(tmp_path / "state")
    _install(monkeypatch, FakePage())

    with pytest.raises(RuntimeError) as excinfo:
        asyncio.run(sc.fetch(store, headless=True, timeout=TIMEOUT))

    assert "connect snapchat" in str(excinfo.value)
    assert "window that just opened" not in capsys.readouterr().err


def test_a_visible_run_still_waits_for_the_user_to_log_in(tmp_path, monkeypatch, capsys):
    """The fast path must not leak into the interactive case: humans need time."""
    store = Store(tmp_path / "state")
    _install(monkeypatch, FakePage())

    started = time.monotonic()
    with pytest.raises(RuntimeError):
        asyncio.run(sc.fetch(store, headless=False, timeout=3.0))

    assert time.monotonic() - started >= 2.5, "it must actually wait when a window is open"
    assert "Log in to Snapchat in the window" in capsys.readouterr().err


def test_a_closed_window_does_not_burn_the_grace_period(tmp_path, monkeypatch):
    """The user closes the window mid-run; POST_READY_GRACE is then 45s of dead polling.

    ``_wait_for_app`` already logged "browser window was closed", and every later step
    checks for a live page -- only the grace wait did not, so it slept out the whole
    budget on a response that provably cannot arrive.
    """
    store = Store(tmp_path / "state")
    _install(monkeypatch, FakePage(closes_after=CLOSE_AFTER))

    started = time.monotonic()
    with pytest.raises(RuntimeError):
        asyncio.run(sc.fetch(store, headless=False, timeout=TIMEOUT))
    elapsed = time.monotonic() - started

    assert elapsed < FAST, f"waited {elapsed:.1f}s after the browser was already gone"


def test_a_closed_window_still_reports_the_real_failure(tmp_path, monkeypatch):
    store = Store(tmp_path / "state")
    _install(monkeypatch, FakePage(closes_after=CLOSE_AFTER))

    with pytest.raises(RuntimeError, match="No Snapchat birthdays were captured"):
        asyncio.run(sc.fetch(store, headless=False, timeout=TIMEOUT))
