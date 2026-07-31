#!/usr/bin/env python3
"""A small local web UI for snap-birthdays.

Run it:

    snap-birthdays

It serves a page on 127.0.0.1, opens your browser, and gets out of the way. All the
real work still happens in core.py - this is only a front door.

Why a web page rather than a desktop window: the interesting part of the UI is a list of
several hundred people with checkboxes and a search box, which HTML does well and Tk does
badly, and the Google Calendar hand-off is a browser flow anyway. It also keeps the
dependency list at exactly one (playwright), since the server is stdlib.
"""

from __future__ import annotations

import asyncio
import json
import secrets
import subprocess
import sys
import threading
import webbrowser
from datetime import date
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from . import core

HERE = Path(__file__).resolve().parent
CACHE = core.HOME / "friends.json"

#: Guards against a web page you happen to be visiting POSTing to our localhost port.
#: Every request must present it; it only appears in the URL we open ourselves, and the
#: Host check in Handler._local keeps another origin from reading it back off the page.
TOKEN = secrets.token_urlsafe(16)

#: One writer at a time, so two requests can't interleave a cache rewrite.
LOCK = threading.Lock()

#: Shown to anyone who reaches the port without the token - usually the user typing the
#: bare address after losing the tab, so say what to do instead of "bad token".
NO_TOKEN = (
    "snap-birthdays is running, but this address is missing its access token.\n"
    "Open the http://127.0.0.1:.../?token=... link printed in the terminal you\n"
    "started it from.\n"
)

MONTHS = ["", "January", "February", "March", "April", "May", "June",
          "July", "August", "September", "October", "November", "December"]


# --------------------------------------------------------------------------------------
# State
# --------------------------------------------------------------------------------------


def empty_cache() -> dict:
    return {"friends": [], "deselected": [], "fetched_at": None}


def write_atomic(path: Path, text: str) -> None:
    """Atomic, owner-only write (see ``core.write_private``), one writer at a time.

    The lock matters here and not in the CLI: two requests can be in flight at once, and
    they would otherwise share the one temp file.
    """
    with LOCK:
        core.write_private(path, text)


def load_cache() -> dict:
    """Previously fetched friends, so reopening the UI does not relaunch a browser.

    A missing file means "not connected yet". Anything else - unreadable, damaged - is
    raised, because treating it as empty would let the next save overwrite real data.
    """
    try:
        text = CACHE.read_text(encoding="utf-8")
    except FileNotFoundError:
        return empty_cache()
    try:
        return json.loads(text)
    except ValueError:
        raise ValueError(f"{CACHE} is damaged. Delete it, then connect again.") from None


def save_cache(data: dict) -> None:
    write_atomic(CACHE, json.dumps(data, indent=2))


def cache_payload() -> dict:
    """The shape the page wants: birthdays only, sorted, with selection state."""
    data = load_cache()
    deselected = set(data.get("deselected", []))
    friends = [
        {
            "username": f["username"],
            "name": f["name"],
            "month": f["month"],
            "day": f["day"],
            "monthName": MONTHS[f["month"]],
            "selected": f["username"] not in deselected,
        }
        for f in data.get("friends", [])
        if f.get("month") and f.get("day")
    ]
    friends.sort(key=lambda f: (f["month"], f["day"], f["name"].lower()))
    return {
        "friends": friends,
        "fetchedAt": data.get("fetched_at"),
        "totalFriends": len(data.get("friends", [])),
        "isMac": sys.platform == "darwin",
    }


# --------------------------------------------------------------------------------------
# Actions
# --------------------------------------------------------------------------------------


def connect() -> dict:
    """Launch the browser, capture friend data, merge into the cache."""
    friends = asyncio.run(core.fetch_friends(timeout=300.0))

    try:
        previous = load_cache()
    except (OSError, ValueError):
        previous = empty_cache()  # we are about to replace it anyway, so a bad cache heals
    # Keep prior deselections, but only for people still in the friend list.
    current = {f["username"] for f in friends}
    deselected = [u for u in previous.get("deselected", []) if u in current]

    save_cache({
        "friends": friends,
        "deselected": deselected,
        "fetched_at": date.today().isoformat(),
    })
    return cache_payload()


def remember_selection(selected: list[str]) -> None:
    """Persist the inverse - so friends added to Snapchat later start out selected."""
    data = load_cache()
    chosen = set(selected)
    known = set(data.get("deselected", []))

    deselected = []
    for friend in data.get("friends", []):
        user = friend["username"]
        if friend.get("month") and friend.get("day"):
            if user not in chosen:
                deselected.append(user)
        elif user in known:
            # Friends without a birthday are not on the page, so the browser cannot
            # speak for them. Keep what the user last said in case one reappears.
            deselected.append(user)

    data["deselected"] = deselected
    save_cache(data)


def write_ics(selected: list[str], calname: str) -> Path:
    data = load_cache()
    chosen = set(selected)
    friends = [f for f in data.get("friends", [])
               if f.get("month") and f.get("day") and f["username"] in chosen]
    if not friends:
        raise ValueError("Nothing selected.")
    write_atomic(core.DEFAULT_ICS, core.build_ics(friends, calname))
    return core.DEFAULT_ICS


# --------------------------------------------------------------------------------------
# Server
# --------------------------------------------------------------------------------------


class Handler(BaseHTTPRequestHandler):
    server_version = "snap-birthdays"

    def log_message(self, *args) -> None:
        pass  # the page is the UI; a request log would just be noise

    # -- helpers ----------------------------------------------------------------------

    def _authorised(self, query: dict) -> bool:
        # Compare as bytes: compare_digest refuses non-ASCII str, and the token comes
        # straight off a URL the user could have mangled.
        token = query.get("token", [""])[0].encode("utf-8")
        return secrets.compare_digest(token, TOKEN.encode("utf-8"))

    def _local(self) -> bool:
        """Only serve requests actually addressed to loopback.

        Stops a page whose hostname has been re-pointed at 127.0.0.1 from becoming
        same-origin with us and reading the token out of the page.
        """
        host = (self.headers.get("Host") or "").rsplit(":", 1)[0].strip("[]")
        return host in ("", "127.0.0.1", "localhost", "::1")

    def _send(self, code: int, body: bytes, ctype: str, extra: dict | None = None) -> None:
        self.sent = True
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        for key, value in (extra or {}).items():
            self.send_header(key, value)
        self.end_headers()
        self.wfile.write(body)

    def _json(self, payload: dict, code: int = 200) -> None:
        self._send(code, json.dumps(payload).encode("utf-8"), "application/json")

    # -- routes -----------------------------------------------------------------------

    def do_GET(self) -> None:
        self._handle(self._get)

    def do_POST(self) -> None:
        self._handle(self._post)

    def _handle(self, route) -> None:
        """Every request answers with JSON, whatever goes wrong.

        BaseException rather than Exception on purpose: core.fetch_friends signals its
        one expected failure - the login timeout - with SystemExit, and letting that
        escape kills the thread and drops the connection, so the page shows "failed to
        fetch" instead of the sentence telling the user what to do.
        """
        self.sent = False
        try:
            if not self._local():
                self._json({"error": "bad host"}, 403)
                return
            route()
        except BaseException as exc:
            if not self.sent:
                self._json({"error": str(exc) or exc.__class__.__name__}, 500)

    def _get(self) -> None:
        url = urlparse(self.path)
        query = parse_qs(url.query)

        # The page carries the token, so it has to be behind the token too - otherwise
        # any other local process can read it off the page and then read your friends.
        if not self._authorised(query):
            if url.path == "/":
                self._send(403, NO_TOKEN.encode("utf-8"), "text/plain; charset=utf-8")
            else:
                self._json({"error": "bad token"}, 403)
            return

        if url.path == "/":
            html = (HERE / "ui.html").read_text(encoding="utf-8")
            html = html.replace("__TOKEN__", TOKEN)
            self._send(200, html.encode("utf-8"), "text/html; charset=utf-8")
        elif url.path == "/api/state":
            self._json(cache_payload())
        elif url.path == "/download":
            try:
                body = core.DEFAULT_ICS.read_bytes()
            except OSError:
                self._json({"error": "nothing to download"}, 404)
                return
            self._send(200, body, "text/calendar; charset=utf-8",
                       {"Content-Disposition": 'attachment; filename="snapchat-birthdays.ics"'})
        else:
            self._json({"error": "not found"}, 404)

    def _post(self) -> None:
        url = urlparse(self.path)
        if not self._authorised(parse_qs(url.query)):
            self._json({"error": "bad token"}, 403)
            return

        try:
            length = int(self.headers.get("Content-Length") or 0)
            body = json.loads(self.rfile.read(length) or b"{}")
        except ValueError:
            self._json({"error": "bad request"}, 400)
            return

        if url.path == "/api/connect":
            self._json(connect())

        elif url.path == "/api/import":
            selected = body.get("selected") or []
            target = body.get("target", "apple")
            if target == "apple" and sys.platform != "darwin":
                self._json({"error": "Apple Calendar is macOS only."}, 400)
                return
            # Build first: an empty selection raises here, before anything is persisted,
            # so a stray import can never wipe the saved selection.
            path = write_ics(selected, body.get("calname") or "Snapchat Birthdays")
            remember_selection(selected)

            if target == "apple":
                subprocess.run(["open", "-a", "Calendar", str(path)], check=True)
                self._json({"ok": True, "count": len(selected), "path": str(path)})
            else:
                # Google can't be automated without OAuth, so hand the user a real
                # download plus the import page, which is two clicks.
                webbrowser.open(core.GOOGLE_IMPORT_URL)
                self._json({"ok": True, "count": len(selected), "path": str(path),
                            "download": f"/download?token={TOKEN}"})
        else:
            self._json({"error": "not found"}, 404)


def main() -> int:
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    port = server.server_address[1]
    url = f"http://127.0.0.1:{port}/?token={TOKEN}"

    print("snap-birthdays\n")
    print(f"  {url}\n")
    print("  Opening your browser. Press Ctrl-C here when you're done.")
    threading.Timer(0.3, lambda: webbrowser.open(url)).start()

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nBye.")
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
