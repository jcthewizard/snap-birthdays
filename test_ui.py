"""Tests for the local web UI.

Run with: python3 -m pytest test_ui.py

No browser: the handler is served on a random loopback port and driven with http.client,
which is the whole API surface the page uses.
"""

from __future__ import annotations

import http.client
import json
import threading
from http.server import ThreadingHTTPServer

import pytest

import snapbirthdays as core
import ui

FRIENDS = [
    {"username": "alice", "name": "Alice", "month": 3, "day": 4},
    {"username": "bob", "name": "Bob", "month": 7, "day": 1},
    {"username": "carol", "name": "Carol", "month": 12, "day": 25},
    {"username": "dave", "name": "Dave", "month": None, "day": None},  # keeps it private
]


@pytest.fixture
def home(tmp_path, monkeypatch):
    """Point the cache and the .ics at a temp dir so nothing real is touched."""
    monkeypatch.setattr(ui, "CACHE", tmp_path / "friends.json")
    monkeypatch.setattr(core, "DEFAULT_ICS", tmp_path / "birthdays.ics")
    monkeypatch.setattr(ui.webbrowser, "open", lambda *a, **k: True)
    ui.save_cache({"friends": FRIENDS, "deselected": [], "fetched_at": "2026-07-30"})
    return tmp_path


@pytest.fixture
def server(home):
    srv = ThreadingHTTPServer(("127.0.0.1", 0), ui.Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield srv
    srv.shutdown()
    srv.server_close()


def call(server, method, path, body=None, headers=None):
    conn = http.client.HTTPConnection("127.0.0.1", server.server_address[1], timeout=10)
    try:
        conn.request(method, path, body, headers or {})
        res = conn.getresponse()
        return res.status, res.read().decode("utf-8")
    finally:
        conn.close()


def api(server, method, path, payload=None):
    body = json.dumps(payload or {})
    status, text = call(server, method, f"{path}?token={ui.TOKEN}", body,
                        {"Content-Type": "application/json"})
    return status, json.loads(text)


# -- auth --------------------------------------------------------------------------


def test_state_lists_only_birthdays(server):
    status, data = api(server, "GET", "/api/state")
    assert status == 200
    assert [f["username"] for f in data["friends"]] == ["alice", "bob", "carol"]
    assert data["totalFriends"] == 4


@pytest.mark.parametrize("query", ["", "?token=wrong", "?token=%C3%A9", "?token=%00"])
def test_bad_token_is_a_403_not_a_dropped_connection(server, query):
    """A non-ASCII token used to raise TypeError and kill the connection."""
    status, text = call(server, "GET", "/api/state" + query)
    assert status == 403
    assert json.loads(text) == {"error": "bad token"}

    status, text = call(server, "POST", "/api/import" + query, "{}")
    assert status == 403
    assert json.loads(text) == {"error": "bad token"}


def test_page_is_only_served_to_loopback(server):
    port = server.server_address[1]

    status, text = call(server, "GET", "/", headers={"Host": f"127.0.0.1:{port}"})
    assert status == 200 and ui.TOKEN in text

    # A hostname re-pointed at 127.0.0.1 must not be able to read the token back out.
    status, text = call(server, "GET", "/", headers={"Host": f"evil.example.com:{port}"})
    assert status == 403
    assert ui.TOKEN not in text

    status, text = call(server, "GET", f"/api/state?token={ui.TOKEN}",
                        headers={"Host": "evil.example.com"})
    assert status == 403


def test_unparsable_content_length_is_a_400(server):
    status, text = call(server, "POST", f"/api/import?token={ui.TOKEN}", "",
                        {"Content-Length": "abc"})
    assert status == 400
    assert json.loads(text) == {"error": "bad request"}


def test_bad_json_body_is_a_400(server):
    status, text = call(server, "POST", f"/api/import?token={ui.TOKEN}", "notjson")
    assert status == 400
    assert json.loads(text) == {"error": "bad request"}


# -- connect -----------------------------------------------------------------------


def test_connect_timeout_reaches_the_page(server, monkeypatch):
    """fetch_friends signals its timeout with SystemExit, which is not an Exception."""
    message = "No friend data seen in 300s.\nLog in, open the Chat tab, then retry."

    async def timeout(*args, **kwargs):
        raise SystemExit(message)

    monkeypatch.setattr(core, "fetch_friends", timeout)
    status, data = api(server, "POST", "/api/connect")
    assert status == 500
    assert data["error"] == message


def test_connect_reports_ordinary_failures_too(server, monkeypatch):
    async def boom(*args, **kwargs):
        raise RuntimeError("playwright is not installed")

    monkeypatch.setattr(core, "fetch_friends", boom)
    status, data = api(server, "POST", "/api/connect")
    assert status == 500
    assert data["error"] == "playwright is not installed"


def test_connect_heals_a_damaged_cache(server, home, monkeypatch):
    ui.CACHE.write_text("{ truncated", encoding="utf-8")

    async def fetched(*args, **kwargs):
        return FRIENDS

    monkeypatch.setattr(core, "fetch_friends", fetched)
    status, data = api(server, "POST", "/api/connect")
    assert status == 200
    assert len(data["friends"]) == 3


# -- the cache ---------------------------------------------------------------------


def test_save_cache_is_atomic(home):
    """Readers must never see the empty window a truncate-then-write leaves behind."""
    stop = threading.Event()
    seen = []

    def read():
        while not stop.is_set():
            seen.append(len(ui.cache_payload()["friends"]))

    reader = threading.Thread(target=read)
    reader.start()
    try:
        for _ in range(200):
            ui.save_cache({"friends": FRIENDS, "deselected": [], "fetched_at": "x"})
    finally:
        stop.set()
        reader.join()

    assert seen and set(seen) == {3}
    assert list(home.glob("*.tmp")) == []


def test_damaged_cache_is_reported_not_overwritten(server, home):
    ui.CACHE.write_text('{"friends": [{"user', encoding="utf-8")

    status, data = api(server, "GET", "/api/state")
    assert status == 500
    assert "damaged" in data["error"]

    status, _ = api(server, "POST", "/api/import", {"selected": ["alice"]})
    assert status == 500
    assert ui.CACHE.read_text(encoding="utf-8") == '{"friends": [{"user'


def test_deselection_survives_a_hidden_birthday(home):
    """Dave has no birthday, so the page can't speak for him - remember him anyway."""
    ui.remember_selection(["alice", "bob", "carol"])
    ui.save_cache({**ui.load_cache(), "deselected": ["carol", "dave"]})

    # The page only ever posts the people it can see.
    ui.remember_selection(["alice", "bob"])
    assert set(ui.load_cache()["deselected"]) == {"carol", "dave"}

    # Dave later shares his birthday: he is still unticked.
    friends = [dict(f, month=2, day=2) if f["username"] == "dave" else f for f in FRIENDS]
    ui.save_cache({**ui.load_cache(), "friends": friends})
    picked = {f["username"]: f["selected"] for f in ui.cache_payload()["friends"]}
    assert picked == {"alice": True, "bob": True, "carol": False, "dave": False}


# -- import ------------------------------------------------------------------------


def test_import_writes_the_ics_and_remembers_the_selection(server):
    status, data = api(server, "POST", "/api/import",
                       {"selected": ["alice", "carol"], "target": "google"})
    assert status == 200 and data["count"] == 2
    ics = core.DEFAULT_ICS.read_text(encoding="utf-8")
    assert ics.count("BEGIN:VEVENT") == 2
    assert ui.load_cache()["deselected"] == ["bob"]


def test_reimporting_reuses_the_same_uids(server):
    api(server, "POST", "/api/import", {"selected": ["alice", "carol"], "target": "google"})
    first = set(core.DEFAULT_ICS.read_text(encoding="utf-8").splitlines())

    api(server, "POST", "/api/import",
        {"selected": ["alice", "bob", "carol"], "target": "google"})
    second = set(core.DEFAULT_ICS.read_text(encoding="utf-8").splitlines())

    uids = {line for line in first if line.startswith("UID:")}
    assert len(uids) == 2 and uids <= second


def test_empty_import_cannot_wipe_the_selection(server):
    api(server, "POST", "/api/import", {"selected": ["alice"], "target": "google"})
    before = ui.load_cache()["deselected"]

    status, data = api(server, "POST", "/api/import", {"selected": [], "target": "google"})
    assert status == 500
    assert data["error"] == "Nothing selected."
    assert ui.load_cache()["deselected"] == before


def test_unknown_route(server):
    status, data = api(server, "POST", "/api/nope")
    assert status == 404
    status, data = api(server, "GET", "/api/nope")
    assert status == 404
