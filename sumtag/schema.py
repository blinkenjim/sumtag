"""The xattr schema: identity, the digest algorithm, timestamps, (de)serialization.

This is sumtag's own authoritative copy. The conformance harness deliberately
keeps a *separate* copy so it can act as an independent oracle; the two must
agree by construction (same algorithm, same timestamp format), not by sharing
code. See CLAUDE.md "Extended attribute schema".
"""

from __future__ import annotations

import json
from datetime import datetime, timezone

from . import __version__

#: Extended-attribute name. The same literal string on Linux and macOS.
XATTR_NAME = "user.sumtag"

#: Software/schema version stamped into every xattr; mirrors pyproject.
VERSION = __version__

#: Digest algorithm key in the ``digests`` map and the DB ``algo`` column.
#: The single switch point for which algorithm sumtag computes -- see
#: ``hashing._HASHERS`` for the set of algorithms actually implemented.
#: A real ``--digest`` CLI selector is future work; today this is a
#: code-level constant, not a flag.
ALGO = "xxh3"


def major_of(version: str) -> int:
    """Return the semver major component of a version string."""
    return int(version.split(".", 1)[0])


# --- timestamps (ISO 8601 UTC, microsecond precision, 'Z' suffix) ---------


def iso_utc_ns(ns: int) -> str:
    """Format an epoch time in **nanoseconds** as a microsecond ISO UTC stamp.

    Works from integer nanoseconds to avoid float rounding at the microsecond
    boundary, so two equal ``st_mtime_ns`` values always format identically.
    """
    sec, nsec = divmod(ns, 1_000_000_000)
    micro = nsec // 1000  # truncate ns -> us, the stored precision
    dt = datetime.fromtimestamp(sec, tz=timezone.utc)
    return dt.strftime("%Y-%m-%dT%H:%M:%S.") + f"{micro:06d}Z"


def now_iso() -> str:
    """Return the current time as a microsecond ISO UTC stamp."""
    dt = datetime.now(timezone.utc)
    return dt.strftime("%Y-%m-%dT%H:%M:%S.") + f"{dt.microsecond:06d}Z"


# --- (de)serialization ----------------------------------------------------

_REQUIRED_KEYS = ("version", "digests", "file_mtime", "hashed_at", "run_started_at")


def build_meta(digests: dict[str, str], file_mtime: str, run_started_at: str) -> dict:
    """Assemble a fresh xattr document."""
    return {
        "version": VERSION,
        "digests": dict(digests),
        "file_mtime": file_mtime,
        "hashed_at": now_iso(),
        "run_started_at": run_started_at,
    }


def dumps(meta: dict) -> bytes:
    """Serialize a metadata document to the UTF-8 JSON bytes stored in the xattr."""
    return json.dumps(meta).encode("utf-8")


def loads(raw: bytes) -> dict:
    """Parse and minimally validate xattr bytes.

    Raises ``ValueError`` if the bytes are not the expected JSON object shape, so
    callers can treat a malformed xattr the same as an absent one.
    """
    doc = json.loads(raw.decode("utf-8"))
    if not isinstance(doc, dict) or any(k not in doc for k in _REQUIRED_KEYS):
        raise ValueError("xattr is not a sumtag metadata document")
    if not isinstance(doc["digests"], dict):
        raise ValueError("digests is not a map")
    return doc
