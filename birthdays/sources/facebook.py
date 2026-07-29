"""Facebook birthday source.

Opens a real, visible Chromium window backed by a persistent Playwright profile so the
user can log in by hand once (2FA / captcha / checkpoints all work), then learns the
GraphQL query that powers https://www.facebook.com/events/birthdays/ by watching the
page's own network traffic and replays it for the rest of the year.

Endpoint / protocol knowledge (GraphQL birthday query, the
``BirthdayCometMonthlyBirthdaysRefetchQuery`` friendly name, the ``offset_month`` +
``scale`` variables, the "offset month plus the following two months" behaviour, the
``for (;;);`` anti-hijacking prefix and the ``c_user`` cookie auth check) is derived from
fb2cal by Mohammad Beigi et al. -- https://github.com/mobeigi/fb2cal -- which is licensed
under the GNU General Public License v3.0.  Because of that derivation this project is
also GPL-3.0.

Unlike fb2cal we do NOT perform a scripted password login (mechanicalsoup + encrypted
password); it trips Facebook checkpoints and cannot handle 2FA.  We also refuse to pin a
``doc_id`` or a response shape, both of which rotate: the doc_id is learned at runtime
from the live page and the JSON is parsed with a recursive, shape-agnostic walk.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import re
import sys
import time
from typing import TYPE_CHECKING, Any, Iterable
from urllib.parse import parse_qs, urlencode

from ..models import Person

if TYPE_CHECKING:  # pragma: no cover - import kept out of runtime for browser-free tests
    from ..store import Store

SOURCE = "facebook"

BIRTHDAYS_URL = "https://www.facebook.com/events/birthdays/"
GRAPHQL_URL = "https://www.facebook.com/api/graphql/"
GRAPHQL_GLOB = "**/api/graphql/**"

#: fb2cal's pinned doc_id.  Almost certainly stale -- only ever used as a last-resort
#: retry after the doc_id we learned from the live page fails.
PINNED_DOC_ID = "5347559575302259"

#: The endpoint returns the offset month *plus the following two months*, so 0/3/6/9
#: covers a full year.
OFFSET_MONTHS = (0, 3, 6, 9)

FRIENDLY_NAME = "BirthdayCometMonthlyBirthdaysRefetchQuery"

_ANTI_HIJACK_RE = re.compile(r"\s*for\s*\(\s*;\s*;\s*\)\s*;")
_LOGIN_URL_RE = re.compile(r"/(login|checkpoint|recover|two_factor|authentication)", re.I)

_MAX_STASHED_BODIES = 60
_MAX_STASHED_BYTES = 24 * 1024 * 1024
_AUTH_POLL_SECONDS = 2.0
_QUERY_OBSERVE_TIMEOUT = 60.0
_NAVIGATION_TIMEOUT_MS = 60_000
_REPLAY_TIMEOUT = 45.0

#: Below this much budget left, there is no point starting the harvest at all; say so
#: instead of "no birthdays found", which would blame the wrong thing.
_MIN_HARVEST_SECONDS = 15.0

#: Even with no budget left, a call gets this much time rather than an instant abort:
#: cutting a request off at 0ms would turn "you asked for a short timeout" into "every
#: request fails", which is not what the user asked for either.
_MIN_STEP_MS = 1_000.0


def _remaining(deadline: float | None) -> float:
    """Seconds left before ``deadline`` (infinite when there is no deadline)."""
    if deadline is None:
        return float("inf")
    return max(0.0, deadline - time.monotonic())


def _step_timeout(deadline: float | None, cap_seconds: float) -> float:
    """A per-step budget that respects both the step's own cap and the overall deadline."""
    return max(_MIN_STEP_MS / 1000.0, min(cap_seconds, _remaining(deadline)))


def log(message: str) -> None:
    """Print a progress line to stderr (stdout stays clean for piped output)."""
    print(f"[{SOURCE}] {message}", file=sys.stderr, flush=True)


# --------------------------------------------------------------------------------------
# Pure parsing -- no browser required, fully unit testable
# --------------------------------------------------------------------------------------


def _as_int(value: Any) -> int | None:
    """Coerce a JSON scalar to int, rejecting bools and junk."""
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value) if value.is_integer() else None
    if isinstance(value, str):
        text = value.strip()
        if text.lstrip("-").isdigit():
            try:
                return int(text)
            except ValueError:
                return None
    return None


def _clean_str(value: Any) -> str | None:
    if isinstance(value, str):
        text = value.strip()
        return text or None
    if isinstance(value, int) and not isinstance(value, bool):
        return str(value)
    return None


def _person_from_node(node: dict[str, Any]) -> Person | None:
    """Build a :class:`Person` from a dict that looks like a Facebook user node.

    Returns ``None`` unless the dict carries a ``birthdate`` sub-dict with sane integer
    ``day`` / ``month`` plus an ``id`` and a ``name``.
    """
    birthdate = node.get("birthdate")
    if not isinstance(birthdate, dict):
        return None

    month = _as_int(birthdate.get("month"))
    day = _as_int(birthdate.get("day"))
    if month is None or day is None:
        return None
    if not (1 <= month <= 12) or not (1 <= day <= 31):
        return None

    source_id = _clean_str(node.get("id"))
    name = _clean_str(node.get("name"))
    if not source_id or not name:
        return None

    year = _as_int(birthdate.get("year"))
    if year is not None and not (1900 <= year <= 2200):
        year = None

    profile_url = (
        _clean_str(node.get("profile_url"))
        or _clean_str(node.get("url"))
        or f"https://www.facebook.com/{source_id}"
    )

    return Person(
        source=SOURCE,
        source_id=source_id,
        name=name,
        month=month,
        day=day,
        year=year,
        profile_url=profile_url,
    )


def _is_placeholder_url(person: Person) -> bool:
    return person.profile_url == f"https://www.facebook.com/{person.source_id}"


def _better(new: Person, old: Person) -> bool:
    """Is ``new`` a strictly better record for the same person than ``old``?"""
    if (new.year is not None) != (old.year is not None):
        return new.year is not None
    if _is_placeholder_url(new) != _is_placeholder_url(old):
        return not _is_placeholder_url(new)
    return False


def _dedupe(people: Iterable[Person]) -> list[Person]:
    """Dedupe by ``source_id``, preferring the richest record, preserving first-seen order."""
    by_id: dict[str, Person] = {}
    for person in people:
        existing = by_id.get(person.source_id)
        if existing is None or _better(person, existing):
            by_id[person.source_id] = person
    return list(by_id.values())


def parse_birthday_json(obj: Any) -> list[Person]:
    """Extract every friend birthday from an arbitrarily shaped GraphQL response.

    Facebook has moved the birthday nodes around repeatedly (fb2cal's transformer reads
    ``data.viewer.all_friends_by_birthday_month.edges[].node.friends.edges[].node`` while
    its own captured fixture also carries ``data.{today,recent,upcoming}.all_friends
    .edges[].node``), so this walks the whole document instead of pinning a path: any
    dict holding a ``birthdate`` object with integer ``day``/``month`` becomes a
    :class:`Person`.

    Pure and browser-free.  Results keep document order and are deduped by ``source_id``,
    preferring the record that knows the birth year.
    """
    found: list[Person] = []
    seen_containers: set[int] = set()
    stack: list[Any] = [obj]

    while stack:
        current = stack.pop()
        if isinstance(current, dict):
            marker = id(current)
            if marker in seen_containers:
                continue
            seen_containers.add(marker)
            person = _person_from_node(current)
            if person is not None:
                found.append(person)
            # Reversed so a plain stack still yields document order.
            stack.extend(reversed(list(current.values())))
        elif isinstance(current, (list, tuple)):
            marker = id(current)
            if marker in seen_containers:
                continue
            seen_containers.add(marker)
            stack.extend(reversed(list(current)))

    return _dedupe(found)


def _strip_anti_hijack(text: str, pos: int = 0) -> int:
    """Return the offset just past an optional ``for (;;);`` prefix at ``pos``."""
    match = _ANTI_HIJACK_RE.match(text, pos)
    return match.end() if match else pos


def loads_graphql(text: str) -> list[Any]:
    """Parse a Facebook GraphQL response body into a list of JSON documents.

    Handles the ``for (;;);`` anti-hijacking prefix and the fact that Comet responses are
    frequently several JSON documents concatenated/newline-separated in one body.
    """
    documents: list[Any] = []
    decoder = json.JSONDecoder()
    index = 0
    length = len(text)

    while index < length:
        index = _strip_anti_hijack(text, index)
        while index < length and text[index] in " \t\r\n":
            index += 1
        if index >= length:
            break
        try:
            document, index = decoder.raw_decode(text, index)
        except ValueError:
            break
        documents.append(document)

    return documents


def _is_error_document(document: Any) -> bool:
    if not isinstance(document, dict):
        return False
    if document.get("errors"):
        return True
    return any(key in document for key in ("error", "errorSummary", "errorDescription"))


def _people_from_body(text: str) -> tuple[list[Person], bool]:
    """Parse a raw response body -> (people, saw_error).

    A body that yields *no* JSON documents is an error, not an empty month.
    :func:`loads_graphql` stops at the first thing it cannot decode, so an HTML re-auth
    interstitial or a truncated body comes back as an empty document list -- and if that
    were reported as success, the quarter of the year that request covered would be
    dropped from the calendar with no retry and no warning.
    """
    documents = loads_graphql(text)
    people: list[Person] = []
    saw_error = not documents
    for document in documents:
        if _is_error_document(document):
            saw_error = True
        people.extend(parse_birthday_json(document))
    return people, saw_error


# --------------------------------------------------------------------------------------
# Browser automation
# --------------------------------------------------------------------------------------


def _is_graphql_url(url: str) -> bool:
    return "/api/graphql" in url


def _parse_form_body(body: str | None) -> dict[str, str]:
    if not body:
        return {}
    try:
        pairs = parse_qs(body, keep_blank_values=True)
    except (ValueError, UnicodeDecodeError):
        return {}
    return {key: values[0] for key, values in pairs.items() if values}


def _capture_rank(friendly_name: str, params: dict[str, str]) -> int:
    """Higher is a better template for month-by-month replay."""
    lowered = friendly_name.lower()
    if "monthlybirthdaysrefetch" in lowered:
        return 4
    if "monthly" in lowered and "birthday" in lowered:
        return 3
    if "offset_month" in params.get("variables", ""):
        return 2
    return 1


class _Recorder:
    """Watches GraphQL traffic: learns the birthday query, stashes birthday responses."""

    def __init__(self) -> None:
        self.captures: dict[str, dict[str, Any]] = {}
        self.bodies: list[str] = []
        self._bytes = 0
        self.seen_birthday_request = asyncio.Event()

    # -- request side: learn doc_id / fb_dtsg / the full form template ------------------
    def on_request(self, request: Any) -> None:
        try:
            if request.method != "POST" or not _is_graphql_url(request.url):
                return
            params = _parse_form_body(request.post_data)
        except Exception:  # noqa: BLE001 - never let a listener break the run
            return

        friendly_name = params.get("fb_api_req_friendly_name", "")
        if not friendly_name:
            try:
                friendly_name = request.headers.get("x-fb-friendly-name", "")
            except Exception:  # noqa: BLE001
                friendly_name = ""
        if "birthday" not in friendly_name.lower():
            return
        if not params.get("doc_id") or not params.get("fb_dtsg"):
            return

        self.captures[friendly_name] = {
            "friendly_name": friendly_name,
            "doc_id": params["doc_id"],
            "fb_dtsg": params["fb_dtsg"],
            "params": params,
            "rank": _capture_rank(friendly_name, params),
        }
        if not self.seen_birthday_request.is_set():
            log(f"observed birthday query {friendly_name} (doc_id={params['doc_id']})")
            self.seen_birthday_request.set()

    def best_capture(self) -> dict[str, Any] | None:
        if not self.captures:
            return None
        return max(self.captures.values(), key=lambda capture: capture["rank"])

    # -- response side: fallback harvest ------------------------------------------------
    async def on_response(self, response: Any) -> None:
        try:
            if not _is_graphql_url(response.url):
                return
            text = await response.text()
        except Exception:  # noqa: BLE001 - body may be gone (navigation, redirect, ...)
            return
        if "birthdate" not in text:
            return
        if len(self.bodies) >= _MAX_STASHED_BODIES or self._bytes >= _MAX_STASHED_BYTES:
            return
        self.bodies.append(text)
        self._bytes += len(text)


async def _is_authenticated(context: Any) -> bool:
    """Facebook sets ``c_user`` to the numeric account id once logged in (per fb2cal)."""
    try:
        cookies = await context.cookies("https://www.facebook.com/")
    except Exception:  # noqa: BLE001
        return False
    for cookie in cookies:
        if cookie.get("name") == "c_user" and str(cookie.get("value", "")).strip().isdigit():
            return True
    return False


async def _looks_like_login_page(page: Any) -> bool:
    if _LOGIN_URL_RE.search(page.url or ""):
        return True
    try:
        return await page.locator(
            "form[action*='login'], input[name='pass'], input[id='pass']"
        ).count() > 0
    except Exception:  # noqa: BLE001
        return False


async def _goto(page: Any, url: str, deadline: float | None = None) -> None:
    timeout_ms = _step_timeout(deadline, _NAVIGATION_TIMEOUT_MS / 1000.0) * 1000.0
    try:
        await page.goto(url, wait_until="domcontentloaded", timeout=timeout_ms)
    except Exception as exc:  # noqa: BLE001 - a slow/partial load is still usable
        log(f"navigation to {url} did not settle ({type(exc).__name__}); continuing")


async def _wait_for_login(context: Any, page: Any, timeout: float) -> None:
    """Block until the user has logged in by hand in the visible window.

    The page is deliberately left alone while we wait.  Facebook sets ``c_user`` as soon
    as the password is accepted, i.e. *before* 2FA / a checkpoint is finished, so
    "authenticated" is not the same as "done": navigating on the strength of the cookie
    would wipe a half-typed 2FA code, Facebook would redirect straight back to the
    checkpoint, and the user could never finish logging in.  The caller navigates to the
    birthdays page once this returns.
    """
    if await _is_authenticated(context) and not await _looks_like_login_page(page):
        return

    log("=" * 68)
    log("Log in to Facebook in the window that just opened, then come back here.")
    log("Take your time -- 2FA, captchas and security checkpoints are all fine.")
    log(f"Waiting up to {int(timeout)}s for you to finish...")
    log("=" * 68)

    deadline = time.monotonic() + timeout
    announced = False
    while time.monotonic() < deadline:
        await asyncio.sleep(min(_AUTH_POLL_SECONDS, max(0.0, deadline - time.monotonic())))
        if page.is_closed():
            raise RuntimeError(
                "The Facebook window was closed before login completed. "
                "Re-run `birthdays connect facebook` and leave the window open."
            )
        if not await _is_authenticated(context):
            continue
        if await _looks_like_login_page(page):
            # Signed in but still on a checkpoint / 2FA / recovery screen. Wait it out.
            if not announced:
                log("password accepted; finish the security check in the window...")
                announced = True
            continue
        log("authenticated.")
        return

    raise RuntimeError(
        f"Timed out after {int(timeout)}s waiting for a manual Facebook login. "
        "Run `birthdays connect facebook` and complete the login in the browser window."
    )


async def _scroll(page: Any, rounds: int = 6, deadline: float | None = None) -> None:
    for _ in range(rounds):
        if _remaining(deadline) <= 0:
            return
        try:
            await page.mouse.wheel(0, 3000)
            await page.keyboard.press("End")
        except Exception:  # noqa: BLE001
            return
        await asyncio.sleep(min(1.0, _remaining(deadline)))


async def _provoke_more_months(page: Any, deadline: float | None = None) -> None:
    """Best-effort: click through the birthday page's month navigation.

    Only used on the fallback harvest path, where we need the page itself to request
    additional months for us.  Every selector here is speculative, so failures are silent
    -- and every one of them is skipped once the caller's budget is gone.
    """
    selectors = (
        "div[role='button'][aria-label*='Next' i]",
        "a[role='link'][aria-label*='Next' i]",
        "div[role='button'][aria-label*='month' i]",
        "div[role='button'][aria-label*='See more' i]",
        "div[role='button']:has-text('See more birthdays')",
    )
    for selector in selectors:
        if _remaining(deadline) <= 0:
            break
        try:
            locator = page.locator(selector).first
            if await locator.count() == 0:
                continue
            for _ in range(4):
                if _remaining(deadline) <= 0:
                    break
                await locator.click(timeout=_step_timeout(deadline, 3.0) * 1000.0)
                await asyncio.sleep(min(1.5, _remaining(deadline)))
        except Exception:  # noqa: BLE001
            continue
    await _scroll(page, rounds=8, deadline=deadline)


_REPLAY_JS = """
async ({ url, body, headers }) => {
  const response = await fetch(url, {
    method: 'POST',
    body,
    headers,
    credentials: 'include',
    mode: 'cors',
  });
  return await response.text();
}
"""


def _build_replay_body(capture: dict[str, Any], offset_month: int, doc_id: str) -> str:
    """Rebuild the captured form body with a new ``offset_month`` and doc_id."""
    params = dict(capture.get("params") or {})

    variables: dict[str, Any]
    try:
        parsed = json.loads(params.get("variables", "") or "{}")
        variables = parsed if isinstance(parsed, dict) else {}
    except (TypeError, ValueError):
        variables = {}
    variables.setdefault("scale", 1.5)
    variables["offset_month"] = offset_month

    params.update(
        {
            "fb_api_req_friendly_name": capture.get("friendly_name") or FRIENDLY_NAME,
            "variables": json.dumps(variables),
            "doc_id": str(doc_id),
            "fb_dtsg": capture["fb_dtsg"],
            "__a": "1",
        }
    )
    return urlencode(params)


def _replay_headers(capture: dict[str, Any]) -> dict[str, str]:
    params = capture.get("params") or {}
    headers = {
        "content-type": "application/x-www-form-urlencoded",
        "x-fb-friendly-name": capture.get("friendly_name") or FRIENDLY_NAME,
    }
    if params.get("lsd"):
        headers["x-fb-lsd"] = params["lsd"]
    return headers


async def _replay(
    page: Any, capture: dict[str, Any], doc_id: str, deadline: float | None = None
) -> tuple[list[Person], list[str], bool]:
    """Replay the birthday query for a year's worth of months, in page context.

    Running inside the page means cookies, headers and origin are all native, so Facebook
    treats it as the page's own traffic.

    Each request is bounded: ``page.evaluate`` awaits the in-page ``fetch()`` promise and
    takes no timeout of its own, so a hung request would otherwise block for ever,
    whatever the user passed to ``--timeout``.

    Returns ``(people, raw_bodies, saw_error)``.
    """
    people: list[Person] = []
    bodies: list[str] = []
    saw_error = False
    headers = _replay_headers(capture)

    for offset_month in OFFSET_MONTHS:
        if _remaining(deadline) <= 0:
            # Out of budget: say so loudly, and flag it so the caller knows this run is
            # missing months rather than genuinely finding nothing there.
            log(f"offset_month={offset_month}: skipped, out of time budget")
            saw_error = True
            continue
        body = _build_replay_body(capture, offset_month, doc_id)
        try:
            text = await asyncio.wait_for(
                page.evaluate(
                    _REPLAY_JS, {"url": GRAPHQL_URL, "body": body, "headers": headers}
                ),
                timeout=_step_timeout(deadline, _REPLAY_TIMEOUT),
            )
        except Exception as exc:  # noqa: BLE001
            log(f"offset_month={offset_month}: replay failed ({type(exc).__name__})")
            saw_error = True
            continue

        if not isinstance(text, str) or not text.strip():
            log(f"offset_month={offset_month}: empty response")
            saw_error = True
            continue

        bodies.append(text)
        found, body_error = _people_from_body(text)
        saw_error = saw_error or body_error
        log(f"offset_month={offset_month}: {len(found)} birthdays")
        people.extend(found)

    return people, bodies, saw_error


def _save_bodies(store: "Store", bodies: Iterable[str], start: int = 0) -> int:
    index = start
    for text in bodies:
        index += 1
        try:
            store.save_raw(SOURCE, f"graphql-{index}.json", text.encode("utf-8", "replace"))
        except Exception as exc:  # noqa: BLE001 - debug dumps must never break a sync
            log(f"could not write raw dump {index} ({type(exc).__name__})")
    return index


async def _launch(playwright: Any, user_data_dir: str, headless: bool) -> Any:
    """Persistent context so the hand-done login survives across runs."""
    kwargs: dict[str, Any] = {
        "user_data_dir": user_data_dir,
        "headless": headless,
        "viewport": {"width": 1280, "height": 900},
        "locale": "en-US",
        "args": ["--disable-blink-features=AutomationControlled"],
    }
    try:
        context = await playwright.chromium.launch_persistent_context(channel="chrome", **kwargs)
        log("using installed Google Chrome")
        return context
    except Exception:  # noqa: BLE001 - Chrome not installed, fall back to bundled build
        log("Google Chrome not available, using bundled Chromium")
        return await playwright.chromium.launch_persistent_context(**kwargs)


async def fetch(store: "Store", headless: bool = False, timeout: float = 300.0) -> list[Person]:
    """Fetch Facebook friend birthdays, prompting for a hand-done login if needed.

    Strategy is "learn then replay": Facebook rotates the GraphQL ``doc_id``, so rather
    than pinning fb2cal's, we watch the real birthdays page issue its own query, steal the
    ``doc_id`` + ``fb_dtsg`` + form template, and replay it for offset months 0/3/6/9.  If
    that fails we fall back to fb2cal's pinned doc_id once, and finally to harvesting the
    response bodies the page produced on its own.

    ``timeout`` is the budget for the *whole* call, as the CLI advertises it ("how long
    to wait for the login plus the fetch"), not just for the login: every wait below is
    clamped against a single deadline.
    """
    from playwright.async_api import async_playwright  # imported lazily: keeps this
    # module (and parse_birthday_json) importable with no browser stack installed.

    profile_dir = store.profile_dir(SOURCE)
    recorder = _Recorder()
    people: list[Person] = []
    raw_count = 0
    deadline = time.monotonic() + max(0.0, timeout)
    waited_for_login = False

    async with async_playwright() as playwright:
        context = await _launch(playwright, str(profile_dir), headless)
        try:
            page = context.pages[0] if context.pages else await context.new_page()

            def watch(target: Any) -> None:
                # Effectively a listener on GRAPHQL_GLOB; the handlers filter by URL.
                target.on("request", recorder.on_request)
                target.on("response", recorder.on_response)

            watch(page)
            context.on("page", watch)

            log(f"opening {BIRTHDAYS_URL}")
            await _goto(page, BIRTHDAYS_URL, deadline)

            if not await _is_authenticated(context) or await _looks_like_login_page(page):
                if headless:
                    raise RuntimeError(
                        "Not logged in to Facebook and running headless. "
                        "Run `birthdays connect facebook` first to log in by hand."
                    )
                waited_for_login = True
                await _wait_for_login(context, page, _remaining(deadline))

            if waited_for_login and _remaining(deadline) < _MIN_HARVEST_SECONDS:
                raise RuntimeError(
                    f"The manual login used the whole {int(timeout)}s --timeout budget, so "
                    "there was no time left to fetch anything.\n"
                    "The session is saved now, so just run `birthdays sync` again (or pass "
                    "a larger --timeout)."
                )

            if BIRTHDAYS_URL.rstrip("/") not in (page.url or "").rstrip("/"):
                await _goto(page, BIRTHDAYS_URL, deadline)

            # Give the page a chance to fire its own birthday query, nudging it along.
            log("waiting for the page's own birthday query...")
            scroller = asyncio.ensure_future(_scroll(page, rounds=6, deadline=deadline))
            try:
                await asyncio.wait_for(
                    recorder.seen_birthday_request.wait(),
                    timeout=_step_timeout(deadline, _QUERY_OBSERVE_TIMEOUT),
                )
            except asyncio.TimeoutError:
                log("no birthday query observed; will fall back to harvesting responses")
            finally:
                scroller.cancel()
                with contextlib.suppress(BaseException):
                    await scroller

            capture = recorder.best_capture()
            if capture is not None:
                log(f"replaying {capture['friendly_name']} for offset months {list(OFFSET_MONTHS)}")
                found, bodies, saw_error = await _replay(
                    page, capture, capture["doc_id"], deadline
                )
                raw_count = _save_bodies(store, bodies, raw_count)
                people.extend(found)

                if (not found or saw_error) and _remaining(deadline) > 0:
                    log(f"replay unhappy; retrying once with fb2cal's pinned doc_id {PINNED_DOC_ID}")
                    found, bodies, _ = await _replay(page, capture, PINNED_DOC_ID, deadline)
                    raw_count = _save_bodies(store, bodies, raw_count)
                    people.extend(found)
            else:
                log("no doc_id/fb_dtsg captured")

            if not people:
                log("falling back to harvesting the page's own responses")
                await _provoke_more_months(page, deadline)
                await asyncio.sleep(min(2.0, _remaining(deadline)))
                bodies = list(recorder.bodies)
                raw_count = _save_bodies(store, bodies, raw_count)
                for text in bodies:
                    found, _ = _people_from_body(text)
                    people.extend(found)
                log(f"harvested {len(bodies)} response bodies")
        finally:
            try:
                await context.close()
            except Exception:  # noqa: BLE001
                pass

    result = _dedupe(people)
    result.sort(key=lambda person: (person.month, person.day, person.name.casefold()))

    if not result:
        raise RuntimeError(
            "No Facebook birthdays found. Facebook may have changed its birthday page, "
            "your session may have expired, or the --timeout may have been too short.\n"
            "  1. Run `birthdays connect facebook` and confirm you can load "
            "https://www.facebook.com/events/birthdays/ in the window.\n"
            f"  2. Check the raw GraphQL dumps in {store.raw_dir} -- if they are empty or "
            "contain an error, the query shape has changed; if they contain 'birthdate' "
            "entries, the parser needs updating."
        )

    log(f"{len(result)} birthdays")
    return result
