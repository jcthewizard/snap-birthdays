"""Shared pytest configuration for the birthdays test suite.

Puts ``tests/fixtures`` on ``sys.path`` so fixture modules (which carry their own
upstream attribution headers) can be imported by name, and keeps every test away
from the real ``~/.birthdays`` directory.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

TESTS_DIR = Path(__file__).resolve().parent
FIXTURES_DIR = TESTS_DIR / "fixtures"

if str(FIXTURES_DIR) not in sys.path:
    sys.path.insert(0, str(FIXTURES_DIR))


@pytest.fixture(autouse=True)
def _isolated_home(tmp_path, monkeypatch):
    """Never touch the developer's real store while testing.

    ``Store()`` falls back to ``$BIRTHDAYS_HOME`` and then to ``~/.birthdays``, so
    both are redirected into the per-test tmp dir.
    """
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("BIRTHDAYS_HOME", str(home / ".birthdays"))
    return home
