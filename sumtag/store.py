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
from dataclasses import dataclass

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
  inode          INTEGER NOT NULL,
  algo           TEXT NOT NULL,
  digest         TEXT NOT NULL,
  file_mtime     TEXT NOT NULL,
  hashed_at      TEXT NOT NULL,
  run_started_at TEXT NOT NULL,
  version        TEXT NOT NULL,
  size           INTEGER,
  mode           INTEGER,
  uid            INTEGER,
  gid            INTEGER,
  nlink          INTEGER,
  dev            INTEGER,
  ctime          TEXT,
  atime          TEXT,
  birthtime      TEXT,
  UNIQUE (mountpoint_id, rel_path)
);
CREATE INDEX IF NOT EXISTS idx_digest ON files(digest);
"""


@dataclass
class StatData:
    """Filesystem metadata from os.stat(), beyond what the xattr already records.

    All fields map directly to st_* attributes. ``birthtime`` is macOS-only
    (creation time); it is None on Linux and other platforms that lack it.
    ``mtime`` and ``inode`` are omitted — they are stored separately in the
    primary columns and do not belong here.
    """
    size: int
    mode: int
    uid: int
    gid: int
    nlink: int
    dev: int
    ctime: str      # metadata-change time, ISO 8601 UTC microseconds
    atime: str      # last-access time, ISO 8601 UTC microseconds
    birthtime: str | None  # creation time (macOS only); None elsewhere


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

    def upsert_file(self, mountpoint_id: int, rel_path: str, inode: int,
                    algo: str, digest: str, file_mtime: str, hashed_at: str,
                    run_started_at: str, version: str,
                    stat: StatData | None = None) -> None:
        """Insert or update the single row for a file location (its identity).

        When ``stat`` is provided, all locate columns are written.  When it is
        ``None``, existing locate columns are preserved via COALESCE so a
        stat-less update never clobbers data written by a prior ``--locate`` run.
        """
        sd = stat
        self._conn.execute(
            """
            INSERT INTO files (mountpoint_id, rel_path, inode, algo, digest,
                               file_mtime, hashed_at, run_started_at, version,
                               size, mode, uid, gid, nlink, dev, ctime, atime, birthtime)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT (mountpoint_id, rel_path) DO UPDATE SET
                inode=excluded.inode, algo=excluded.algo, digest=excluded.digest,
                file_mtime=excluded.file_mtime, hashed_at=excluded.hashed_at,
                run_started_at=excluded.run_started_at, version=excluded.version,
                size=COALESCE(excluded.size, size),
                mode=COALESCE(excluded.mode, mode),
                uid=COALESCE(excluded.uid, uid),
                gid=COALESCE(excluded.gid, gid),
                nlink=COALESCE(excluded.nlink, nlink),
                dev=COALESCE(excluded.dev, dev),
                ctime=COALESCE(excluded.ctime, ctime),
                atime=COALESCE(excluded.atime, atime),
                birthtime=COALESCE(excluded.birthtime, birthtime)
            """,
            (mountpoint_id, rel_path, inode, algo, digest,
             file_mtime, hashed_at, run_started_at, version,
             sd.size if sd else None, sd.mode if sd else None,
             sd.uid if sd else None, sd.gid if sd else None,
             sd.nlink if sd else None, sd.dev if sd else None,
             sd.ctime if sd else None, sd.atime if sd else None,
             sd.birthtime if sd else None),
        )

    def update_stat(self, mountpoint_id: int, rel_path: str,
                    stat: StatData) -> None:
        """Update only the locate/stat columns on an existing row; no-op if absent.

        Used by ``--locate`` in ``--import`` mode for files that lack a usable
        xattr but may already have a row from a previous stamping run.
        """
        self._conn.execute(
            """
            UPDATE files
               SET size=?, mode=?, uid=?, gid=?, nlink=?, dev=?,
                   ctime=?, atime=?, birthtime=?
             WHERE mountpoint_id=? AND rel_path=?
            """,
            (stat.size, stat.mode, stat.uid, stat.gid, stat.nlink, stat.dev,
             stat.ctime, stat.atime, stat.birthtime,
             mountpoint_id, rel_path),
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
