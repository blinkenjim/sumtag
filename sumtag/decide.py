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
    if force:
        return True, "forced"
    if meta is None:
        return True, "no usable metadata"
    if schema.major_of(meta["version"]) < current_major:
        return True, "older major version"
    # Freshness is algorithm-agnostic (CLAUDE.md "Re-hashing logic"): a file
    # with *any* current digest counts as up to date, whichever algorithm
    # produced it. Only a genuinely empty map (no digest at all) re-hashes;
    # switching the active algorithm never does, so an archive stamped under
    # one algorithm is not re-read wholesale when the default changes.
    if not meta.get("digests"):
        return True, "no digest present"
    # ISO 8601 UTC strings of fixed width compare lexicographically as time does.
    if meta["file_mtime"] < live_mtime:
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
