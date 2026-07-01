"""Oracle: read back the true on-disk state after sumtag has run, independently.

Everything here reads through the harness's own machinery, never sumtag's: the
xattr bytes come from :mod:`xattrio`, the JSON is parsed with the stdlib, and the
digest is recomputed with :mod:`schema`'s own hash. That independence is what
lets the oracle *catch* a bug in sumtag rather than mirror it.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path

from . import schema, xattrio


@dataclass
class FileState:
    """The observed state of one file: its xattr (if any) plus oracle facts."""

    path: Path

    # --- as recorded in the xattr (None / empty when absent or unparseable) ---
    present: bool = False              # a usable, schema-shaped xattr exists
    raw: bytes | None = None          # the raw xattr bytes, if any
    parse_error: str | None = None    # set when raw bytes exist but don't parse
    version: str | None = None
    digests: dict[str, str] = field(default_factory=dict)
    file_mtime: str | None = None
    hashed_at: str | None = None
    run_started_at: str | None = None

    # --- computed independently from the file's current contents ---
    actual_digest: str = ""           # oracle's own XXH3 of the live bytes
    live_mtime: str = ""              # the file's current mtime, ISO microseconds

    # -- convenience predicates the scenarios lean on --------------------------

    @property
    def stored_digest(self) -> str | None:
        """The digest the xattr records for the active algorithm, if present."""
        return self.digests.get(schema.ALGO)

    @property
    def digest_matches_content(self) -> bool:
        """True iff the stored digest equals the live file's actual digest."""
        return self.stored_digest == self.actual_digest

    @property
    def mtime_matches(self) -> bool:
        """True iff the xattr's file_mtime equals the file's current mtime."""
        return self.file_mtime == self.live_mtime


def inspect(path) -> FileState:
    """Read and interpret the sumtag xattr on ``path``, plus oracle-computed facts."""
    path = Path(path)
    st = FileState(path=path)

    data = path.read_bytes()
    st.actual_digest = schema.compute_digest(data)
    st.live_mtime = schema.file_mtime_iso(path)

    raw = xattrio.get(path, schema.XATTR_NAME)
    st.raw = raw
    if raw is None:
        return st  # no xattr: present stays False

    try:
        doc = json.loads(raw.decode("utf-8"))
        st.version = doc["version"]
        st.digests = dict(doc["digests"])
        st.file_mtime = doc["file_mtime"]
        st.hashed_at = doc["hashed_at"]
        st.run_started_at = doc["run_started_at"]
        st.present = True
    except (UnicodeDecodeError, json.JSONDecodeError, KeyError, TypeError) as e:
        st.parse_error = f"{type(e).__name__}: {e}"

    return st


# --- database mirror ------------------------------------------------------


@dataclass
class DbRow:
    """One row of the ``files`` table, joined to its mountpoint path."""

    mountpoint: str
    rel_path: str
    inode: int
    algo: str
    digest: str
    file_mtime: str
    hashed_at: str
    run_started_at: str
    version: str
    size: int | None
    mode: int | None
    uid: int | None
    gid: int | None
    nlink: int | None
    dev: int | None
    ctime: str | None
    atime: str | None
    birthtime: str | None


def read_db(db_path) -> list[DbRow]:
    """Return all ``files`` rows from the SQLite mirror, joined to mountpoints.

    Opened read-only-ish (a plain connection; the harness never writes). Returns
    ``[]`` if the database or expected tables do not exist yet — convenient for
    asserting "nothing was mirrored".
    """
    db_path = Path(db_path)
    if not db_path.exists():
        return []

    conn = sqlite3.connect(str(db_path))
    try:
        try:
            cur = conn.execute(
                """
                SELECT m.path, f.rel_path, f.inode, f.algo, f.digest,
                       f.file_mtime, f.hashed_at, f.run_started_at, f.version,
                       f.size, f.mode, f.uid, f.gid, f.nlink, f.dev,
                       f.ctime, f.atime, f.birthtime
                  FROM files f
                  JOIN mountpoints m ON m.id = f.mountpoint_id
                 ORDER BY m.path, f.rel_path, f.algo
                """
            )
        except sqlite3.OperationalError:
            return []  # tables not created yet
        return [DbRow(*row) for row in cur.fetchall()]
    finally:
        conn.close()
