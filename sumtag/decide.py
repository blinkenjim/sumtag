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
    if meta is None:
        return UNVERIFIABLE
    stored = meta.get("digests", {})
    if not stored:
        return UNVERIFIABLE

    all_match = all(stored[a] == computed.get(a) for a in stored)
    if all_match:
        return INTACT  # contents identical, regardless of mtime
    mtime_same = meta["file_mtime"] == live_mtime
    return CORRUPTION if mtime_same else STALE
