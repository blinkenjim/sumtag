"""Pure decision logic — no filesystem, no platform, just values in, verdict out.

Keeping these as free functions over plain data is what makes the trickiest
parts of sumtag (the re-hash table and the verify truth table from CLAUDE.md)
exhaustively unit-testable without touching a disk.
"""

from __future__ import annotations

from . import schema


def should_rehash(
    meta: dict | None,
    live_mtime: str,
    force: bool,
    current_major: int,
) -> tuple[bool, str]:
    """Decide whether a file must be (re-)hashed, and why.

    ``meta`` is the parsed xattr document or ``None`` (absent/unreadable/malformed).
    ``live_mtime`` is the file's current mtime as a microsecond ISO stamp.
    Mirrors CLAUDE.md "Re-hashing logic".
    """
    # CLAUDE.md "Re-hashing logic": re-hash when ANY of the four conditions
    # holds. --force is unconditional, so it is answered before the metadata
    # is even consulted.
    if force:
        return True, "forced"
    if meta is None:
        # Absent, unreadable, and malformed xattrs all reach here as None.
        return True, "no usable metadata"
    if schema.major_of(meta["version"]) < current_major:
        # Only a LOWER stored major triggers (semver major bump = re-hash);
        # equal or newer majors pass through to the freshness checks.
        return True, "older major version"
    if not meta.get("digests"):
        # Freshness is algorithm-agnostic: any stored digest, under any
        # algorithm, counts as current -- so only a genuinely empty map
        # forces a hash. Switching the active algorithm never re-hashes an
        # already-stamped archive by itself.
        return True, "no digest present"
    if meta["file_mtime"] < live_mtime:
        # Strictly "older than": equal microsecond stamps are up to date, and
        # a stored mtime NEWER than the live one is not staleness. Fixed-width
        # ISO 8601 UTC strings compare lexicographically as time does.
        return True, "file modified since last hash"
    return False, "up-to-date"


# Verify outcomes (CLAUDE.md "Verification" truth table).
INTACT = "intact"
CORRUPTION = "corruption"
STALE = "stale"
UNVERIFIABLE = "unverifiable"


def classify_verify(meta: dict | None, live_mtime: str, computed: dict[str, str]) -> str:
    """Classify a verify result for one file.

    ``computed`` maps each algorithm present in the xattr to the freshly
    recomputed digest. Returns one of INTACT / CORRUPTION / STALE / UNVERIFIABLE.

    The mtime gate is what separates corruption from a normal edit: a digest
    mismatch with an *unchanged* mtime is the alarm case; a mismatch with a
    *changed* mtime is merely a stale stamp.
    """
    # No usable record -> nothing to verify against: unverifiable, never
    # corruption (CLAUDE.md "Verification").
    if meta is None:
        return UNVERIFIABLE
    stored = meta.get("digests", {})
    if not stored:
        return UNVERIFIABLE

    # Intact requires EVERY stored algorithm's digest to match its fresh
    # recomputation -- generic iteration over whatever the map holds. A full
    # match is intact regardless of mtime (touched but content identical).
    if all(computed.get(algo) == digest for algo, digest in stored.items()):
        return INTACT

    # Some digest disagrees: the mtime gate decides which row of the truth
    # table this is. "Same" vs "changed" is pure equality -- any difference,
    # in either direction, means the modification left a trace (stale); only
    # an UNCHANGED mtime makes the mismatch silent corruption.
    return CORRUPTION if meta["file_mtime"] == live_mtime else STALE


def match_moved_dirs(lost: dict, found: dict) -> tuple[dict, set]:
    """Match vanished directories to candidate new locations (--prune-dirs
    move detection; design in TODO.md, decided 2026-08-05).

    ``lost`` maps a lost directory's key to its recorded residents,
    ``{basename: (inode, file_mtime)}``; ``found`` maps a candidate
    directory's key to its live regular-file children in the same shape.
    Returns ``(matches, ambiguous)``: ``matches[lost_key] = found_key`` for
    every uniquely resolved move, and ``ambiguous`` holds the lost keys
    whose evidence could not name a single answer.

    The rule: L matches F iff EVERY recorded resident of L has a child of F
    agreeing on basename, inode, AND the exact recorded microsecond mtime
    -- the zero-content-I/O trust-veto idiom. Inode alone is never trusted
    (numbers recycle); the mtime agreement makes reuse collisions
    essentially impossible. The subset direction (residents <= children)
    tolerates files added after the move; anything deleted, renamed, or
    modified since breaks the match and the directory falls back to
    prune-plus-rescan -- the safe direction. A rowless lost directory
    matches nothing (it would match everything vacuously).

    Ambiguity -- one lost directory matching several candidates, or several
    lost directories claiming one candidate (possible only via hard-link
    farms, where links share inode and mtime) -- is refused, never guessed:
    those lost keys land in ``ambiguous`` and in no match.
    """
    # Each lost dir's candidate set, by full-agreement subset test.
    claims: dict = {}
    for lkey, residents in lost.items():
        if not residents:
            continue
        claims[lkey] = [
            fkey for fkey, children in found.items()
            if all(children.get(name) == sig for name, sig in residents.items())
        ]

    ambiguous = {lkey for lkey, fkeys in claims.items() if len(fkeys) > 1}
    tentative = {lkey: fkeys[0] for lkey, fkeys in claims.items()
                 if len(fkeys) == 1}

    # A candidate claimed by more than one tentative match settles nothing
    # for any of its claimants.
    counts: dict = {}
    for fkey in tentative.values():
        counts[fkey] = counts.get(fkey, 0) + 1
    matches = {}
    for lkey, fkey in tentative.items():
        if counts[fkey] == 1:
            matches[lkey] = fkey
        else:
            ambiguous.add(lkey)
    return matches, ambiguous
