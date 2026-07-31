# snap-birthdays

Put your Snapchat friends' birthdays in your calendar.

Snapchat has no API for this, but its web app fetches every friend's birthday in a single
response when it loads. So this opens a real browser, lets you log in by hand, reads that
one response as it goes past, and writes an `.ics` file.

```
427 birthdays from 564 friends (137 keep theirs private)
```

## Run it

```bash
uvx snap-birthdays
```

That's the whole thing. ([Don't have `uv`?](https://docs.astral.sh/uv/getting-started/installation/)
Or use `pipx run snap-birthdays`, or `pip install snap-birthdays && snap-birthdays`.)

A page opens in your browser with four steps — connect Snapchat, pick Apple or Google
Calendar, tick the people you want, import. Your selection is remembered, so the next
run starts where you left off, and anyone who adds you on Snapchat later shows up
already ticked.

<!-- Absolute URL, not a relative path: this file is also the PyPI description, and
     PyPI will not render relative image paths. -->
<img src="https://raw.githubusercontent.com/jcthewizard/snap-birthdays/main/docs/ui.png"
     alt="The snap-birthdays window" width="620">

Nothing leaves your machine. The page is served from `127.0.0.1` on a random port with a
random token, and stops when you press Ctrl-C. Your Snapchat login happens in a real
browser window that only you touch; the session lives in `~/.snap-birthdays/` and is
never transmitted anywhere.

**On the very first run** it downloads a browser for Playwright (~170MB, ~350MB on disk,
once) unless you already have Google Chrome installed, in which case it just uses that.
The download runs before the browser window appears, and its progress is printed in the
terminal you started this from, not in the page.

### Or from the terminal

```bash
snap-birthdays-cli                 # fetch, then open the file in Calendar (macOS)
snap-birthdays-cli --to google     # ...or open Google Calendar's import page
snap-birthdays-cli --to file       # ...or just write the .ics and stop
```

A browser window opens. Log in if it asks; after the first time the session is remembered,
so later runs need nothing from you. Once the friend data goes past, the window closes on
its own.

### Apple Calendar

The default. Calendar opens and asks which calendar to add to — **make a new one** rather
than dumping 400 birthdays into your main calendar, so you can hide or delete the whole
lot in one action later.

### Google Calendar

Opens the import page and hands you the `.ics`. Drag it in, pick a calendar, hit Import.
There's no way to automate this without OAuth credentials and a Google Cloud project,
which is a lot of machinery for something you'll run twice a year.

Works on macOS, Linux and Windows. Apple Calendar is macOS-only for obvious reasons; on
anything else the tool says so and leaves you the file.

### Options

| flag | what it does |
| --- | --- |
| `--to apple\|google\|file` | where to send it (default: `apple` on macOS, else `file`) |
| `--out PATH` | where to write the `.ics` |
| `--calname NAME` | calendar name embedded in the file |
| `--timeout SECONDS` | how long to wait for login + data (default 300) |
| `--from-file PATH` | decode a saved response instead of opening a browser |

## Re-running won't duplicate anything

Each event's `UID` is derived from the friend's Snapchat username, which never changes.
Calendars treat a matching UID as the same event, so importing again updates what's there
instead of adding a second copy. Deliberately not keyed on the display name or the
birthday, since a friend can edit both — rename themselves and you get a renamed event,
not a duplicate.

## Things worth knowing

**This will break eventually.** It depends on the internal shape of a Snapchat response.
Nothing stops them changing it. If a run reports friends but no birthdays, or no friends
at all, the field numbers near the top of the script are the place to look.

**Only month and day.** Snapchat never exposes birth years, so events recur yearly with
no age. Friends who keep their birthday private are silently skipped — that's the
`137 keep theirs private` in the output above.

**Leap days** anchor to Feb 28. A yearly rule anchored on Feb 29 only fires every four
years in most calendar apps, which is not what you want.

**It runs headed, always.** Snapchat doesn't issue the friend sync in a headless browser,
so there'd be nothing to capture. A `--headless` flag would just hang.

**It's against Snapchat's terms of service.** Automating a logged-in session is not
something they permit, even when it's your own account reading your own friends'
birthdays. Realistically the risk is your account getting rate-limited or flagged, not a
lawyer — but it is your account and your call. Don't put it on a cron; run it when you
actually want your calendar updated.

**Those birthdays belong to other people.** Several hundred of them, none of whom were
asked. Keep the file to yourself — `~/.snap-birthdays/` is created readable only by you,
and the `.ics` is written the same way.

## Tests

```bash
git clone https://github.com/jcthewizard/snap-birthdays
cd snap-birthdays && uv sync --group dev && uv run pytest
```

93 tests, no network and no browser — the protobuf decoder runs against synthetic
payloads, the calendar writer against hand-built friends, delivery against all three
platforms, the browser launch against a stub, and the UI's API surface against a real
server on a random port.

## Credit

The protobuf decoding is adapted from
[Snap2Calendar-Birthday-Export](https://github.com/J4A-Industries/Snap2Calendar-Birthday-Export)
by James Arnott (MIT), which works out the field numbers and does the same job as a
browser extension. This started out also supporting Facebook via
[fb2cal](https://github.com/mobeigi/fb2cal), but Facebook's birthday page no longer
issues the GraphQL query that approach depends on, so that half was cut.

MIT licensed.
