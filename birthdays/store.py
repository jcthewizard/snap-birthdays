"""
    birthdays - friend birthdays from Facebook/Snapchat to an ICS calendar.

    On-disk state: browser profiles, cached people, raw debug dumps, the ICS.

    Layout (root defaults to ~/.birthdays, override with $BIRTHDAYS_HOME):

        <root>/profiles/<source>/     Playwright persistent context (login session)
        <root>/data/<source>.json     cached Person records for that source
        <root>/raw/<source>-<label>   raw responses kept for debugging
        <root>/birthdays.ics          generated calendar

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

import json
import os
import re
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

from .models import Person

__all__ = ["Store", "HOME_ENV_VAR", "DEFAULT_HOME"]

#: Environment variable that relocates the whole store (handy for tests).
HOME_ENV_VAR = "BIRTHDAYS_HOME"

#: Directory used when neither the ctor arg nor $BIRTHDAYS_HOME is given.
DEFAULT_HOME = "~/.birthdays"

_UNSAFE_CHARS = re.compile(r"[^A-Za-z0-9._-]+")


def _warn(message: str) -> None:
    print(f"birthdays: warning: {message}", file=sys.stderr)


def _safe_component(value: str, fallback: str = "unknown") -> str:
    """Squash an arbitrary string into a single safe path component."""
    cleaned = _UNSAFE_CHARS.sub("-", (value or "").strip()).strip("-.")
    return cleaned or fallback


def _source_component(source: str) -> str:
    """Path component for a source name.

    Source names are case-insensitive everywhere else (``Person.source`` and the
    ``source`` field written into each cache are lower-cased), so the paths
    derived from them must be too: otherwise ``save_people("Facebook")``
    followed by ``load_people("facebook")`` silently reads nothing on a
    case-sensitive filesystem, and ``connected_sources()`` reports a key that no
    other module recognises.
    """
    return _safe_component(_clean_source(source))


def _utc_now_iso() -> str:
    """Current UTC time as ISO 8601 with a trailing Z, second resolution."""
    return (
        datetime.now(timezone.utc)
        .replace(microsecond=0)
        .isoformat()
        .replace("+00:00", "Z")
    )


class Store:
    """Everything the tool persists between runs lives under :attr:`root`."""

    def __init__(self, root: Path | None = None) -> None:
        if root is None:
            env_root = os.environ.get(HOME_ENV_VAR, "").strip()
            root = Path(env_root) if env_root else Path(DEFAULT_HOME)
        self.root: Path = Path(root).expanduser()
        self.root.mkdir(parents=True, exist_ok=True)

    def __repr__(self) -> str:
        return f"Store(root={str(self.root)!r})"

    # ------------------------------------------------------------------
    # directories
    # ------------------------------------------------------------------

    @property
    def profiles_dir(self) -> Path:
        return self.root / "profiles"

    @property
    def data_dir(self) -> Path:
        return self.root / "data"

    @property
    def raw_dir(self) -> Path:
        return self.root / "raw"

    def profile_dir(self, source: str) -> Path:
        """Playwright persistent-context directory for ``source`` (created)."""
        path = self.profiles_dir / _source_component(source)
        path.mkdir(parents=True, exist_ok=True)
        return path

    def data_path(self, source: str) -> Path:
        """Where cached people for ``source`` are stored (not created)."""
        return self.data_dir / f"{_source_component(source)}.json"

    def ics_path(self) -> Path:
        """Path of the generated calendar file."""
        return self.root / "birthdays.ics"

    def uid_map_path(self) -> Path:
        """Where the exported-UID ledger lives (not created).

        Deliberately *not* under ``data/``: ``load_people(None)`` reads every JSON file
        in that directory as a people cache.
        """
        return self.root / "uids.json"

    # ------------------------------------------------------------------
    # UID ledger
    # ------------------------------------------------------------------

    def load_uid_map(self) -> dict[str, str]:
        """UIDs already exported, keyed by ``"<source>:<source_id>"``.

        This file is the anti-duplicate contract with the calendar app: a UID that moves
        between runs makes the client insert a second event instead of updating the
        first.  A missing or corrupt file therefore degrades to "recompute the UIDs"
        rather than raising -- the same behaviour as a first run.
        """
        path = self.uid_map_path()
        try:
            raw = path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return {}
        except OSError as exc:
            _warn(f"could not read {path}: {exc}")
            return {}

        try:
            payload = json.loads(raw)
        except json.JSONDecodeError as exc:
            _warn(f"could not parse {path}: {exc}; UIDs will be recomputed")
            return {}

        if not isinstance(payload, dict):
            _warn(f"unexpected contents in {path}, ignoring")
            return {}
        # Tolerate both the wrapped form we write and a bare {key: uid} mapping.
        records = payload.get("uids", payload)
        if not isinstance(records, dict):
            _warn(f"unexpected 'uids' value in {path}, ignoring")
            return {}

        return {
            str(key): value
            for key, value in records.items()
            if isinstance(value, str) and value.strip() and str(key).strip()
        }

    def save_uid_map(self, mapping: dict[str, str]) -> None:
        """Persist the exported-UID ledger, replacing what was there."""
        payload = {
            "updated_at": _utc_now_iso(),
            "uids": {key: mapping[key] for key in sorted(mapping)},
        }
        text = json.dumps(payload, indent=2, ensure_ascii=False) + "\n"
        _atomic_write_text(self.uid_map_path(), text)

    # ------------------------------------------------------------------
    # people
    # ------------------------------------------------------------------

    def save_people(self, source: str, people: list[Person]) -> None:
        """Write ``people`` to ``data/<source>.json``, replacing what was there."""
        path = self.data_path(source)
        payload = {
            "source": _clean_source(source),
            "fetched_at": _utc_now_iso(),
            "people": [p.to_dict() for p in people],
        }
        text = json.dumps(payload, indent=2, ensure_ascii=False) + "\n"
        _atomic_write_text(path, text)

    def load_people(self, source: str | None = None) -> list[Person]:
        """Load cached people for one source, or for every source when None.

        Files that cannot be read or parsed are skipped with a warning on stderr
        so one corrupt cache never breaks the whole run.
        """
        if source is not None:
            return self._load_file(self.data_path(source))

        if not self.data_dir.is_dir():
            return []

        people: list[Person] = []
        for path in sorted(self.data_dir.glob("*.json")):
            people.extend(self._load_file(path))
        return people

    def _load_file(self, path: Path) -> list[Person]:
        try:
            raw = path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return []
        except OSError as exc:
            _warn(f"could not read {path}: {exc}")
            return []

        try:
            payload = json.loads(raw)
        except json.JSONDecodeError as exc:
            _warn(f"could not parse {path}: {exc}")
            return []

        if isinstance(payload, dict):
            records = payload.get("people", [])
        elif isinstance(payload, list):
            records = payload  # tolerate a bare list of people
        else:
            _warn(f"unexpected contents in {path}, skipping")
            return []

        if not isinstance(records, list):
            _warn(f"unexpected 'people' value in {path}, skipping")
            return []

        people: list[Person] = []
        for index, record in enumerate(records):
            try:
                people.append(Person.from_dict(record))
            except (ValueError, TypeError, AttributeError) as exc:
                _warn(f"skipping bad record #{index} in {path}: {exc}")
        return people

    def fetched_at(self, source: str) -> str | None:
        """ISO 8601 timestamp of the last successful fetch for ``source``."""
        try:
            payload = json.loads(self.data_path(source).read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None
        if isinstance(payload, dict):
            stamp = payload.get("fetched_at")
            if isinstance(stamp, str) and stamp:
                return stamp
        return None

    # ------------------------------------------------------------------
    # sources
    # ------------------------------------------------------------------

    def connected_sources(self) -> list[str]:
        """Sources whose browser profile exists and is non-empty (i.e. logged in)."""
        if not self.profiles_dir.is_dir():
            return []

        connected: list[str] = []
        try:
            entries = sorted(self.profiles_dir.iterdir())
        except OSError as exc:
            _warn(f"could not list {self.profiles_dir}: {exc}")
            return []

        for entry in entries:
            if not entry.is_dir():
                continue
            try:
                if any(entry.iterdir()):
                    connected.append(entry.name)
            except OSError as exc:
                _warn(f"could not list {entry}: {exc}")
        return connected

    # ------------------------------------------------------------------
    # raw debug dumps
    # ------------------------------------------------------------------

    def save_raw(self, source: str, label: str, data: bytes) -> Path:
        """Dump a raw response to ``raw/<source>-<label>`` for debugging.

        Filenames are deterministic: if the target already exists we append a
        monotonically increasing integer (``-1``, ``-2``, ...) before the
        extension rather than baking in a timestamp.
        """
        self.raw_dir.mkdir(parents=True, exist_ok=True)

        stem_source = _source_component(source)
        safe_label = _safe_component(label, fallback="dump")
        base = Path(f"{stem_source}-{safe_label}")
        stem, suffix = base.stem, base.suffix

        path = self.raw_dir / base.name
        counter = 1
        while path.exists():
            path = self.raw_dir / f"{stem}-{counter}{suffix}"
            counter += 1

        if isinstance(data, str):  # be forgiving; callers sometimes have text
            data = data.encode("utf-8")
        path.write_bytes(data)
        return path


def _clean_source(source: str) -> str:
    return (source or "").strip().lower()


def _atomic_write_text(path: Path, text: str) -> None:
    """Write ``text`` to ``path`` atomically so a crash cannot truncate a cache."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(dir=str(path.parent), prefix=f".{path.name}.")
    tmp_path = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)
        os.replace(tmp_path, path)
    except BaseException:
        tmp_path.unlink(missing_ok=True)
        raise
