"""
    birthdays - Snapchat source.

    Drives a REAL, visible Chromium window (Playwright persistent context) at
    https://web.snapchat.com/ so the user can log in by hand (2FA / captcha /
    checkpoints all work), then listens for the gRPC-web ``SyncFriendData``
    response that the web app fires on load and decodes the friend birthdays out
    of it.

    The endpoint, the gRPC-web framing and the protobuf field layout were learned
    from:
      * Snap2Calendar-Birthday-Export
        (https://github.com/J4A-Industries/Snap2Calendar-Birthday-Export)
        MIT licensed, Copyright (c) 2023 James Arnott.
      * fb2cal (https://github.com/mobeigi/fb2cal) GPL-3.0 - overall approach.

    Where Snap2Calendar proxies ``window.fetch`` from a MAIN-world content script,
    we use Playwright's native ``page.on("response")`` listener instead: it sees
    every response regardless of how the app issued it, and cannot be defeated by
    the page reassigning ``fetch``.

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

import asyncio
import hashlib
import sys
import time
from pathlib import Path
from typing import Any, Iterable, Sequence

from ..models import Person
from ..protobuf import decode_snap_friends
from ..store import Store

__all__ = ["fetch", "friends_to_people", "SNAPCHAT_WEB_URL", "SYNC_FRIEND_DATA_MARKER"]

SOURCE = "snapchat"

#: The web app. https://www.snapchat.com/web/ redirects here.
SNAPCHAT_WEB_URL = "https://web.snapchat.com/"

#: Every friend-sync response URL contains this path fragment.
SYNC_FRIEND_DATA_MARKER = "com.snapchat.atlas.gw.AtlasGw/SyncFriendData"

#: Public profile page for a username (used as ``Person.profile_url``).
PROFILE_URL_TEMPLATE = "https://www.snapchat.com/add/{username}"

#: How long to sit quietly waiting for the app's own sync before poking it.
POST_READY_GRACE = 45.0

#: Outcomes of :func:`_wait_for_app`.
APP_READY = "ready"
APP_CLOSED = "closed"
APP_LOGIN_REQUIRED = "login-required"
APP_TIMEOUT = "timeout"

#: Signs the app shell is up (logged in). Any one of these is enough.
READY_SELECTORS: tuple[str, ...] = (
    '[data-testid="chat-list"]',
    '[data-testid="conversation-list"]',
    '[data-testid="conversations-list"]',
    '[data-testid="chat-item"]',
    '[data-testid="new-chat-button"]',
    'button[aria-label="New Chat"]',
    'button[aria-label="Send a Chat"]',
    '[data-testid="camera-button"]',
    'div[role="main"] [role="listbox"]',
)

#: Signs we are still on a login/landing screen.
LOGIN_SELECTORS: tuple[str, ...] = (
    'input[name="accountIdentifier"]',
    'input[name="password"]',
    'input[type="password"]',
    'form[action*="login"]',
    '[data-testid="login-button"]',
    'a[href*="/accounts/login"]',
)

#: Things worth clicking to make the app ask the server for friends again.
CHAT_TAB_SELECTORS: tuple[str, ...] = (
    '[data-testid="chat-tab"]',
    'a[href*="/web/chat"]',
    'a[href$="/chat"]',
    'button[aria-label="Chat"]',
    'button[aria-label="Chats"]',
    '[aria-label="Friends"]',
    'button:has-text("Chats")',
    'button:has-text("Chat")',
)

_UA_BY_PLATFORM = {
    "darwin": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
    ),
    "win32": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
    ),
}
_UA_DEFAULT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
)


def _log(message: str) -> None:
    """Progress chatter on stderr so stdout stays machine-readable."""
    print(f"[{SOURCE}] {message}", file=sys.stderr, flush=True)


def _user_agent() -> str:
    return _UA_BY_PLATFORM.get(sys.platform, _UA_DEFAULT)


# --------------------------------------------------------------------------- #
# pure helpers (no browser, unit-testable)
# --------------------------------------------------------------------------- #
def _coerce_int(value: Any) -> int | None:
    """Best-effort int, returning None for anything that is not one."""
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return None


def _has_birthday(record: dict) -> bool:
    month = _coerce_int(record.get("month"))
    day = _coerce_int(record.get("day"))
    return bool(month and day and 1 <= month <= 12 and 1 <= day <= 31)


def _dedupe_friends(records: Iterable[dict]) -> list[dict]:
    """Union decoded friend dicts, keyed by username.

    Snapchat re-sends the same friend across successive syncs, sometimes without
    the birthday sub-message. Always keep the richest record: one that has a
    birthday beats one that does not, and a real display name beats a blank one.
    """
    best: dict[str, dict] = {}
    for record in records:
        if not isinstance(record, dict):
            continue
        username = str(record.get("username") or "").strip()
        if not username:
            continue
        existing = best.get(username)
        if existing is None:
            best[username] = record
            continue
        if _has_birthday(record) and not _has_birthday(existing):
            best[username] = record
        elif _has_birthday(record) == _has_birthday(existing) and not str(
            existing.get("display_name") or ""
        ).strip():
            if str(record.get("display_name") or "").strip():
                best[username] = record
    return list(best.values())


def friends_to_people(records: Iterable[dict]) -> list[Person]:
    """Turn ``decode_snap_friends`` output into :class:`Person` rows.

    Friends without a usable month/day are dropped - Snapchat only exposes a
    birthday when the friend chose to share it. Snapchat never gives a year.
    """
    people: list[Person] = []
    for record in _dedupe_friends(records):
        username = str(record.get("username") or "").strip()
        month = _coerce_int(record.get("month"))
        day = _coerce_int(record.get("day"))
        if not (month and day) or not (1 <= month <= 12) or not (1 <= day <= 31):
            continue
        display_name = str(record.get("display_name") or "").strip()
        people.append(
            Person(
                source=SOURCE,
                source_id=username,
                name=display_name or username,
                month=month,
                day=day,
                year=None,
                profile_url=PROFILE_URL_TEMPLATE.format(username=username),
                username=username,
            )
        )
    people.sort(key=lambda p: (p.month, p.day, p.name.lower()))
    return people


# --------------------------------------------------------------------------- #
# browser plumbing
# --------------------------------------------------------------------------- #
async def _launch_context(playwright: Any, profile_dir: Path, headless: bool) -> Any:
    """Persistent context from ``profile_dir``; real Chrome if we can get it.

    Snapchat web is fussy about the browser it is talking to, so try the
    installed Chrome channel first (its UA/feature set is genuine) and fall back
    to Playwright's bundled Chromium with a plain desktop UA.
    """
    base: dict[str, Any] = {
        "user_data_dir": str(profile_dir),
        "headless": headless,
        "locale": "en-US",
        "viewport": {"width": 1280, "height": 900},
        "args": ["--disable-blink-features=AutomationControlled"],
        "ignore_default_args": ["--enable-automation"],
    }

    errors: list[str] = []
    for channel in ("chrome", None):
        kwargs = dict(base)
        if channel:
            kwargs["channel"] = channel
        else:
            # Bundled Chromium advertises "HeadlessChrome"/"Chromium"; override.
            kwargs["user_agent"] = _user_agent()
        try:
            context = await playwright.chromium.launch_persistent_context(**kwargs)
        except Exception as exc:  # noqa: BLE001 - fall through to next candidate
            errors.append(f"{channel or 'bundled chromium'}: {exc}")
            continue
        _log(f"browser ready ({channel or 'bundled chromium'}), profile {profile_dir}")
        return context

    raise RuntimeError(
        "Could not launch a browser for Snapchat. Tried Chrome and Playwright's "
        "bundled Chromium:\n  " + "\n  ".join(errors) + "\n"
        "Install the browser with: python -m playwright install chromium"
    )


def _live_page(context: Any, page: Any) -> Any | None:
    """The page we should be looking at, or None if the user closed everything."""
    try:
        if page is not None and not page.is_closed():
            return page
    except Exception:  # noqa: BLE001 - context may already be gone
        pass
    try:
        for candidate in context.pages:
            if not candidate.is_closed():
                return candidate
    except Exception:  # noqa: BLE001
        return None
    return None


async def _any_visible(page: Any, selectors: Sequence[str]) -> bool:
    """True if any selector matches a visible element right now."""
    for selector in selectors:
        try:
            if await page.locator(selector).first.is_visible(timeout=400):
                return True
        except Exception:  # noqa: BLE001 - navigation/detached/bad selector
            continue
    return False


async def _wait_for_app(
    context: Any, page: Any, bodies: list[bytes], deadline: float, headless: bool = False
) -> str:
    """Poll until the app shell is up (or a sync response already landed).

    Returns one of :data:`APP_READY`, :data:`APP_CLOSED`, :data:`APP_LOGIN_REQUIRED` or
    :data:`APP_TIMEOUT`.  The caller needs the distinction: two of those states are
    unsatisfiable by waiting, and waiting anyway is how a five minute budget gets burned
    on a browser that cannot possibly produce anything.
    """
    announced_login = False
    last_note = time.monotonic()
    while time.monotonic() < deadline:
        if bodies:
            return APP_READY
        page = _live_page(context, page)
        if page is None:
            _log("browser window was closed.")
            return APP_CLOSED
        if await _any_visible(page, READY_SELECTORS):
            _log("Snapchat app shell is loaded.")
            return APP_READY
        if await _any_visible(page, LOGIN_SELECTORS):
            if headless:
                # Nothing opened and nothing can be typed: waiting is pointless, and the
                # "log in to the window" advice below would be impossible to follow.
                return APP_LOGIN_REQUIRED
            if not announced_login:
                _log("Log in to Snapchat in the window that just opened, then come back here.")
                announced_login = True
        if time.monotonic() - last_note > 30:
            remaining = int(max(0.0, deadline - time.monotonic()))
            _log(f"still waiting for Snapchat to load... ({remaining}s left)")
            last_note = time.monotonic()
        await asyncio.sleep(1.0)
    return APP_READY if bodies else APP_TIMEOUT


async def _wait_for_bodies(bodies: list[bytes], deadline: float) -> bool:
    """Sleep until at least one SyncFriendData body shows up, or time runs out."""
    while not bodies and time.monotonic() < deadline:
        await asyncio.sleep(0.5)
    return bool(bodies)


async def _click_chat_tab(page: Any, deadline: float) -> bool:
    """Nudge the app into re-syncing friends by opening the Chat/Friends tab.

    Counts before clicking so absent selectors cost nothing instead of burning a
    click timeout each - a dozen 2s waits would eat a meaningful slice of the
    caller's budget.
    """
    for selector in CHAT_TAB_SELECTORS:
        if time.monotonic() >= deadline:
            break
        try:
            locator = page.locator(selector).first
            if await locator.count() == 0:
                continue
            await locator.click(timeout=2000)
            _log(f"clicked {selector}")
            return True
        except Exception:  # noqa: BLE001 - not clickable on this build; try the next
            continue
    return False


# --------------------------------------------------------------------------- #
# public entry point
# --------------------------------------------------------------------------- #
async def fetch(store: Store, headless: bool = False, timeout: float = 300.0) -> list[Person]:
    """Open Snapchat web, capture ``SyncFriendData`` and return friend birthdays.

    Args:
        store: where the browser profile and raw debug dumps live.
        headless: run without a window. Only useful once a session exists -
            the first run needs a visible window to log in.
        timeout: overall budget, in seconds, for login + capture.

    Raises:
        RuntimeError: if no birthdays could be captured or decoded.
    """
    try:
        from playwright.async_api import async_playwright
    except ImportError as exc:  # pragma: no cover - depends on the environment
        raise RuntimeError(
            "Playwright is not installed. Run: pip install playwright && "
            "python -m playwright install chromium"
        ) from exc

    profile_dir = store.profile_dir(SOURCE)
    deadline = time.monotonic() + timeout

    bodies: list[bytes] = []
    seen_hashes: set[str] = set()
    pending: set[asyncio.Task[None]] = set()
    browser_error: str | None = None
    login_required = False

    async def _grab(response: Any) -> None:
        try:
            body = await response.body()
        except Exception as exc:  # noqa: BLE001 - bodies get evicted, that's life
            _log(f"could not read a SyncFriendData body ({exc}); waiting for another")
            return
        if not body:
            return
        digest = hashlib.sha256(body).hexdigest()
        if digest in seen_hashes:
            return
        seen_hashes.add(digest)
        bodies.append(body)
        _log(f"captured SyncFriendData response #{len(bodies)} ({len(body)} bytes)")

    def _on_response(response: Any) -> None:
        try:
            url = response.url or ""
        except Exception:  # noqa: BLE001
            return
        if SYNC_FRIEND_DATA_MARKER not in url:
            return
        task = asyncio.ensure_future(_grab(response))
        pending.add(task)
        task.add_done_callback(pending.discard)

    playwright = await async_playwright().start()
    context = None
    try:
        context = await _launch_context(playwright, profile_dir, headless)

        # Listeners must be live BEFORE navigation: the app fires SyncFriendData
        # during its very first render. Cover every tab the restored profile
        # brought back, plus any popup Snapchat opens later.
        open_pages = [p for p in context.pages if not p.is_closed()]
        if not open_pages:
            open_pages = [await context.new_page()]
        for open_page in open_pages:
            open_page.on("response", _on_response)
        context.on("page", lambda new_page: new_page.on("response", _on_response))
        page = open_pages[0]

        _log(f"opening {SNAPCHAT_WEB_URL}")
        try:
            await page.goto(SNAPCHAT_WEB_URL, wait_until="domcontentloaded", timeout=60_000)
        except Exception as exc:  # noqa: BLE001 - redirects can abort the goto
            _log(f"initial navigation reported {exc}; continuing anyway")

        status = await _wait_for_app(context, page, bodies, deadline, headless=headless)

        if status == APP_LOGIN_REQUIRED and not bodies:
            # Fail immediately rather than polling a login screen nobody can fill in.
            login_required = True
        elif status == APP_CLOSED and not bodies:
            # The window is gone, so no response can ever arrive: every wait below would
            # be dead time. Skip straight to reporting what (if anything) we captured.
            _log("nothing was captured before the window closed.")
        else:
            if status == APP_TIMEOUT and not bodies:
                _log("gave up waiting for the Snapchat app to become ready.")

            # 1) The app usually syncs friends on its own.
            grace = min(deadline, time.monotonic() + POST_READY_GRACE)
            if not await _wait_for_bodies(bodies, grace) and time.monotonic() < deadline:
                # 2) Poke the Chat tab.
                _log("no friend sync seen yet - opening the Chat tab to provoke one...")
                page = _live_page(context, page)
                if page is not None and await _click_chat_tab(page, deadline):
                    await _wait_for_bodies(bodies, min(deadline, time.monotonic() + 20))

            if not bodies and time.monotonic() < deadline:
                # 3) One reload, then wait out the remaining budget.
                _log("still nothing - reloading Snapchat once...")
                page = _live_page(context, page)
                if page is not None:
                    try:
                        await page.reload(wait_until="domcontentloaded", timeout=60_000)
                    except Exception as exc:  # noqa: BLE001
                        _log(f"reload reported {exc}; continuing anyway")
                    await _wait_for_app(
                        context,
                        page,
                        bodies,
                        min(deadline, time.monotonic() + 60),
                        headless=headless,
                    )
                    await _wait_for_bodies(bodies, deadline)

        if pending:
            await asyncio.wait(pending, timeout=15)
    except Exception as exc:  # noqa: BLE001 - report, but still decode what we got
        browser_error = f"{type(exc).__name__}: {exc}"
        _log(f"browser session failed: {browser_error}")
    finally:
        if context is not None:
            try:
                await context.close()
            except Exception:  # noqa: BLE001
                pass
        try:
            await playwright.stop()
        except Exception:  # noqa: BLE001
            pass

    if login_required and not bodies:
        raise RuntimeError(
            "Not logged in to Snapchat and running headless. "
            "Run `birthdays connect snapchat` first to log in by hand."
        )

    return _people_from_bodies(store, bodies, browser_error)


def _people_from_bodies(
    store: Store, bodies: list[bytes], browser_error: str | None
) -> list[Person]:
    """Save raw captures, decode them and turn them into people."""
    records: list[dict] = []
    for index, body in enumerate(bodies, start=1):
        try:
            path = store.save_raw(SOURCE, f"syncfrienddata-{index}.bin", body)
            _log(f"saved raw capture -> {path}")
        except Exception as exc:  # noqa: BLE001 - debug dumps are best-effort
            _log(f"could not save raw capture #{index}: {exc}")
        try:
            decoded = decode_snap_friends(body)
        except Exception as exc:  # noqa: BLE001 - a bad frame shouldn't kill the run
            _log(f"could not decode capture #{index}: {exc}")
            continue
        _log(f"capture #{index}: decoded {len(decoded)} friend record(s)")
        records.extend(decoded)

    people = friends_to_people(records)
    if people:
        total = len({str(r.get('username') or '') for r in records if r.get("username")})
        _log(f"{len(people)} friend(s) with a birthday (out of {total} friends)")
        return people

    raise RuntimeError(_failure_message(store, bodies, records, browser_error))


def _failure_message(
    store: Store, bodies: list[bytes], records: list[dict], browser_error: str | None
) -> str:
    """Actionable text for the 'we got nothing' case."""
    raw_dir = store.root / "raw"
    lines = ["No Snapchat birthdays were captured."]
    if browser_error:
        lines.append(f"The browser session ended with: {browser_error}")
    if not bodies:
        lines += [
            "No SyncFriendData response was seen at all. Usually that means:",
            f"  * you were not logged in at {SNAPCHAT_WEB_URL} before the timeout - "
            "re-run `birthdays connect snapchat` and finish the login in the window,",
            "  * or the window was closed too early - leave it open until this "
            "command prints that it captured a response,",
            "  * or Snapchat changed the endpoint. Check that responses whose URL "
            f"contains '{SYNC_FRIEND_DATA_MARKER}' still exist in DevTools > Network.",
        ]
        lines.append(f"Nothing was written to {raw_dir} this run.")
    elif not records:
        lines += [
            f"Captured {len(bodies)} SyncFriendData response(s) but decoded 0 friends, "
            "so the protobuf layout has probably changed.",
            f"The raw bodies were saved to {raw_dir} - please attach them to a bug report.",
        ]
    else:
        lines += [
            f"Decoded {len(records)} friend(s), but none of them shared a birthday. "
            "Snapchat only exposes a birthday when the friend has chosen to share it.",
            f"Raw bodies are in {raw_dir} if you want to double-check.",
        ]
    return "\n".join(lines)
