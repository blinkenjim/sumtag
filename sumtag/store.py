"""Database mirror: the optional second sink for file metadata.

The xattr is the source of truth that travels with the file; the database is a
detached, queryable mirror (CLAUDE.md "Database storage"). Everything funnels
through :func:`open_store`, which resolves the ``--database`` value to a backend.
Today only SQLite exists; the ``scheme://`` DSN grammar is recognized so paths
written into scripts now are never reinterpreted later.

The ``Store`` interface is kept narrow — only the operations actually used —
rather than grown speculatively for backends that do not exist yet.
"""

from __future__ import annotations

import os
import re
import sqlite3

# A value is a DSN iff it matches scheme://… ; otherwise it is a SQLite file
# path (a bare "mysql:host", with no slashes, is a path — ':' is legal in names).
_SCHEME_RE = re.compile(r"^[a-z][a-z0-9+.-]*://")

_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS mountpoints (
  id   INTEGER PRIMARY KEY,
  path TEXT UNIQUE NOT NULL
);
CREATE TABLE IF NOT EXISTS files (
  mountpoint_id  INTEGER NOT NULL REFERENCES mountpoints(id),
  rel_path       TEXT NOT NULL,
  algo           TEXT NOT NULL,
  digest         TEXT NOT NULL,
  file_mtime     TEXT NOT NULL,
  hashed_at      TEXT NOT NULL,
  run_started_at TEXT NOT NULL,
  version        TEXT NOT NULL,
  UNIQUE (mountpoint_id, rel_path)
);
CREATE INDEX IF NOT EXISTS idx_digest ON files(digest);
"""


class SQLiteStore:
    """The SQLite backend. Creates its schema on open; idempotent UPSERT by location."""

    def __init__(self, path: str) -> None:
        self._conn = sqlite3.connect(path)
        self._conn.executescript(_SCHEMA_SQL)
        self._mp_cache: dict[str, int] = {}

    def ensure_mountpoint(self, path: str) -> int:
        """Return the id for a mount point, inserting it once if new."""
        cached = self._mp_cache.get(path)
        if cached is not None:
            return cached
        self._conn.execute(
            "INSERT OR IGNORE INTO mountpoints(path) VALUES (?)", (path,))
        row = self._conn.execute(
            "SELECT id FROM mountpoints WHERE path = ?", (path,)).fetchone()
        self._mp_cache[path] = row[0]
        return row[0]

    def upsert_file(self, mountpoint_id: int, rel_path: str, algo: str, digest: str,
                    file_mtime: str, hashed_at: str, run_started_at: str,
                    version: str) -> None:
        """Insert or update the single row for a file location (its identity)."""
        self._conn.execute(
            """
            INSERT INTO files (mountpoint_id, rel_path, algo, digest,
                               file_mtime, hashed_at, run_started_at, version)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT (mountpoint_id, rel_path) DO UPDATE SET
                algo=excluded.algo, digest=excluded.digest,
                file_mtime=excluded.file_mtime, hashed_at=excluded.hashed_at,
                run_started_at=excluded.run_started_at, version=excluded.version
            """,
            (mountpoint_id, rel_path, algo, digest,
             file_mtime, hashed_at, run_started_at, version),
        )

    def close(self) -> None:
        self._conn.commit()
        self._conn.close()


def open_store(value: str):
    """Resolve a ``--database`` value to a Store. Only SQLite is implemented today.

    No scheme  -> SQLite file path. ``scheme://…`` -> a DSN dispatched by scheme;
    ``sqlite://`` is accepted, other schemes are recognized but rejected.
    """
    if _SCHEME_RE.match(value):
        scheme, _, rest = value.partition("://")
        if scheme == "sqlite":
            return SQLiteStore(rest)
        raise NotImplementedError(
            f"database backend '{scheme}://' is not yet supported")
    return SQLiteStore(value)


def mount_relative(path, ismount=os.path.ismount) -> tuple[str, str]:
    """Return ``(mount_point, rel_path)`` for ``path``.

    Walks up with ``ismount`` (injectable for testing) until a mount boundary,
    so the stored path stays stable across remounts (CLAUDE.md "Path strategy").
    """
    ap = os.path.abspath(path)
    cur = ap if os.path.isdir(ap) else os.path.dirname(ap)
    while not ismount(cur):
        parent = os.path.dirname(cur)
        if parent == cur:  # reached the filesystem root
            break
        cur = parent
    return cur, os.path.relpath(ap, cur)
