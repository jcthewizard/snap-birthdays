# birthdays

Pull your friends' birthdays out of **Facebook** and **Snapchat**, merge the two lists
into one de-duplicated set of people, and write a single `.ics` calendar you can import
anywhere. Re-running never creates duplicate calendar events.

```
$ birthdays connect facebook      # a real browser window opens - log in by hand
$ birthdays connect snapchat
$ birthdays sync                  # fetch -> merge -> ~/.birthdays/birthdays.ics -> Calendar.app

source          records
------------  ---------
facebook            412
snapchat            118
------------  ---------
total raw           530
after merge         491
cross-source         39  (people found on more than one source)

Wrote 491 events to /Users/you/.birthdays/birthdays.ics
```

## How it works

There is no Facebook or Snapchat API for friend birthdays, so this tool drives a **real,
visible browser** (Playwright, persistent profile) and reads the same responses the web
apps read:

* **Facebook** - opens `facebook.com/events/birthdays/`, watches the page issue its own
  GraphQL birthday query, steals the `doc_id` + `fb_dtsg` form template, and replays it
  for offset months 0/3/6/9 (each request returns three months, so that covers a year).
* **Snapchat** - opens `web.snapchat.com`, listens for the gRPC-web `SyncFriendData`
  response the app fires on load, and decodes the protobuf friend records out of it.

You log in **by hand, once, in a normal browser window**. Nothing scripts your password,
so 2FA, captcha and login checkpoints all just work. The session lives in a persistent
Chromium profile under `~/.birthdays/profiles/<source>/` and is reused on later runs.

## Honest caveats - please read

* **This violates Facebook's and Snapchat's Terms of Service.** Automated collection of
  data from their sites is not permitted. Using this tool is at your own risk, and could
  in principle get your account rate-limited, checkpointed or banned. Only run it against
  your own account, and don't hammer it.
* **It will break.** Both sites change their internals - endpoint names, GraphQL document
  ids, protobuf field numbers, DOM selectors - without notice. The code is written to be
  as shape-agnostic as possible (Facebook's `doc_id` is learned at runtime, the JSON is
  walked rather than indexed, the Snapchat protobuf is scanned rather than compiled), but
  it is scraping, and scraping rots.
* **Snapchat never gives you a birth year**, and many friends don't share a birthday at
  all. Facebook gives a year only for friends who made it visible to you.
* This is a personal-use tool. Don't publish or resell the data it collects; those are
  other people's birthdays.

## Install

Requires Python 3.11+.

With [uv](https://docs.astral.sh/uv/) (recommended):

```bash
git clone https://github.com/jcthewizard/birthdays.git
cd birthdays
uv venv
uv pip install -e .
uv run playwright install chromium
uv run birthdays --help
```

With pip:

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -e .
playwright install chromium
birthdays --help
```

`playwright install chromium` downloads the browser Playwright drives. If you already
have Google Chrome installed, the tool prefers it (it looks and behaves more like a
normal browser, which matters when logging in) and falls back to the bundled Chromium.

## Usage

```
birthdays [--home PATH] <command>
```

`--home` relocates the state directory (default `~/.birthdays`); the `BIRTHDAYS_HOME`
environment variable does the same.

### `birthdays connect <facebook|snapchat|all>`

Opens a browser window, waits for you to log in, fetches that source's birthdays and
caches them. Prints `Found N birthdays from <source>`.

```
--headless          run without a window (only works once a session exists)
--timeout SECONDS   budget for login + fetch (default 300)
```

With `all`, a source that fails is reported and the other one still runs; the command
only fails hard if every source failed.

### `birthdays sync`

Fetches every connected source, merges, writes the ICS and hands it to your calendar app.

```
--source NAME       only this source (repeatable); default = every connected source
--no-import         write the file but don't open Calendar
--out PATH          where to write the .ics (default ~/.birthdays/birthdays.ics)
--calname NAME      calendar name embedded in the file (default "Friend Birthdays")
--headless          no window
--timeout SECONDS   per-source budget
```

If one source fails, `sync` prints the error, falls back to that source's cached records
so its birthdays don't disappear from your calendar, and carries on with the others.

### `birthdays export`

Exactly like `sync`, but uses the **cached** data from previous fetches - no browser, no
network. Same `--out` / `--no-import` / `--calname` / `--source` options.

### `birthdays list`

Prints the merged birthdays, sorted by date, one per line:

```
Mar 14  Jane Doe  [facebook, snapchat]
Mar 22  Ravi Patel  [facebook]
Apr  2  lucy_b  [snapchat]
```

### `birthdays status`

Which sources are connected, how many records are cached for each, when they were last
fetched, and where the ICS lives.

## Why re-running doesn't duplicate events

Two mechanisms, and you need both:

1. **Stable UIDs.** A new person's `UID` is `sha1(month-day|first and last name token)` -
   derived only from the human facts, never from a Facebook/Snapchat id or from the run
   time. An ICS import is an *upsert* keyed on UID, so importing the file a second time
   updates the existing events in place instead of adding a second copy of every birthday.

   From then on the assignment is **remembered** in `~/.birthdays/uids.json`, keyed by
   `<source>:<source_id>`, and reused verbatim. That file is what makes the guarantee
   hold when the data itself changes: connecting a second source can teach us a middle
   name, a different word order, or that the handle `janedoe22` is "Jane Doe", and every
   one of those rewrites the name the UID would otherwise be derived from. Delete
   `uids.json` and the next sync may re-create your birthday events instead of updating
   them.
2. **Cross-source merge.** Before writing, records are bucketed by `(month, day)` - only
   people who share a birthday are ever compared - and then matched on name: identical
   normalized names, the same first *and* last token ("Jane Doe" ≈ "Jane Marie Doe"),
   reordered tokens, or a high fuzzy-similarity score. Matching is transitive within a
   bucket, so a Facebook record and a Snapchat record for the same human collapse into one
   event listing both sources in its description.

   The matcher deliberately errs toward **splitting**: a shared first name alone never
   merges, and when one side is only a handle (`jdoe_23`, `sk8rboi`) it has to clear a much
   higher bar. A false split costs you a duplicate-looking calendar entry you can spot; a
   false merge silently deletes someone's birthday forever.

The `.ics` itself is hand-rolled RFC 5545 (CRLF, 75-octet folding that never splits a
UTF-8 character, escaped TEXT values) rather than the unmaintained `ics` package, so the
output is byte-stable between runs when the data hasn't changed.

## Where things live

```
~/.birthdays/
  profiles/<source>/     persistent browser profile (your login session)
  data/<source>.json     cached Person records + last fetch timestamp
  uids.json              which UID each friend's calendar event was exported under
  raw/                   raw API responses, kept for debugging
  birthdays.ics          the generated calendar
```

## Troubleshooting

**"No Facebook birthdays found" / "No Snapchat birthdays were captured"**

Look in `~/.birthdays/raw/`. Every response the tool managed to read is dumped there
(`facebook-graphql-1.json`, `snapchat-syncfrienddata-1.bin`, ...), and which files exist
tells you where it broke:

* *No files at all* - nothing was captured. Usually the login didn't finish before the
  timeout, or the window was closed early. Re-run `birthdays connect <source>`, leave the
  window open, and wait for the terminal to say it captured something. Try a longer
  `--timeout`.
* *Files exist but are tiny / contain `"errors"`* - the session expired or the request
  shape changed. Delete `~/.birthdays/profiles/<source>/` and connect again.
* *Files look full of real data but 0 birthdays were parsed* - the site changed its
  response format and the parser needs updating. Those dumps are exactly what a bug report
  needs (scrub them first - they contain your friends' names).

**The browser window opens and immediately closes.** Run without `--headless`, and make
sure `playwright install chromium` finished successfully.

**Calendar.app didn't open.** Use `--no-import` and import `~/.birthdays/birthdays.ics`
by hand. On non-macOS the tool prints per-client import instructions instead of trying.

**I want to start over.** `rm -rf ~/.birthdays` removes every session, cache and dump.
Events already imported into your calendar app stay there.

## Attribution

This project is derived from prior work and would not exist without it:

* **[fb2cal](https://github.com/mobeigi/fb2cal)** by Mohammad Beigi et al. - GPL-3.0.
  Source of the Facebook birthday approach: the GraphQL birthday endpoint, the
  `BirthdayCometMonthlyBirthdaysRefetchQuery` friendly name, the `offset_month` variable
  and its three-months-per-request behaviour, the `for (;;);` anti-hijacking prefix, and
  the `c_user` cookie authentication check.
* **[Snap2Calendar-Birthday-Export](https://github.com/J4A-Industries/Snap2Calendar-Birthday-Export)**
  by James Arnott - MIT, Copyright (c) 2023. Source of the Snapchat approach: the
  `com.snapchat.atlas.gw.AtlasGw/SyncFriendData` endpoint, the gRPC-web framing, and the
  protobuf field layout for friend records and birthdays.

Neither project is affiliated with this one, and neither is affiliated with Facebook/Meta
or Snap Inc.

## License

GPL-3.0-or-later. Because this project derives from fb2cal (GPL-3.0), it must be GPL-3.0
too. See [LICENSE](LICENSE).
