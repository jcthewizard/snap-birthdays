#!/usr/bin/env python3
"""
    birthdays - cross-source friend birthday sync
    calendar_import.py - hand the generated .ics to the local calendar app.

    Derived from / inspired by:
      * fb2cal   (GPL-3.0, Copyright (C) mobeigi)            https://github.com/mobeigi/fb2cal
      * Snap2Calendar-Birthday-Export (MIT, (c) James Arnott) https://github.com/J4A-Industries/Snap2Calendar-Birthday-Export

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

import subprocess
import sys
from pathlib import Path

__all__ = ["is_macos", "import_ics"]


def is_macos() -> bool:
    """True when running on macOS, where we can drive Calendar.app directly."""
    return sys.platform == "darwin"


def import_ics(path: Path) -> None:
    """Open ``path`` in the system calendar (macOS) or explain what to do elsewhere.

    Never raises on a non-macOS platform: it just prints import instructions.
    """
    path = Path(path).expanduser().resolve()
    if not path.exists():
        raise FileNotFoundError(f"No calendar file at {path} - run `birthdays sync` first.")

    if not is_macos():
        _print_manual_instructions(path)
        return

    print(f"Opening {path} in Calendar.app ...")
    subprocess.run(["open", "-a", "Calendar", str(path)], check=True)
    print(
        "\nCalendar will ask which calendar to add these events to."
        "\nTip: pick (or create) a dedicated calendar such as 'Friend Birthdays' so you"
        "\ncan hide or delete the whole set in one go."
        "\n"
        "\nEvery event carries a stable UID derived from the person's name and birthday,"
        "\nso re-importing this file after a later `birthdays sync` updates the existing"
        "\nevents in place instead of creating duplicates."
    )


def _print_manual_instructions(path: Path) -> None:
    print(f"Calendar file written to:\n  {path}\n")
    print("This platform has no supported one-click import, so add it by hand:")
    print(
        "\nGoogle Calendar (one-off import):"
        "\n  1. Open https://calendar.google.com/calendar/r/settings/export"
        "\n  2. Settings -> Import & export -> Import"
        f"\n  3. Select {path.name}, choose the destination calendar, click Import"
        "\n"
        "\nGoogle Calendar (auto-updating): host the file at a public URL and use"
        "\n  'Other calendars' -> '+' -> 'From URL' instead, so each sync propagates."
        "\n"
        "\nOutlook: File -> Open & Export -> Import/Export -> Import an iCalendar (.ics)."
        "\nThunderbird / Evolution: Calendar -> Import -> pick the file."
        "\n"
        "\nEvent UIDs are stable across runs, so re-importing updates the existing"
        "\nentries rather than duplicating them."
    )
