"""Independent copy of sumtag's xattr schema constants and helpers.

This module intentionally re-implements the small pieces of the schema the
harness needs (the attribute name, the digest algorithm, the timestamp format,
the hash function) rather than importing them from :mod:`sumtag`. The harness is
an *oracle*: if it reused sumtag's code, a bug shared by writer and reader could
pass undetected. Everything here mirrors the spec in CLAUDE.md by hand.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import xxhash

# --- xattr identity -------------------------------------------------------

#: The extended-attribute name sumtag writes (CLAUDE.md "Extended attribute
#: schema"). The same literal string is used on both Linux and macOS.
XATTR_NAME = "user.sumtag"

#: Software/schema version sumtag stamps today. Mirrors ``sumtag.__version__``
#: and pyproject; kept here independently on purpose.
CURRENT_VERSION = "0.1.0"


# --- digest algorithm -----------------------------------------------------
#
# sumtag uses XXH3 in its **64-bit** variant (settled 2026-06-13; see CLAUDE.md).
# This is the single place that choice lives for the whole harness; a 64-bit
# digest is 16 lowercase-hex characters.

#: Algorithm key as it appears in the xattr ``digests`` map and the DB ``algo``
#: column.
ALGO = "xxh3"


def compute_digest(data: bytes) -> str:
    """Return the lowercase-hex XXH3-64 digest of ``data`` (the oracle's own hash)."""
    return xxhash.xxh3_64_hexdigest(data)


def corrupt_digest(digest: str) -> str:
    """Return a different-but-same-width digest, for 'wrong-digest' prestamps.

    Flipping the low bit guarantees a value that is definitely not equal to the
    input yet is still a valid hex string of identical length — so it round-trips
    through the schema exactly like a real digest would.
    """
    flipped = int(digest, 16) ^ 1
    return format(flipped, "x").zfill(len(digest))


# --- timestamps -----------------------------------------------------------
#
# CLAUDE.md "Timestamp precision": ISO 8601 UTC with exactly microsecond
# precision (6 decimal places) and a 'Z' suffix, e.g. 2026-06-03T10:00:00.123456Z.


def iso_utc_ns(ns: int) -> str:
    """Format an epoch time given in **nanoseconds** as a microsecond ISO stamp.

    Works from integer nanoseconds (``st_mtime_ns``) rather than a float second
    count to avoid float rounding at the microsecond boundary — the comparison
    sumtag makes is only meaningful if both sides truncate identically.
    """
    sec, nsec = divmod(ns, 1_000_000_000)
    micro = nsec // 1000  # truncate ns -> us, matching the stored precision
    dt = datetime.fromtimestamp(sec, tz=timezone.utc)
    return dt.strftime("%Y-%m-%dT%H:%M:%S.") + f"{micro:06d}Z"


def file_mtime_iso(path: Path) -> str:
    """Return ``path``'s current mtime as a microsecond ISO UTC stamp."""
    return iso_utc_ns(path.stat().st_mtime_ns)
