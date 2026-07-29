"""
    birthdays - command line interface.

    Subcommands:
      connect <facebook|snapchat|all>   log in by hand in a real browser window
      sync                              fetch every connected source -> merge -> ICS
      export                            same, from the cache, without a browser
      list                              print merged birthdays
      status                            what is connected / cached / where things are

    Derived from:
      * fb2cal (https://github.com/mobeigi/fb2cal) - GPL-3.0
      * Snap2Calendar-Birthday-Export
        (https://github.com/J4A-Industries/Snap2Calendar-Birthday-Export) - MIT

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
import calendar
import sys
from datetime import datetime, timezone
from pathlib import Path

import click

from . import __version__
from .calendar_import import import_ics
from .dedupe import UidLedger, member_key, merge
from .icsfile import write_ics
from .models import MergedPerson, Person, source_label
from .sources import AVAILABLE_SOURCES, get_fetcher
from .store import Store

__all__ = ["main", "cli"]

DEFAULT_TIMEOUT = 300.0


# --------------------------------------------------------------------------------------
# small output helpers
# --------------------------------------------------------------------------------------


def _out(message: str = "") -> None:
    """Result output: stdout, so `birthdays list` stays pipeable."""
    click.echo(message)


def _note(message: str = "") -> None:
    """Progress / diagnostics: stderr, so they never pollute piped output."""
    click.echo(message, err=True)


def _fail(message: str) -> None:
    """Print an error and exit non-zero."""
    raise click.ClickException(message)


def _store(ctx: click.Context) -> Store:
    """The Store for this invocation, built on first use.

    Constructing a Store creates its root directory, so we do it lazily: nobody
    wants `birthdays connect --help` to leave a ~/.birthdays behind.
    """
    store = ctx.obj.get("store")
    if store is None:
        store = Store(ctx.obj.get("home"))
        ctx.obj["store"] = store
    return store


def _resolve_sources(requested: tuple[str, ...], store: Store) -> list[str]:
    """Sources to work on: the ones asked for, else everything connected."""
    if requested:
        seen: list[str] = []
        for name in requested:
            key = name.strip().lower()
            if key not in seen:
                seen.append(key)
        return seen
    return [s for s in store.connected_sources() if s in AVAILABLE_SOURCES]


# --------------------------------------------------------------------------------------
# fetching
# --------------------------------------------------------------------------------------


def _merge_with_cache(
    cached: list[Person], fresh: list[Person]
) -> tuple[list[Person], list[Person]]:
    """Union a fresh fetch with the cache, preferring the fresh record.

    Returns ``(combined, kept_from_cache)``.

    A fetch can succeed *partially*: Facebook asks for the year in four quarterly slices
    and tolerates a slice failing, so a rate-limited or expired-mid-run session returns a
    real but truncated list.  Overwriting the cache with it deletes the friends in the
    missing slices from every later `export`/`list`/ICS until some future fetch happens
    to come back complete.  Keeping the records this fetch did not mention costs nothing
    in return: an ICS import never deletes calendar events anyway, so a friend who really
    has gone stays in the calendar either way.
    """
    combined = list(fresh)
    seen = {member_key(person) for person in fresh}
    kept = [person for person in cached if member_key(person) not in seen]
    combined.extend(kept)
    return combined, kept


def _fetch_one(store: Store, source: str, headless: bool, timeout: float) -> list[Person]:
    """Run one source's async fetch to completion and cache the result."""
    fetch = get_fetcher(source)
    people = asyncio.run(fetch(store, headless=headless, timeout=timeout))
    combined, kept = _merge_with_cache(store.load_people(source), people)
    if kept:
        _note(
            f"    kept {len(kept)} cached {source} record(s) this fetch did not return "
            "(partial fetch?)"
        )
    store.save_people(source, combined)
    return combined


def _fetch_many(
    store: Store,
    sources: list[str],
    headless: bool,
    timeout: float,
    *,
    use_cache: bool = True,
) -> tuple[list[Person], dict[str, int], set[str], list[str]]:
    """Fetch several sources, surviving individual failures.

    Returns ``(people, per_source_counts, sources_served_from_cache, failures)``.

    ``use_cache`` (the default) makes a failed source fall back to whatever it cached on
    a previous run, so one broken site never silently deletes the other site's events
    from the calendar.  ``connect`` turns it off: it builds no calendar, so announcing a
    fallback there would describe work that has no effect -- and contradict the "nothing
    was connected" error printed a line later.
    """
    people: list[Person] = []
    counts: dict[str, int] = {}
    from_cache: set[str] = set()
    failures: list[str] = []

    for source in sources:
        _note(f"==> {source_label(source)}")
        try:
            found = _fetch_one(store, source, headless, timeout)
        except KeyboardInterrupt:
            raise
        except Exception as exc:  # noqa: BLE001 - one bad source must not stop the rest
            failures.append(source)
            _note(f"error: {source} failed: {exc}")
            cached = store.load_people(source) if use_cache else []
            if cached:
                _note(f"       falling back to {len(cached)} cached {source} record(s)")
                counts[source] = len(cached)
                from_cache.add(source)
                people.extend(cached)
            continue
        counts[source] = len(found)
        people.extend(found)
        _note(f"    {len(found)} birthdays from {source}")

    return people, counts, from_cache, failures


# --------------------------------------------------------------------------------------
# rendering
# --------------------------------------------------------------------------------------


def _print_summary(
    counts: dict[str, int],
    from_cache: set[str],
    people: list[Person],
    merged: list[MergedPerson],
) -> None:
    """The per-source / raw / merged table printed by `sync` and `export`."""
    cross = sum(1 for m in merged if len(m.sources) > 1)
    width = max([len(s) for s in counts] + [12])

    _out("")
    _out(f"{'source':<{width}}  {'records':>7}")
    _out(f"{'-' * width}  {'-' * 7}")
    for source in sorted(counts):
        suffix = "  (cached)" if source in from_cache else ""
        _out(f"{source:<{width}}  {counts[source]:>7}{suffix}")
    _out(f"{'-' * width}  {'-' * 7}")
    _out(f"{'total raw':<{width}}  {len(people):>7}")
    _out(f"{'after merge':<{width}}  {len(merged):>7}")
    _out(f"{'cross-source':<{width}}  {cross:>7}  (people found on more than one source)")


def _display_name(person: MergedPerson) -> str:
    """Never print a blank line: fall back to a member's handle, then its id."""
    if person.name:
        return person.name
    for member in person.members:
        if member.username or member.name:
            return member.username or member.name
    return person.members[0].source_id if person.members else "(unknown)"


def _print_people(merged: list[MergedPerson]) -> None:
    """One line per person: ``Mar 14  Jane Doe  [facebook, snapchat]``."""
    for person in merged:
        month = calendar.month_abbr[person.month]
        sources = ", ".join(person.sources)
        _out(f"{month} {person.day:2d}  {_display_name(person)}  [{sources}]")


def _ledger(store: Store) -> UidLedger:
    """The UIDs already exported from this store (see :class:`UidLedger`)."""
    return UidLedger(store.load_uid_map())


def _save_ledger(store: Store, ledger: UidLedger) -> None:
    """Persist newly assigned UIDs so the next run updates events instead of inserting.

    Only the commands that actually write a calendar do this: `list` and `status` read
    the ledger so they show the real UIDs, but must not mint new ones for people who have
    never been exported.
    """
    if not ledger.dirty:
        return
    try:
        store.save_uid_map(ledger.as_dict())
    except OSError as exc:  # noqa: BLE001 - the ICS is written; losing the ledger is not fatal
        _note(f"warning: could not save the UID ledger: {exc}")
        _note("         a later sync may re-create calendar events instead of updating them.")


def _emit_calendar(
    merged: list[MergedPerson], path: Path, calname: str, do_import: bool
) -> None:
    """Write the ICS and (optionally) hand it to the calendar app."""
    written = write_ics(merged, path, calname=calname)
    _out(f"\nWrote {len(merged)} events to {written}")
    if not do_import:
        return
    try:
        import_ics(written)
    except Exception as exc:  # noqa: BLE001 - the file is written; import is a bonus
        _note(f"warning: could not open the calendar app: {exc}")
        _note(f"         import {written} by hand.")


# --------------------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------------------


@click.group(context_settings={"help_option_names": ["-h", "--help"]})
@click.option(
    "--home",
    type=click.Path(file_okay=False, path_type=Path),
    default=None,
    help="Override the state directory (default: ~/.birthdays).",
)
@click.version_option(__version__, "-V", "--version", prog_name="birthdays")
@click.pass_context
def cli(ctx: click.Context, home: Path | None) -> None:
    """Collect friend birthdays from Facebook and Snapchat into one calendar.

    First run: `birthdays connect facebook` (a real browser window opens - log in by
    hand), then `birthdays sync`.  Re-running never duplicates calendar events.
    """
    ctx.ensure_object(dict)
    ctx.obj["home"] = home


@cli.command()
@click.argument(
    "source",
    type=click.Choice([*AVAILABLE_SOURCES, "all"], case_sensitive=False),
)
@click.option("--headless", is_flag=True, help="No window (only works once logged in).")
@click.option(
    "--timeout",
    type=float,
    default=DEFAULT_TIMEOUT,
    show_default=True,
    metavar="SECONDS",
    help="How long to wait for the login plus the fetch.",
)
@click.pass_context
def connect(ctx: click.Context, source: str, headless: bool, timeout: float) -> None:
    """Log in to a SOURCE (facebook, snapchat or all) and cache its birthdays.

    A real browser window opens so you can complete 2FA / captcha / checkpoints by
    hand.  The session is stored under ~/.birthdays/profiles/<source>/ and reused by
    every later run.
    """
    store = _store(ctx)
    sources = list(AVAILABLE_SOURCES) if source.lower() == "all" else [source.lower()]

    if not headless:
        _note("A browser window will open. Log in there, then come back to this terminal.")

    _people, counts, _cached, failures = _fetch_many(
        store, sources, headless, timeout, use_cache=False
    )

    for name in sources:
        if name in failures:
            continue
        _out(f"Found {counts.get(name, 0)} birthdays from {name}")

    if failures and len(failures) == len(sources):
        _fail("every source failed; nothing was connected.")
    if failures:
        _note(f"note: {', '.join(failures)} failed; the other source(s) are connected.")


@cli.command()
@click.option(
    "--source",
    "sources",
    multiple=True,
    type=click.Choice(AVAILABLE_SOURCES, case_sensitive=False),
    help="Only sync this source (repeatable). Default: every connected source.",
)
@click.option("--no-import", "no_import", is_flag=True, help="Do not open the calendar app.")
@click.option(
    "--out",
    type=click.Path(dir_okay=False, path_type=Path),
    default=None,
    help="Where to write the .ics (default: ~/.birthdays/birthdays.ics).",
)
@click.option(
    "--calname", default="Friend Birthdays", show_default=True, help="Calendar name."
)
@click.option("--headless", is_flag=True, help="No window (only works once logged in).")
@click.option(
    "--timeout",
    type=float,
    default=DEFAULT_TIMEOUT,
    show_default=True,
    metavar="SECONDS",
    help="Per-source budget for the fetch.",
)
@click.pass_context
def sync(
    ctx: click.Context,
    sources: tuple[str, ...],
    no_import: bool,
    out: Path | None,
    calname: str,
    headless: bool,
    timeout: float,
) -> None:
    """Fetch every connected source, merge, write the ICS and import it."""
    store = _store(ctx)
    targets = _resolve_sources(sources, store)
    if not targets:
        _fail(
            "no sources are connected yet.\n"
            "Run `birthdays connect facebook` and/or `birthdays connect snapchat` first."
        )

    people, counts, from_cache, failures = _fetch_many(store, targets, headless, timeout)

    if len(failures) == len(targets) and not people:
        _fail("every source failed; the calendar was left untouched.")

    ledger = _ledger(store)
    merged = merge(people, ledger)
    _print_summary(counts, from_cache, people, merged)
    _emit_calendar(merged, out or store.ics_path(), calname, not no_import)
    _save_ledger(store, ledger)

    if failures:
        _note(f"\nnote: these sources failed this run: {', '.join(failures)}")


@cli.command()
@click.option(
    "--source",
    "sources",
    multiple=True,
    type=click.Choice(AVAILABLE_SOURCES, case_sensitive=False),
    help="Only export this source (repeatable). Default: everything cached.",
)
@click.option("--no-import", "no_import", is_flag=True, help="Do not open the calendar app.")
@click.option(
    "--out",
    type=click.Path(dir_okay=False, path_type=Path),
    default=None,
    help="Where to write the .ics (default: ~/.birthdays/birthdays.ics).",
)
@click.option(
    "--calname", default="Friend Birthdays", show_default=True, help="Calendar name."
)
@click.pass_context
def export(
    ctx: click.Context,
    sources: tuple[str, ...],
    no_import: bool,
    out: Path | None,
    calname: str,
) -> None:
    """Rebuild the ICS from CACHED data - no browser, no network."""
    store = _store(ctx)
    people, counts = _load_cached(store, sources)
    if not people:
        _fail("no cached birthdays. Run `birthdays sync` first.")

    _note("using cached data - nothing was fetched")
    ledger = _ledger(store)
    merged = merge(people, ledger)
    _print_summary(counts, set(), people, merged)
    _emit_calendar(merged, out or store.ics_path(), calname, not no_import)
    _save_ledger(store, ledger)


@cli.command("list")
@click.option(
    "--source",
    "sources",
    multiple=True,
    type=click.Choice(AVAILABLE_SOURCES, case_sensitive=False),
    help="Only list this source (repeatable).",
)
@click.pass_context
def list_cmd(ctx: click.Context, sources: tuple[str, ...]) -> None:
    """Print cached birthdays, merged and sorted by date."""
    store = _store(ctx)
    people, _counts = _load_cached(store, sources)
    if not people:
        _fail("no cached birthdays. Run `birthdays sync` first.")
    _print_people(merge(people, _ledger(store)))


@cli.command()
@click.pass_context
def status(ctx: click.Context) -> None:
    """Show connected sources, cached counts, last fetch times and the ICS path."""
    store = _store(ctx)
    connected = set(store.connected_sources())

    _out(f"home      {store.root}")
    ics = store.ics_path()
    if ics.exists():
        stamp = datetime.fromtimestamp(ics.stat().st_mtime, timezone.utc)
        _out(f"calendar  {ics}  (written {stamp.strftime('%Y-%m-%d %H:%M:%SZ')})")
    else:
        _out(f"calendar  {ics}  (not written yet)")
    _out("")

    _out(f"{'source':<10}  {'connected':<9}  {'cached':>6}  last fetch")
    _out(f"{'-' * 10}  {'-' * 9}  {'-' * 6}  {'-' * 20}")
    total = 0
    for source in AVAILABLE_SOURCES:
        cached = len(store.load_people(source))
        total += cached
        fetched = store.fetched_at(source) or "never"
        _out(
            f"{source:<10}  {'yes' if source in connected else 'no':<9}  "
            f"{cached:>6}  {fetched}"
        )

    unknown = sorted(connected - set(AVAILABLE_SOURCES))
    for source in unknown:
        _out(f"{source:<10}  {'yes':<9}  {'?':>6}  (unknown source)")

    _out("")
    if total:
        merged = merge(store.load_people(), _ledger(store))
        cross = sum(1 for m in merged if len(m.sources) > 1)
        _out(f"{total} cached records -> {len(merged)} people ({cross} cross-source merges)")
    if not connected:
        _out("No browser session yet. Start with: birthdays connect facebook")
    elif not total:
        _out("Connected, but nothing cached yet. Run: birthdays sync")


def _load_cached(
    store: Store, sources: tuple[str, ...]
) -> tuple[list[Person], dict[str, int]]:
    """Read cached people for the requested sources (or all of them)."""
    names = [s.strip().lower() for s in sources]
    if not names:
        people = store.load_people()
    else:
        people = []
        for name in names:
            people.extend(store.load_people(name))

    counts: dict[str, int] = {}
    for person in people:
        counts[person.source] = counts.get(person.source, 0) + 1
    return people, counts


def main() -> None:
    """Console-script entry point (see ``[project.scripts]``).

    ``standalone_mode=False`` hands us click's exceptions instead of letting it
    exit for us, which is the only way to turn a Ctrl-C (click re-raises it as
    ``Abort``) into a friendly message and the conventional 130 exit status.
    A fetch can sit in front of a browser window for minutes, so Ctrl-C is the
    normal way to stop this program, not an edge case.
    """
    try:
        code = cli.main(args=None, obj={}, standalone_mode=False)
    except click.ClickException as exc:
        exc.show()
        sys.exit(exc.exit_code)
    except (click.exceptions.Abort, KeyboardInterrupt):
        click.echo("\nInterrupted.", err=True)
        sys.exit(130)
    sys.exit(code if isinstance(code, int) else 0)


if __name__ == "__main__":  # pragma: no cover
    main()
