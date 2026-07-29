"""Regression tests for the Facebook source's browser choreography.

A real Facebook login cannot run in CI, so the browser is faked: every one of these
tests drives the *real* coroutines from :mod:`birthdays.sources.facebook` against a stub
page/context that reproduces one specific state Facebook puts users in (a 2FA
checkpoint, an HTML re-auth interstitial, a page that never fires its own query).  The
production logic is untouched; only Playwright is replaced.
"""

from __future__ import annotations

import asyncio
import json
import time
from urllib.parse import parse_qs

import pytest

from birthdays.sources import facebook as fb
from birthdays.store import Store

CHECKPOINT_URL = (
    "https://www.facebook.com/checkpoint/?next=https%3A%2F%2Fwww.facebook.com"
    "%2Fevents%2Fbirthdays%2F"
)


# --------------------------------------------------------------------------------------
# Fakes
# --------------------------------------------------------------------------------------


class FakeLocator:
    def __init__(self, count: int = 0) -> None:
        self._count = count
        self.clicks = 0

    @property
    def first(self) -> "FakeLocator":
        return self

    async def count(self) -> int:
        return self._count

    async def click(self, timeout: float | None = None) -> None:
        self.clicks += 1


class FakeContext:
    def __init__(self, cookies: list[dict] | None = None, pages: list | None = None) -> None:
        self._cookies = cookies if cookies is not None else [{"name": "c_user", "value": "42"}]
        self.pages = pages or []
        self.closed = False

    async def cookies(self, url: str | None = None) -> list[dict]:
        return list(self._cookies)

    def on(self, event: str, handler) -> None:
        pass

    async def new_page(self):  # pragma: no cover - only used if pages is empty
        raise AssertionError("the fake context always has a page")

    async def close(self) -> None:
        self.closed = True


class CheckpointPage:
    """Signed in (``c_user`` is set) but parked on a 2FA / checkpoint screen.

    ``code`` is the user's half-typed 2FA code: any navigation wipes it, exactly as a
    real page load would.
    """

    def __init__(self, clears_after: int = 3) -> None:
        self.clears_after = clears_after
        self.polls = 0
        self.gotos: list[str] = []
        self.code = "12345"

    @property
    def url(self) -> str:
        return fb.BIRTHDAYS_URL if self.polls > self.clears_after else CHECKPOINT_URL

    def is_closed(self) -> bool:
        self.polls += 1  # called exactly once per poll tick
        return False

    async def goto(self, url: str, **kwargs) -> None:
        self.gotos.append(url)
        self.code = ""  # a navigation destroys whatever the user had typed

    def locator(self, selector: str) -> FakeLocator:
        return FakeLocator(0)


class StubPage:
    """A page that answers everything instantly and never produces a birthday query."""

    def __init__(self, url: str = fb.BIRTHDAYS_URL) -> None:
        self._url = url
        self.evaluated: list[dict] = []

    @property
    def url(self) -> str:
        return self._url

    def is_closed(self) -> bool:
        return False

    async def goto(self, url: str, **kwargs) -> None:
        self._url = url

    def locator(self, selector: str) -> FakeLocator:
        return FakeLocator(0)

    def on(self, event: str, handler) -> None:
        pass

    @property
    def mouse(self):
        return self

    @property
    def keyboard(self):
        return self

    async def wheel(self, x: int, y: int) -> None:
        pass

    async def press(self, key: str) -> None:
        pass

    async def evaluate(self, script: str, arg=None):
        self.evaluated.append(arg)
        return ""


class FakeChromium:
    def __init__(self, context: FakeContext) -> None:
        self._context = context

    async def launch_persistent_context(self, **kwargs):
        return self._context


class FakePlaywright:
    def __init__(self, context: FakeContext) -> None:
        self.chromium = FakeChromium(context)

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc) -> bool:
        return False


# --------------------------------------------------------------------------------------
# _wait_for_login must never navigate away from a checkpoint (facebook.py:403)
# --------------------------------------------------------------------------------------


def test_waiting_for_login_leaves_a_2fa_checkpoint_alone(monkeypatch):
    """Facebook sets ``c_user`` as soon as the password is accepted -- before 2FA.

    Re-navigating on the strength of that cookie wipes the half-typed code, Facebook
    bounces straight back to the checkpoint, and the loop does it again every tick until
    the deadline: the login can never be completed.
    """
    monkeypatch.setattr(fb, "_AUTH_POLL_SECONDS", 0.01)
    page = CheckpointPage(clears_after=3)
    context = FakeContext()

    asyncio.run(fb._wait_for_login(context, page, timeout=5.0))

    assert page.gotos == [], "the user's checkpoint page must not be navigated away"
    assert page.code == "12345", "the half-typed 2FA code was destroyed"
    assert page.polls > 3, "it must keep waiting while the checkpoint is up"


def test_waiting_for_login_returns_once_the_checkpoint_clears(monkeypatch):
    monkeypatch.setattr(fb, "_AUTH_POLL_SECONDS", 0.01)
    page = CheckpointPage(clears_after=2)

    asyncio.run(fb._wait_for_login(FakeContext(), page, timeout=5.0))  # must not raise


def test_waiting_for_login_times_out_when_the_checkpoint_is_never_finished(monkeypatch):
    monkeypatch.setattr(fb, "_AUTH_POLL_SECONDS", 0.01)
    page = CheckpointPage(clears_after=10_000)

    with pytest.raises(RuntimeError, match="Timed out"):
        asyncio.run(fb._wait_for_login(FakeContext(), page, timeout=0.2))

    assert page.gotos == []


# --------------------------------------------------------------------------------------
# An unparseable body is an error, not an empty month (facebook.py:249)
# --------------------------------------------------------------------------------------


HTML_INTERSTITIAL = "<!DOCTYPE html><html><body>Please re-enter your password</body></html>"
TRUNCATED_JSON = '{"data":{"viewer":{"all_friends":{"edges":[{"node":{"id":"1","na'


def _graphql_body(name: str, month: int, day: int) -> str:
    return json.dumps(
        {
            "data": {
                "viewer": {
                    "all_friends": {
                        "edges": [
                            {
                                "node": {
                                    "id": f"id-{name}",
                                    "name": name,
                                    "birthdate": {"month": month, "day": day, "year": None},
                                }
                            }
                        ]
                    }
                }
            }
        }
    )


@pytest.mark.parametrize("body", [HTML_INTERSTITIAL, TRUNCATED_JSON, "not json at all"])
def test_a_body_that_yields_no_json_documents_is_reported_as_an_error(body):
    people, saw_error = fb._people_from_body(body)

    assert people == []
    assert saw_error, "silently reporting success drops a quarter of the year"


def test_a_valid_body_with_no_birthdays_is_not_an_error():
    people, saw_error = fb._people_from_body(json.dumps({"data": {"viewer": {}}}))

    assert people == []
    assert not saw_error, "an empty month is not a failure"


def test_replay_flags_an_error_when_one_offset_month_returns_an_interstitial():
    """Only offset 0 breaks; the run still has to know it is missing Jan-Mar."""
    responses = {0: HTML_INTERSTITIAL}
    for offset in fb.OFFSET_MONTHS[1:]:
        responses[offset] = _graphql_body(f"Friend {offset}", offset + 1, 1)

    class ReplayPage:
        async def evaluate(self, script, arg):
            # The replay body is urlencoded; read back which offset_month it asks for.
            variables = json.loads(parse_qs(arg["body"])["variables"][0])
            return responses[variables["offset_month"]]

    capture = {"friendly_name": fb.FRIENDLY_NAME, "doc_id": "1", "fb_dtsg": "x", "params": {}}
    people, bodies, saw_error = asyncio.run(fb._replay(ReplayPage(), capture, "1"))

    assert len(people) == 3, "the three good quarters must still be kept"
    assert len(bodies) == 4
    assert saw_error, "without this flag the pinned-doc_id retry never runs"


# --------------------------------------------------------------------------------------
# --timeout bounds the whole fetch, not just the login (facebook.py:619)
# --------------------------------------------------------------------------------------


@pytest.mark.parametrize("timeout", [1.0, 3.0])
def test_timeout_bounds_the_whole_fetch_not_only_the_login(tmp_path, monkeypatch, timeout):
    """Everything here is instant except the code's own waits, so the clock measures them.

    Before this was fixed the elapsed time was identical (and enormous) for every
    ``--timeout``: the value reached ``_wait_for_login`` and nothing else, so on the
    normal already-logged-in `sync` path it had no effect at all.
    """
    store = Store(tmp_path / "state")
    context = FakeContext(pages=[StubPage()])
    monkeypatch.setattr(
        "playwright.async_api.async_playwright", lambda: FakePlaywright(context)
    )

    started = time.monotonic()
    with pytest.raises(RuntimeError, match="No Facebook birthdays found"):
        asyncio.run(fb.fetch(store, headless=False, timeout=timeout))
    elapsed = time.monotonic() - started

    assert elapsed <= timeout + 3.0, f"asked for {timeout}s, took {elapsed:.1f}s"
    assert context.closed


def test_the_failure_message_points_at_this_stores_raw_directory(tmp_path, monkeypatch):
    """`--home` / $BIRTHDAYS_HOME move the dumps; the advice has to move with them."""
    store = Store(tmp_path / "elsewhere")
    context = FakeContext(pages=[StubPage()])
    monkeypatch.setattr(
        "playwright.async_api.async_playwright", lambda: FakePlaywright(context)
    )

    with pytest.raises(RuntimeError) as excinfo:
        asyncio.run(fb.fetch(store, headless=False, timeout=1.0))

    message = str(excinfo.value)
    assert str(store.raw_dir) in message
    assert "~/.birthdays/raw" not in message


def test_headless_without_a_session_fails_immediately(tmp_path, monkeypatch):
    """The existing guard, kept honest: no window means nobody can log in."""
    store = Store(tmp_path / "state")
    context = FakeContext(cookies=[], pages=[StubPage(url="https://www.facebook.com/login/")])
    monkeypatch.setattr(
        "playwright.async_api.async_playwright", lambda: FakePlaywright(context)
    )

    started = time.monotonic()
    with pytest.raises(RuntimeError, match="headless"):
        asyncio.run(fb.fetch(store, headless=True, timeout=30.0))

    assert time.monotonic() - started < 5.0
