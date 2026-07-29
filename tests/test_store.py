"""Tests for :mod:`birthdays.store` -- the on-disk state directory.

Every test drives the store through an explicit ``tmp_path`` root (or through a
monkeypatched ``$BIRTHDAYS_HOME``); nothing here may touch the real
``~/.birthdays``.  ``tests/conftest.py`` redirects ``$HOME`` and
``$BIRTHDAYS_HOME`` as a second line of defence.

This program is free software: you can redistribute it and/or modify it under
the terms of the GNU General Public License as published by the Free Software
Foundation, either version 3 of the License, or (at your option) any later
version.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from birthdays.models import Person
from birthdays.store import DEFAULT_HOME, HOME_ENV_VAR, Store


# --------------------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------------------


def make_store(tmp_path: Path, name: str = "store") -> Store:
    """A Store rooted somewhere inside the per-test tmp dir."""
    return Store(tmp_path / name)


def person(**overrides) -> Person:
    fields = {
        "source": "facebook",
        "source_id": "100001",
        "name": "Jane Doe",
        "month": 3,
        "day": 14,
        "year": 1991,
        "profile_url": "https://facebook.com/100001",
        "username": "janedoe",
    }
    fields.update(overrides)
    return Person(**fields)


# --------------------------------------------------------------------------------------
# root resolution: ctor arg vs $BIRTHDAYS_HOME vs default
# --------------------------------------------------------------------------------------


def test_root_defaults_to_env_var(tmp_path, monkeypatch):
    home = tmp_path / "env-home"
    monkeypatch.setenv(HOME_ENV_VAR, str(home))

    store = Store()

    assert store.root == home
    assert store.root.is_dir()


def test_ctor_root_beats_env_var(tmp_path, monkeypatch):
    monkeypatch.setenv(HOME_ENV_VAR, str(tmp_path / "env-home"))
    explicit = tmp_path / "explicit-home"

    store = Store(explicit)

    assert store.root == explicit
    assert store.root.is_dir()
    assert not (tmp_path / "env-home").exists()


def test_blank_env_var_falls_back_to_default_home(tmp_path, monkeypatch):
    """An empty/whitespace $BIRTHDAYS_HOME must not resolve the root to ``.``."""
    fake_home = tmp_path / "unix-home"
    fake_home.mkdir()
    monkeypatch.setenv("HOME", str(fake_home))
    monkeypatch.setenv(HOME_ENV_VAR, "   ")

    store = Store()

    assert store.root == Path(DEFAULT_HOME).expanduser()
    assert store.root == fake_home / ".birthdays"
    assert store.root.is_dir()


def test_env_var_absent_uses_default_home(tmp_path, monkeypatch):
    fake_home = tmp_path / "unix-home"
    fake_home.mkdir()
    monkeypatch.setenv("HOME", str(fake_home))
    monkeypatch.delenv(HOME_ENV_VAR, raising=False)

    store = Store()

    assert store.root == fake_home / ".birthdays"


def test_root_expands_user_and_is_created(tmp_path, monkeypatch):
    fake_home = tmp_path / "unix-home"
    fake_home.mkdir()
    monkeypatch.setenv("HOME", str(fake_home))

    store = Store("~/somewhere/deep")

    assert store.root == fake_home / "somewhere" / "deep"
    assert store.root.is_dir()


# --------------------------------------------------------------------------------------
# paths
# --------------------------------------------------------------------------------------


def test_ics_path_is_under_root(tmp_path):
    store = make_store(tmp_path)

    ics = store.ics_path()

    assert ics.parent == store.root
    assert ics == store.root / "birthdays.ics"
    # Asking for the path must not create the file.
    assert not ics.exists()


def test_data_and_raw_dirs_are_under_root(tmp_path):
    store = make_store(tmp_path)

    assert store.data_dir == store.root / "data"
    assert store.raw_dir == store.root / "raw"
    assert store.profiles_dir == store.root / "profiles"
    assert store.data_path("facebook") == store.root / "data" / "facebook.json"


def test_profile_dir_creates_and_is_idempotent(tmp_path):
    store = make_store(tmp_path)

    first = store.profile_dir("facebook")
    assert first.is_dir()
    assert first == store.profiles_dir / "facebook"

    (first / "Cookies").write_text("session", encoding="utf-8")

    second = store.profile_dir("facebook")

    assert second == first
    assert (second / "Cookies").read_text(encoding="utf-8") == "session"


def test_profile_dir_sanitises_source(tmp_path):
    store = make_store(tmp_path)

    path = store.profile_dir("../../evil")

    assert path.parent == store.profiles_dir
    assert store.profiles_dir in path.parents


# --------------------------------------------------------------------------------------
# save_people / load_people round-trip
# --------------------------------------------------------------------------------------


def test_round_trip_preserves_every_field(tmp_path):
    store = make_store(tmp_path)
    people = [
        person(),
        person(
            source_id="100002",
            name="John Smith",
            month=12,
            day=31,
            year=None,
            profile_url=None,
            username=None,
        ),
    ]

    store.save_people("facebook", people)
    loaded = store.load_people("facebook")

    assert loaded == people
    for original, restored in zip(people, loaded):
        assert restored.to_dict() == original.to_dict()

    no_year = loaded[1]
    assert no_year.year is None
    assert no_year.username is None
    assert no_year.profile_url is None


def test_save_people_replaces_previous_contents(tmp_path):
    store = make_store(tmp_path)
    store.save_people("facebook", [person(), person(source_id="2", name="B")])

    store.save_people("facebook", [person(source_id="3", name="C")])

    loaded = store.load_people("facebook")
    assert [p.source_id for p in loaded] == ["3"]


def test_save_people_writes_source_and_timestamp(tmp_path):
    store = make_store(tmp_path)

    store.save_people("facebook", [person()])

    payload = json.loads(store.data_path("facebook").read_text(encoding="utf-8"))
    assert payload["source"] == "facebook"
    assert payload["fetched_at"].endswith("Z")
    assert store.fetched_at("facebook") == payload["fetched_at"]


def test_save_people_empty_list_round_trips(tmp_path):
    store = make_store(tmp_path)

    store.save_people("facebook", [])

    assert store.data_path("facebook").exists()
    assert store.load_people("facebook") == []


def test_save_people_leaves_no_temp_files_behind(tmp_path):
    """The atomic write must not leave droppings that load_people() re-reads."""
    store = make_store(tmp_path)

    store.save_people("facebook", [person()])
    store.save_people("facebook", [person()])

    assert sorted(p.name for p in store.data_dir.iterdir()) == ["facebook.json"]


def test_save_and_load_are_case_insensitive_on_source(tmp_path):
    """``save_people("Facebook")`` must be readable as ``load_people("facebook")``.

    The stored payload already lower-cases the source name, so the filename has
    to agree or the cache silently disappears.
    """
    store = make_store(tmp_path)

    store.save_people("Facebook", [person()])

    # Asserted on the path itself as well as through the API: macOS filesystems
    # are case-insensitive, so a round-trip alone would not catch a regression
    # that only bites on Linux/CI.
    assert store.data_path("Facebook").name == "facebook.json"
    assert store.data_path("Facebook") == store.data_path("facebook")
    assert [p.source_id for p in store.load_people("facebook")] == ["100001"]
    assert [p.source_id for p in store.load_people("Facebook")] == ["100001"]
    assert [p.source_id for p in store.load_people(None)] == ["100001"]


# --------------------------------------------------------------------------------------
# load_people: aggregation, missing data, corrupt data
# --------------------------------------------------------------------------------------


def test_load_people_none_concatenates_all_sources(tmp_path):
    store = make_store(tmp_path)
    fb = [person(), person(source_id="100002", name="John Smith")]
    snap = [
        Person(
            source="snapchat",
            source_id="s-1",
            name="Jane D",
            month=3,
            day=14,
            username="janedoe22",
        )
    ]
    store.save_people("facebook", fb)
    store.save_people("snapchat", snap)

    everyone = store.load_people(None)

    assert len(everyone) == 3
    assert set(everyone) == set(fb + snap)
    assert {p.source for p in everyone} == {"facebook", "snapchat"}


def test_load_people_filters_to_one_source(tmp_path):
    store = make_store(tmp_path)
    store.save_people("facebook", [person()])
    store.save_people(
        "snapchat",
        [Person(source="snapchat", source_id="s-1", name="Jane D", month=3, day=14)],
    )

    only_fb = store.load_people("facebook")

    assert [p.source for p in only_fb] == ["facebook"]
    assert [p.source_id for p in only_fb] == ["100001"]


def test_load_people_no_data_returns_empty(tmp_path):
    store = make_store(tmp_path)

    assert store.load_people() == []
    assert store.load_people(None) == []
    assert store.load_people("facebook") == []
    assert store.fetched_at("facebook") is None


def test_load_people_unknown_source_returns_empty(tmp_path):
    store = make_store(tmp_path)
    store.save_people("facebook", [person()])

    assert store.load_people("myspace") == []


def test_corrupt_file_is_skipped_and_other_sources_still_load(tmp_path, capsys):
    """One truncated cache must not brick the whole tool."""
    store = make_store(tmp_path)
    store.save_people("facebook", [person()])
    store.save_people(
        "snapchat",
        [Person(source="snapchat", source_id="s-1", name="Jane D", month=3, day=14)],
    )

    # Truncate the facebook cache mid-object, as a killed process would.
    good = store.data_path("facebook").read_text(encoding="utf-8")
    store.data_path("facebook").write_text(good[: len(good) // 2], encoding="utf-8")

    everyone = store.load_people(None)

    assert [p.source for p in everyone] == ["snapchat"]

    err = capsys.readouterr().err
    assert "facebook.json" in err
    assert "warning" in err.lower()
    # The healthy source must not have generated a warning.
    assert "snapchat.json" not in err


def test_corrupt_single_source_load_warns_and_returns_empty(tmp_path, capsys):
    store = make_store(tmp_path)
    store.data_dir.mkdir(parents=True, exist_ok=True)
    store.data_path("facebook").write_text("{not json at all", encoding="utf-8")

    assert store.load_people("facebook") == []
    assert "facebook.json" in capsys.readouterr().err


def test_non_object_json_is_skipped_with_warning(tmp_path, capsys):
    store = make_store(tmp_path)
    store.data_dir.mkdir(parents=True, exist_ok=True)
    store.data_path("facebook").write_text('"just a string"', encoding="utf-8")

    assert store.load_people("facebook") == []
    assert "facebook.json" in capsys.readouterr().err


def test_bare_list_of_people_is_tolerated(tmp_path):
    store = make_store(tmp_path)
    store.data_dir.mkdir(parents=True, exist_ok=True)
    store.data_path("facebook").write_text(
        json.dumps([person().to_dict()]), encoding="utf-8"
    )

    assert store.load_people("facebook") == [person()]


def test_bad_record_is_skipped_but_good_records_survive(tmp_path, capsys):
    store = make_store(tmp_path)
    store.data_dir.mkdir(parents=True, exist_ok=True)
    payload = {
        "source": "facebook",
        "people": [
            {"source": "facebook", "source_id": "1", "name": "Bad", "month": 13, "day": 1},
            person().to_dict(),
            "not-a-record",
        ],
    }
    store.data_path("facebook").write_text(json.dumps(payload), encoding="utf-8")

    loaded = store.load_people("facebook")

    assert [p.name for p in loaded] == ["Jane Doe"]
    err = capsys.readouterr().err
    assert "#0" in err
    assert "#2" in err


def test_unreadable_entry_is_skipped_and_others_still_load(tmp_path, capsys):
    """A directory called ``*.json`` (or any unreadable entry) must not raise."""
    store = make_store(tmp_path)
    store.save_people("snapchat", [Person(source="snapchat", source_id="s-1",
                                          name="Jane D", month=3, day=14)])
    (store.data_dir / "facebook.json").mkdir()

    everyone = store.load_people(None)

    assert [p.source for p in everyone] == ["snapchat"]
    assert "facebook.json" in capsys.readouterr().err


def test_leftover_atomic_write_temp_file_is_ignored(tmp_path):
    """A crash mid-write leaves a dot-prefixed temp file; it must not be loaded."""
    store = make_store(tmp_path)
    store.save_people("facebook", [person()])
    (store.data_dir / ".facebook.json.tmp123").write_text(
        json.dumps({"people": [person(source_id="ghost").to_dict()]}), encoding="utf-8"
    )

    assert [p.source_id for p in store.load_people(None)] == ["100001"]


# --------------------------------------------------------------------------------------
# connected_sources
# --------------------------------------------------------------------------------------


def test_connected_sources_empty_when_nothing_connected(tmp_path):
    store = make_store(tmp_path)

    assert store.connected_sources() == []


def test_connected_sources_ignores_empty_profile_dirs(tmp_path):
    store = make_store(tmp_path)
    store.profile_dir("facebook")  # created, but the user never logged in

    assert store.connected_sources() == []


def test_connected_sources_lists_only_non_empty_profiles(tmp_path):
    store = make_store(tmp_path)
    fb = store.profile_dir("facebook")
    store.profile_dir("snapchat")  # left empty
    (fb / "Cookies").write_text("session", encoding="utf-8")

    assert store.connected_sources() == ["facebook"]


def test_connected_sources_ignores_stray_files(tmp_path):
    store = make_store(tmp_path)
    snap = store.profile_dir("snapchat")
    (snap / "Cookies").write_text("session", encoding="utf-8")
    (store.profiles_dir / "README.txt").write_text("not a profile", encoding="utf-8")

    assert store.connected_sources() == ["snapchat"]


def test_connected_sources_is_sorted(tmp_path):
    store = make_store(tmp_path)
    for name in ("snapchat", "facebook"):
        path = store.profile_dir(name)
        (path / "Cookies").write_text("session", encoding="utf-8")

    assert store.connected_sources() == ["facebook", "snapchat"]


def test_connected_sources_keys_match_the_source_names_used_elsewhere(tmp_path):
    """A profile created as "Facebook" must still be reported as "facebook"."""
    store = make_store(tmp_path)
    path = store.profile_dir("Facebook")
    (path / "Cookies").write_text("session", encoding="utf-8")

    assert store.connected_sources() == ["facebook"]


# --------------------------------------------------------------------------------------
# save_raw
# --------------------------------------------------------------------------------------


def test_save_raw_writes_bytes_and_returns_path(tmp_path):
    store = make_store(tmp_path)

    path = store.save_raw("facebook", "graphql-0.json", b'{"data": 1}')

    assert path.exists()
    assert path.parent == store.raw_dir
    assert path.read_bytes() == b'{"data": 1}'
    assert path.name == "facebook-graphql-0.json"


def test_save_raw_twice_does_not_overwrite(tmp_path):
    store = make_store(tmp_path)

    first = store.save_raw("snapchat", "syncfrienddata.bin", b"payload-one")
    second = store.save_raw("snapchat", "syncfrienddata.bin", b"payload-two")

    assert first != second
    assert first.exists() and second.exists()
    assert first.read_bytes() == b"payload-one"
    assert second.read_bytes() == b"payload-two"
    assert second.suffix == ".bin"
    assert sorted(p.name for p in store.raw_dir.iterdir()) == [
        "snapchat-syncfrienddata-1.bin",
        "snapchat-syncfrienddata.bin",
    ]


def test_save_raw_three_times_keeps_every_payload(tmp_path):
    store = make_store(tmp_path)
    payloads = [b"one", b"two", b"three"]

    paths = [store.save_raw("facebook", "dump", data) for data in payloads]

    assert len({p.name for p in paths}) == 3
    assert [p.read_bytes() for p in paths] == payloads
    assert len(list(store.raw_dir.iterdir())) == 3


def test_save_raw_accepts_str_payload(tmp_path):
    store = make_store(tmp_path)

    path = store.save_raw("facebook", "page.html", "<html>ü</html>")

    assert path.read_text(encoding="utf-8") == "<html>ü</html>"


def test_save_raw_sanitises_label(tmp_path):
    store = make_store(tmp_path)

    path = store.save_raw("facebook", "../../etc/passwd", b"x")

    assert path.parent == store.raw_dir
    assert "/" not in path.name


def test_save_raw_blank_label_gets_a_fallback(tmp_path):
    store = make_store(tmp_path)

    path = store.save_raw("facebook", "", b"x")

    assert path.parent == store.raw_dir
    assert path.name


def test_save_raw_creates_raw_dir_lazily(tmp_path):
    store = make_store(tmp_path)
    assert not store.raw_dir.exists()

    store.save_raw("facebook", "dump", b"x")

    assert store.raw_dir.is_dir()


# --------------------------------------------------------------------------------------
# isolation guard
# --------------------------------------------------------------------------------------


@pytest.mark.parametrize("method", ["ics_path", "data_dir", "raw_dir", "profiles_dir"])
def test_nothing_escapes_the_tmp_root(tmp_path, method):
    store = make_store(tmp_path)
    attr = getattr(store, method)
    path = attr() if callable(attr) else attr

    assert tmp_path in path.parents
    assert Path.home() not in path.parents
