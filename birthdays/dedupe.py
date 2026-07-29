#!/usr/bin/env python3
"""
    birthdays - cross-source friend birthday sync
    dedupe.py - match and merge the same human across sources.

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

import hashlib
import re
import unicodedata
from dataclasses import replace
from difflib import SequenceMatcher
from typing import Iterable, NamedTuple, Sequence

from .models import MergedPerson, Person

__all__ = [
    "merge",
    "normalize_name",
    "name_tokens",
    "looks_like_handle",
    "make_uid",
    "identity_key",
    "member_key",
    "UidLedger",
    "FUZZY_RATIO",
    "STRICT_FUZZY_RATIO",
    "MIN_SQUASHED_MATCH_LEN",
    "SOURCE_PRIORITY",
]

# --------------------------------------------------------------------------------------
# Tunables
# --------------------------------------------------------------------------------------

#: difflib ratio required to call two normal (human looking) names the same person.
FUZZY_RATIO = 0.90

#: difflib ratio required when at least one side is only a handle ("jdoe_23").
#: Deliberately higher: a false split is cheap (two calendar events), a false merge
#: silently loses a friend's birthday forever.
STRICT_FUZZY_RATIO = 0.95

#: Minimum length of the space-stripped form before a handle may match anything at all.
#: A handle is usually just a full name with the spaces squeezed out and some digits
#: bolted on ("janedoe22"), which normalisation already reduces to "janedoe" -- close to
#: "jane doe" but not close enough for STRICT_FUZZY_RATIO to catch. Short squashed forms
#: collide far too easily ("al" vs "a l", and every "mike*" handle in the friend list
#: normalises to plain "mike") so they are held back by this floor -- including on the
#: exact-equality path, which is precisely where digit-stripped handles collide.
MIN_SQUASHED_MATCH_LEN = 6

#: Lower number == more trustworthy display name. Used only as a tie-break.
SOURCE_PRIORITY: dict[str, int] = {"facebook": 0, "snapchat": 1}

_HANDLE_CHARS = re.compile(r"[0-9_]")

#: Suffix on every generated UID. Kept as a constant because uniqueness salting has to
#: splice around it (see :func:`_disambiguate_uids`).
_UID_HOST = "@birthdays.local"


# --------------------------------------------------------------------------------------
# Name normalisation
# --------------------------------------------------------------------------------------


def normalize_name(name: str | None) -> str:
    """Fold a display name down to lowercase ASCII-ish letters and single spaces.

    Steps: NFKD unicode normalisation -> drop combining marks (diacritics) ->
    lowercase -> everything that is not a letter becomes a separator -> collapse
    whitespace.

    Note we *replace* non-letters with a space rather than deleting them, so that
    ``"jane_doe"`` -> ``"jane doe"`` and ``"Anne-Marie"`` -> ``"anne marie"``.  The
    output still contains nothing but letters and single spaces (as required), but
    token boundaries that punctuation was carrying are preserved instead of silently
    gluing two names together ("janedoe").

        >>> normalize_name("José  \U0001f60e García-López!")
        'jose garcia lopez'
    """
    if not name:
        return ""
    decomposed = unicodedata.normalize("NFKD", name)
    chars: list[str] = []
    for ch in decomposed:
        if unicodedata.combining(ch):
            continue  # accent / diacritic left over from NFKD
        chars.append(ch if ch.isalpha() else " ")
    return " ".join("".join(chars).lower().split())


def name_tokens(name: str | None) -> list[str]:
    """Normalized name split into its whitespace separated tokens."""
    return normalize_name(name).split()


def looks_like_handle(raw: str | None) -> bool:
    """True if ``raw`` looks like a username rather than a human's name.

    Handles look like ``jdoe_23`` / ``sk8rboi`` / ``mmiller``: they carry digits or
    underscores, or they are a single blob with no space in it.  Empty names count as
    handles (i.e. untrustworthy) so they can never trigger a fuzzy merge.
    """
    s = (raw or "").strip()
    if not s:
        return True
    if _HANDLE_CHARS.search(s):
        return True
    return " " not in s


def identity_key(name: str) -> str:
    """The part of a name that survives learning more of it.

    Keyed on ``{first token, last token}`` as an unordered pair rather than the whole
    name, because the whole name is *not* stable: the day a friend gains a second source
    we may learn a middle name ("Jane Doe" -> "Jane Marie Doe") or a reordering
    ("Doe Jane"), and every one of those spellings has to hash to the same thing.  Both
    of those transformations leave the first/last pair untouched.

    It is *not* invariant under every merge rule, though, and it never can be: a merge
    justified by a strict token subset ("Jane Doe" + "Jane Doe Smith"), by a reordering
    of a three token name, or by a squashed handle ("janedoe22" + "Jane Doe") changes the
    first/last pair, and so changes this key.  Two groups that stayed apart can also share
    a key (a handle-only record never reaches the first/last merge rule at all).  UID
    stability across runs therefore cannot rest on this function alone -- it rests on
    :class:`UidLedger`, which pins the UID a person was already exported with.  This
    function only decides the *first* UID a person is ever given.
    """
    tokens = name_tokens(name)
    if not tokens:
        return ""
    return " ".join(sorted({tokens[0], tokens[-1]}))


def make_uid(month: int, day: int, canonical_name: str) -> str:
    """Deterministic ICS UID for a merged person.

    The UID is derived from (month, day, identity key) and *deliberately not* from any
    source id.  Rationale: a friend who exists only on Snapchat today may gain a Facebook
    record tomorrow (or be removed from one source entirely).  If the UID were built from
    source ids it would change the moment the group's membership changed, and every
    calendar client would treat the result as a brand new event -> duplicates.  Keying on
    the human facts instead keeps the UID stable across the transformations
    :func:`identity_key` absorbs.

    This is only the *initial* UID for a person: once an event has been exported, its UID
    is pinned by :class:`UidLedger` and this function is not consulted again for them.

    ``canonical_name`` must already be an :func:`identity_key`, not a display name --
    see that function for why the display name alone is too volatile to hash.
    """
    key = f"{month:02d}-{day:02d}|{canonical_name}"
    digest = hashlib.sha1(key.encode("utf-8")).hexdigest()[:20]
    return f"bd-{digest}{_UID_HOST}"


def member_key(person: Person) -> str:
    """Ledger key for one source record: ``"facebook:100"`` / ``"snapchat:janedoe"``.

    Stable for as long as the source keeps the record: Facebook's numeric id and
    Snapchat's username are exactly what each source uses as ``source_id``.
    """
    return f"{person.source}:{person.source_id}"


class UidLedger:
    """Remembers which UID each source record was already exported under.

    Why this exists: a UID computed purely from *today's* data cannot be stable, because
    merging changes the data it is computed from.  The day a friend gains a second source
    we may learn a middle name, a reordering, or that a bare handle ("janedoe22") belongs
    to "Jane Doe" -- and every one of those merges rewrites the group's display name, and
    with it any name-derived key.  Two groups that stayed apart can also collide on the
    same derived key and need salting, and *which* of them gets salted depends on what
    else happened to be in that run.

    A calendar client keys on the UID alone: if it moves, the client inserts a second
    event instead of updating the first, which is the one thing this tool exists to
    prevent.  So the assignment is recorded here (and persisted by
    :meth:`birthdays.store.Store.save_uid_map`) instead of being re-derived every run:
    once a person has an event, they keep its UID no matter how their name is spelled or
    which sources they turn up on next.
    """

    __slots__ = ("_by_member", "dirty")

    def __init__(self, mapping: dict[str, str] | None = None) -> None:
        self._by_member: dict[str, str] = {
            str(key): str(uid)
            for key, uid in (mapping or {}).items()
            if str(key).strip() and str(uid).strip()
        }
        #: True once :meth:`remember` learned something worth persisting.
        self.dirty = False

    def __len__(self) -> int:
        return len(self._by_member)

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"UidLedger({len(self._by_member)} records, dirty={self.dirty})"

    def as_dict(self) -> dict[str, str]:
        """The mapping to persist, sorted so the file has no spurious diffs."""
        return dict(sorted(self._by_member.items()))

    def lookup(self, members: Iterable[Person]) -> str | None:
        """UID this group was exported under before, or None if it is new.

        When a group's members carry *different* remembered UIDs (two events that this
        run decided are one person) the best-supported one wins, ties broken
        lexicographically so the choice does not depend on member order.  The losing
        event is orphaned in the calendar -- unavoidable, since an ICS import cannot
        delete -- but keeping the majority means the fewest events go stale.
        """
        votes: dict[str, int] = {}
        for person in members:
            uid = self._by_member.get(member_key(person))
            if uid:
                votes[uid] = votes.get(uid, 0) + 1
        if not votes:
            return None
        return min(votes, key=lambda uid: (-votes[uid], uid))

    def remember(self, members: Iterable[Person], uid: str) -> None:
        """Record that every member of this group is exported as ``uid``."""
        if not uid:
            return
        for person in members:
            key = member_key(person)
            if self._by_member.get(key) != uid:
                self._by_member[key] = uid
                self.dirty = True


# --------------------------------------------------------------------------------------
# Matching
# --------------------------------------------------------------------------------------


class _Variant(NamedTuple):
    """One comparable spelling of a record's name, with everything precomputed.

    ``digits`` is kept because :func:`normalize_name` deliberately throws digits away,
    and for a *handle* the digits are frequently the only thing that distinguishes two
    different friends ("mike_23" and "mike99" both normalise to bare "mike").
    """

    norm: str
    tokens: frozenset[str]
    squashed: str
    is_handle: bool
    digits: str


def _digits_of(raw: str) -> str:
    return "".join(ch for ch in raw if ch.isdigit())


def _make_variant(raw: str) -> _Variant:
    norm = normalize_name(raw)
    return _Variant(
        norm=norm,
        tokens=frozenset(norm.split()),
        squashed=norm.replace(" ", ""),
        is_handle=looks_like_handle(raw),
        digits=_digits_of(raw),
    )


class _Candidate:
    """A Person plus its precomputed name variants (normalisation is not free)."""

    __slots__ = ("person", "index", "variants")

    def __init__(self, person: Person, index: int) -> None:
        self.person = person
        self.index = index

        # A Snapchat record may carry a real display name *and* a handle; try both.
        # Each variant remembers whether *it* reads like a handle, because that is a
        # property of the spelling and not of the record: "Mike Adams" deserves the
        # normal rules even when the same record also offers the handle "mike_23".
        raw_variants: list[str] = []
        for raw in (person.name, person.username):
            if raw and raw.strip() and raw not in raw_variants:
                raw_variants.append(raw)

        seen: set[str] = set()
        variants: list[_Variant] = []
        for raw in raw_variants:
            variant = _make_variant(raw)
            if not variant.norm or variant.norm in seen:
                continue
            seen.add(variant.norm)
            variants.append(variant)
        self.variants: list[_Variant] = variants

    @property
    def canonical(self) -> str:
        """Best normalized name for this record ('' if the name was pure emoji)."""
        return self.variants[0].norm if self.variants else ""


def _handle_match(a: _Variant, b: _Variant) -> bool:
    """Matching rules when at least one side is a handle rather than a human name.

    Handles reach us stripped of everything that made them unique: normalisation turns
    punctuation and digits into separators, so "mike_23" and "mike99" -- two different
    friends who happen to share a birthday -- both arrive as the single token "mike".
    Merging those two silently deletes one friend's birthday, which is the expensive
    direction of this trade-off, so a handle has to clear two floors before any of the
    string comparisons below is even consulted.
    """
    # (i) Digits are evidence, and normalisation destroyed them. If both sides carry
    #     digits and they disagree, these are two different handles no matter how alike
    #     their letters are ("jane_doe22" vs "jane_doe99").
    if a.digits and b.digits and a.digits != b.digits:
        return False

    # (ii) A short squashed form is not evidence of anything: "mike", "sarah" and "al"
    #      are shared by half a friend list. This floor applies to exact equality too --
    #      that is exactly where digit-stripped handles collide.
    if min(len(a.squashed), len(b.squashed)) < MIN_SQUASHED_MATCH_LEN:
        return False

    if a.norm == b.norm:
        return True
    if SequenceMatcher(None, a.norm, b.norm).ratio() >= STRICT_FUZZY_RATIO:
        return True
    # A handle is often a full name with the spaces squeezed out (normalisation has
    # already dropped the trailing digits), so "janedoe22" and "Jane Doe" both reduce to
    # "janedoe". Safe -- the birthday already had to match exactly to get here.
    return a.squashed == b.squashed


def _names_match(a: _Variant, b: _Variant) -> bool:
    """Apply the matching rules to two already-normalized name variants."""
    if not a.norm or not b.norm:
        return False

    # A handle on either side means the strict rules, and *only* the strict rules:
    # falling through to the generous ones would undo the floors they enforce.
    if a.is_handle or b.is_handle:
        return _handle_match(a, b)

    # (a) identical normalized name -- the strongest signal we have.
    if a.norm == b.norm:
        return True

    # (b) same tokens, any order: "jane doe" == "doe jane"
    if a.tokens == b.tokens:
        return True

    la, lb = a.norm.split(), b.norm.split()

    # (c) same first *and* last token: "jane doe" == "jane marie doe".
    #     Both must agree on both ends, so a shared first name alone can never merge.
    if la[0] == lb[0] and la[-1] == lb[-1]:
        return True

    # (d) strict subset sharing >= 2 tokens: "jane doe" vs "jane marie doe".
    shared = a.tokens & b.tokens
    if len(shared) >= 2 and (a.tokens < b.tokens or b.tokens < a.tokens):
        return True

    # (e) fuzzy string similarity as the last resort.
    return SequenceMatcher(None, a.norm, b.norm).ratio() >= FUZZY_RATIO


def _same_person(a: _Candidate, b: _Candidate) -> bool:
    """Would we call these two records the same human?

    Callers must have already bucketed by (month, day); we assert that here rather than
    re-check, because birthday equality is a precondition and *never* on its own enough.
    """
    for va in a.variants:
        for vb in b.variants:
            if _names_match(va, vb):
                return True
    return False


class _DisjointSet:
    """Tiny union-find; merging is transitive (A~B and B~C => one group)."""

    def __init__(self, n: int) -> None:
        self._parent = list(range(n))

    def find(self, x: int) -> int:
        parent = self._parent
        root = x
        while parent[root] != root:
            root = parent[root]
        while parent[x] != root:  # path compression
            parent[x], x = root, parent[x]
        return root

    def union(self, a: int, b: int) -> None:
        ra, rb = self.find(a), self.find(b)
        if ra != rb:
            self._parent[max(ra, rb)] = min(ra, rb)  # keep the earliest index as root


# --------------------------------------------------------------------------------------
# Merging
# --------------------------------------------------------------------------------------


def _display_name(person: Person) -> str:
    """Best human-readable string a record can offer, falling back to its handle.

    Snapchat friends can arrive with an empty display name, in which case the handle is
    all we have and is still better than showing the user a blank calendar entry.
    """
    return (person.name or "").strip() or (person.username or "").strip()


def _pick_name(members: list[Person]) -> str:
    """Prefer the longest name containing a space; tie-break facebook > snapchat."""

    def key(p: Person) -> tuple[int, int, int, str, str]:
        n = _display_name(p)
        return (
            0 if " " in n else 1,  # full names first
            -len(n),  # then longest
            SOURCE_PRIORITY.get(p.source, 99),  # then facebook over snapchat
            p.source,  # then deterministic
            p.source_id,
        )

    return _display_name(min(members, key=key))


def _build_merged(members: list[Person]) -> MergedPerson:
    name = _pick_name(members)
    month, day = members[0].month, members[0].day

    year: int | None = None
    for p in members:  # only Facebook ever supplies a birth year
        if p.year is not None:
            year = p.year
            break

    # Canonical key for the UID.  Fall back through username -> raw name so that a
    # friend whose display name is pure emoji still gets a distinguishable UID instead
    # of colliding with every other emoji-named friend born the same day.
    canonical = identity_key(name)
    if not canonical:
        for p in members:
            canonical = identity_key(p.username or "") or identity_key(p.name or "")
            if canonical:
                break
    if not canonical:
        canonical = "|".join(sorted(member_key(p) for p in members))

    return MergedPerson(
        uid=make_uid(month, day, canonical),
        name=name,
        month=month,
        day=day,
        year=year,
        sources=sorted({p.source for p in members}),
        members=list(members),
    )


def merge(people: list[Person], ledger: UidLedger | None = None) -> list[MergedPerson]:
    """Collapse duplicate humans across sources into stable :class:`MergedPerson` rows.

    Two records are only ever considered for merging when their (month, day) are
    identical -- the birthday alone is never enough, and neither is a shared first name.
    Because candidate pairs are only compared inside a (month, day) bucket the cost is
    O(n * bucket^2) rather than O(n^2), and the union-find below makes matching
    transitive within a bucket.

    ``ledger`` is what makes UIDs survive *between* runs: a group whose members were
    exported before keeps the UID they were exported under, whatever their names have
    since become and whichever sources they now appear on.  Without it (tests, one-shot
    calls) UIDs are derived from the data alone, which is stable only for as long as the
    data is -- see :class:`UidLedger`.  The ledger is updated in place; persisting it is
    the caller's job.

    The result is sorted by (month, day, name).
    """
    buckets: dict[tuple[int, int], list[_Candidate]] = {}
    for i, person in enumerate(people):
        buckets.setdefault((person.month, person.day), []).append(_Candidate(person, i))

    # Each row is (person, uid-was-remembered-not-derived).
    rows: list[tuple[MergedPerson, bool]] = []
    for (_month, _day), bucket in buckets.items():
        dsu = _DisjointSet(len(bucket))
        for i in range(len(bucket)):
            for j in range(i + 1, len(bucket)):
                if dsu.find(i) == dsu.find(j):
                    continue  # already transitively linked
                if _same_person(bucket[i], bucket[j]):
                    dsu.union(i, j)

        groups: dict[int, list[Person]] = {}
        for i, cand in enumerate(bucket):
            groups.setdefault(dsu.find(i), []).append(cand.person)

        for _root, members in groups.items():
            person = _build_merged(members)
            remembered = ledger.lookup(members) if ledger is not None else None
            if remembered:
                person = replace(person, uid=remembered)
            rows.append((person, remembered is not None))

    rows.sort(key=lambda row: (row[0].month, row[0].day, row[0].name.casefold(), row[0].uid))
    merged = [person for person, _ in rows]

    out = _disambiguate_uids(merged, [is_pinned for _, is_pinned in rows])
    if ledger is not None:
        for person in out:
            ledger.remember(person.members, person.uid)
    return out


def _uid_salt(person: MergedPerson, bump: int) -> str:
    """Deterministic salt for a UID collision, derived from *who is in the group*.

    Not from the group's position in this run's list: that would make a UID depend on
    which other people happen to be in the calendar, so unfriending somebody else (or
    merely a name change that reorders the two) would move this person's UID and produce
    the duplicate the salting exists to avoid.
    """
    keys = "|".join(sorted(member_key(m) for m in person.members))
    return hashlib.sha1(f"{bump}|{keys}".encode("utf-8")).hexdigest()[:6]


def _disambiguate_uids(
    merged: list[MergedPerson], pinned: Sequence[bool] | None = None
) -> list[MergedPerson]:
    """Guarantee UIDs are unique across the whole calendar.

    Collisions between groups that stayed apart are real (a handle-only record never
    reaches the first/last-token merge rule, so "jane_doe" and "Jane Marie Doe" can share
    an identity key without merging), and a duplicate UID is silent data loss: calendar
    clients collapse same-UID events, so one friend just vanishes.

    ``pinned[i]`` marks a row whose UID came from the ledger, i.e. an event that already
    exists in the user's calendar.  Those claim their UID first and never move; only the
    newcomer is salted, which keeps the incumbent's event an update rather than an insert.
    """
    flags = list(pinned) if pinned is not None else [False] * len(merged)
    # Pinned rows claim first, then everything else in list order.
    order = sorted(range(len(merged)), key=lambda i: (not flags[i], i))

    used: set[str] = set()
    assigned: dict[int, str] = {}
    for i in order:
        person = merged[i]
        uid = person.uid
        bump = 0
        while uid in used:
            bump += 1
            uid = f"{person.uid[: -len(_UID_HOST)]}-{_uid_salt(person, bump)}{_UID_HOST}"
        used.add(uid)
        assigned[i] = uid

    return [
        person if assigned[i] == person.uid else replace(person, uid=assigned[i])
        for i, person in enumerate(merged)
    ]
