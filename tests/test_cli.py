"""Regression tests for the command line surface.

These run the real Click commands through ``CliRunner`` with the browser replaced by a
stub fetcher, so they exercise the actual wiring between fetch, cache, merge, ICS and
the messages the user reads.  ``--no-import`` everywhere: opening the calendar app is
not this suite's business.
"""

from __future__ import annotations

import os
import re
from pathlib import Path

import pytest
from click.testing import CliRunner

from birthdays import cli as climod
from birthdays.models import Person
from birthdays.store import Store


@pytest.fixture()
def runner() -> CliRunner:
    return CliRunner()


def fb_person(index: int, month: int = 3) -> Person:
    return Person(
        source="facebook",
        source_id=f"fb-{index}",
        name=f"Friend {index}",
        month=month,
        day=(index % 28) + 1,
    )


def snap_person(username: str, month: int, day: int, name: str = "") -> Person:
    return Person(
        source="snapchat",
        source_id=username,
        name=name,
        month=month,
        day=day,
        username=username,
    )


def fake_fetcher(people: list[Person] | None = None, error: str | None = None):
    """A stand-in for ``sources.get_fetcher(...)``'s return value."""

    async def _fetch(store, headless: bool = False, timeout: float = 300.0):
        if error:
            raise RuntimeError(error)
        return list(people or [])

    return lambda source: _fetch


def run(runner: CliRunner, home: Path, *args: str):
    return runner.invoke(climod.cli, ["--home", str(home), *args], obj={})


def uids_in(path: Path) -> list[str]:
    return re.findall(r"^UID:(.+)$", path.read_text(encoding="utf-8"), flags=re.M)


# --------------------------------------------------------------------------------------
# --out is written where the user (and the importer) expects (cli.py:195)
# --------------------------------------------------------------------------------------


def test_out_expands_a_tilde_instead_of_creating_a_literal_directory(
    runner, tmp_path, monkeypatch
):
    """A quoted ``--out '~/cal.ics'`` reaches us with the tilde intact.

    Written verbatim it lands in a junk ``./~/`` directory while ``import_ics`` expands
    the tilde and looks somewhere else entirely -- so the run "succeeds", nothing reaches
    the calendar, and the two printed paths disagree with each other and with reality.
    """
    home = tmp_path / "state"
    fake_home = tmp_path / "userhome"
    fake_home.mkdir()
    monkeypatch.setenv("HOME", str(fake_home))
    Store(home).save_people("facebook", [fb_person(1)])

    workdir = tmp_path / "cwd"
    workdir.mkdir()
    monkeypatch.chdir(workdir)

    result = run(runner, home, "export", "--no-import", "--out", "~/mycal.ics")

    assert result.exit_code == 0, result.output
    assert (fake_home / "mycal.ics").exists(), "the ICS did not land in the home directory"
    assert not (workdir / "~").exists(), "a literal '~' directory was created"
    assert str(fake_home / "mycal.ics") in result.output, "the printed path must be real"


# --------------------------------------------------------------------------------------
# A truncated fetch must not destroy the cache (cli.py:105)
# --------------------------------------------------------------------------------------


def test_a_partial_fetch_does_not_delete_cached_birthdays(runner, tmp_path, monkeypatch):
    """Facebook fetches the year in quarters and tolerates a quarter failing.

    The short list it returns is a real, non-raising success, so the cache used to be
    replaced with it -- deleting the other nine months from every later export until some
    future fetch happened to come back complete.
    """
    home = tmp_path / "state"
    store = Store(home)
    everyone = [fb_person(i) for i in range(40)]
    store.save_people("facebook", everyone)

    only_one_quarter = everyone[:10]
    monkeypatch.setattr(climod, "get_fetcher", fake_fetcher(only_one_quarter))

    result = run(runner, home, "sync", "--source", "facebook", "--headless", "--no-import")

    assert result.exit_code == 0, result.output
    cached = Store(home).load_people("facebook")
    assert len(cached) == len(everyone), "the missing quarters were wiped from the cache"
    assert len(uids_in(store.ics_path())) == len(everyone)


def test_a_fetch_that_updates_a_record_keeps_the_fresh_version(runner, tmp_path, monkeypatch):
    home = tmp_path / "state"
    store = Store(home)
    store.save_people("facebook", [fb_person(1), fb_person(2)])

    renamed = Person(
        source="facebook", source_id="fb-1", name="Renamed Friend", month=3, day=2
    )
    monkeypatch.setattr(climod, "get_fetcher", fake_fetcher([renamed]))

    run(runner, home, "sync", "--source", "facebook", "--headless", "--no-import")

    cached = {p.source_id: p.name for p in Store(home).load_people("facebook")}
    assert cached == {"fb-1": "Renamed Friend", "fb-2": "Friend 2"}


# --------------------------------------------------------------------------------------
# `connect` must not claim a fallback it never used (cli.py:134)
# --------------------------------------------------------------------------------------


def test_a_failed_connect_does_not_claim_to_fall_back_to_the_cache(
    runner, tmp_path, monkeypatch
):
    home = tmp_path / "state"
    Store(home).save_people("facebook", [fb_person(i) for i in range(3)])
    monkeypatch.setattr(climod, "get_fetcher", fake_fetcher(error="facebook session expired"))

    result = run(runner, home, "connect", "facebook", "--headless")

    assert result.exit_code != 0
    assert "every source failed; nothing was connected." in result.output
    assert "falling back" not in result.output, "connect builds no calendar; nothing fell back"
    assert len(Store(home).load_people("facebook")) == 3, "the cache must survive untouched"


def test_sync_still_falls_back_to_the_cache_when_a_source_fails(runner, tmp_path, monkeypatch):
    """The fallback is right for `sync` -- one broken site must not empty the calendar."""
    home = tmp_path / "state"
    store = Store(home)
    store.save_people("facebook", [fb_person(i) for i in range(3)])
    monkeypatch.setattr(climod, "get_fetcher", fake_fetcher(error="facebook is down"))

    result = run(runner, home, "sync", "--source", "facebook", "--headless", "--no-import")

    assert result.exit_code == 0, result.output
    assert "falling back to 3 cached facebook record(s)" in result.output
    assert len(uids_in(store.ics_path())) == 3


# --------------------------------------------------------------------------------------
# End to end: connecting a second source updates events, it does not duplicate them
# --------------------------------------------------------------------------------------


def test_connecting_facebook_later_does_not_duplicate_snapchat_events(
    runner, tmp_path, monkeypatch
):
    """The headline promise: sync Snapchat, then connect Facebook -> same UIDs.

    Both stories in one: "janedoe22" gains the name "Jane Doe" (they merge, so the group
    is renamed) and "jane_doe" collides with "Jane Marie Doe" without merging (so the
    newcomer needs salting).  Either one used to move an already-exported UID.
    """
    home = tmp_path / "state"
    snapchat = [
        snap_person("janedoe22", 5, 6),
        snap_person("jane_doe", 2, 2),
        snap_person("bestie", 9, 9, name="Best Friend"),
    ]
    facebook = [
        Person(source="facebook", source_id="1", name="Jane Doe", month=5, day=6),
        Person(source="facebook", source_id="2", name="Jane Marie Doe", month=2, day=2),
        Person(source="facebook", source_id="3", name="Best Friend", month=9, day=9),
    ]

    monkeypatch.setattr(climod, "get_fetcher", fake_fetcher(snapchat))
    first = run(runner, home, "sync", "--source", "snapchat", "--headless", "--no-import")
    assert first.exit_code == 0, first.output
    week1 = uids_in(Store(home).ics_path())

    def two_sources(source: str):
        async def _fetch(store, headless: bool = False, timeout: float = 300.0):
            return snapchat if source == "snapchat" else facebook

        return _fetch

    monkeypatch.setattr(climod, "get_fetcher", two_sources)
    second = run(
        runner, home, "sync", "--source", "snapchat", "--source", "facebook",
        "--headless", "--no-import",
    )
    assert second.exit_code == 0, second.output
    week2 = uids_in(Store(home).ics_path())

    assert len(week1) == 3
    assert set(week1) <= set(week2), "an existing event's UID moved -> duplicate in the calendar"
    assert len(week2) == len(set(week2)) == 4, "one snapchat-only friend + three merged"


def test_repeated_syncs_are_idempotent(runner, tmp_path, monkeypatch):
    home = tmp_path / "state"
    people = [snap_person("janedoe22", 5, 6), snap_person("bestie", 9, 9, name="Best Friend")]
    monkeypatch.setattr(climod, "get_fetcher", fake_fetcher(people))

    run(runner, home, "sync", "--source", "snapchat", "--headless", "--no-import")
    first = uids_in(Store(home).ics_path())
    run(runner, home, "sync", "--source", "snapchat", "--headless", "--no-import")
    second = uids_in(Store(home).ics_path())

    assert first == second


def test_list_and_status_do_not_mint_uids_for_people_never_exported(
    runner, tmp_path, monkeypatch
):
    """Read-only commands must not write the ledger; only a real export assigns a UID."""
    home = tmp_path / "state"
    store = Store(home)
    store.save_people("facebook", [fb_person(1)])

    assert run(runner, home, "list").exit_code == 0
    assert run(runner, home, "status").exit_code == 0
    assert not store.uid_map_path().exists()

    assert run(runner, home, "export", "--no-import").exit_code == 0
    assert store.uid_map_path().exists()
    assert store.load_uid_map()


def test_the_ledger_is_reused_rather_than_regenerated(runner, tmp_path, monkeypatch):
    home = tmp_path / "state"
    store = Store(home)
    store.save_people("facebook", [fb_person(1)])
    run(runner, home, "export", "--no-import")
    written = store.uid_map_path().stat().st_mtime_ns
    before = store.load_uid_map()

    os.utime(store.uid_map_path(), ns=(written, written))
    run(runner, home, "export", "--no-import")

    assert store.load_uid_map() == before
    assert store.uid_map_path().stat().st_mtime_ns == written, "nothing new to learn"
