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

import json
import os
import re
import sqlite3
import sys
from dataclasses import dataclass

# --- Mount-point detection -------------------------------------------------
#
# The database stores paths relative to their mount point (CLAUDE.md "Path
# strategy"), so we must find the mount a file lives on. The obvious tool,
# os.path.ismount(), detects a boundary by a change in st_dev between a
# directory and its parent -- which is WRONG on macOS. Under APFS the read-only
# System volume ("/") and the writable Data volume ("/System/Volumes/Data") are
# joined by firmlinks and share a single st_dev, so ismount() sees no boundary
# and walks straight past the real mount up to "/". A file in ~/Development then
# gets recorded under mount "/" instead of "/System/Volumes/Data".
#
# The syscall that actually knows the answer is statfs(2): its f_mntonname field
# is the mount point (it is what df(1) reports). We bind it via ctypes on macOS
# -- matching xattr.py's approach of avoiding a second third-party dependency --
# and fall back to the ismount walk elsewhere (Linux, where ismount is correct).

_IS_MACOS = sys.platform == "darwin"

if _IS_MACOS:
    import ctypes
    import ctypes.util

    _MFSTYPENAMELEN = 16
    _MAXPATHLEN = 1024

    class _fsid_t(ctypes.Structure):
        _fields_ = [("val", ctypes.c_int32 * 2)]

    class _statfs_t(ctypes.Structure):
        # struct statfs from <sys/mount.h>, 64-bit-inode layout (the modern
        # default on all supported macOS). f_mntonname is the field we want.
        _fields_ = [
            ("f_bsize", ctypes.c_uint32),
            ("f_iosize", ctypes.c_int32),
            ("f_blocks", ctypes.c_uint64),
            ("f_bfree", ctypes.c_uint64),
            ("f_bavail", ctypes.c_uint64),
            ("f_files", ctypes.c_uint64),
            ("f_ffree", ctypes.c_uint64),
            ("f_fsid", _fsid_t),
            ("f_owner", ctypes.c_uint32),
            ("f_type", ctypes.c_uint32),
            ("f_flags", ctypes.c_uint32),
            ("f_fssubtype", ctypes.c_uint32),
            ("f_fstypename", ctypes.c_char * _MFSTYPENAMELEN),
            ("f_mntonname", ctypes.c_char * _MAXPATHLEN),
            ("f_mntfromname", ctypes.c_char * _MAXPATHLEN),
            ("f_flags_ext", ctypes.c_uint32),
            ("f_reserved", ctypes.c_uint32 * 7),
        ]

    _libc = ctypes.CDLL(ctypes.util.find_library("c"), use_errno=True)
    # arm64 exports plain `statfs` (already 64-bit inode); x86_64 needs the
    # `$INODE64` variant to get this struct layout. Prefer the suffixed symbol
    # where present so the layout above always matches.
    _statfs = None
    for _sym in ("statfs$INODE64", "statfs"):
        try:
            _statfs = getattr(_libc, _sym)
        except AttributeError:
            continue
        _statfs.restype = ctypes.c_int
        _statfs.argtypes = [ctypes.c_char_p, ctypes.POINTER(_statfs_t)]
        break

    def _mount_point(abs_path: str) -> str | None:
        """The mount point of an existing path via statfs(2), or None on failure."""
        if _statfs is None:
            return None
        buf = _statfs_t()
        if _statfs(os.fsencode(abs_path), ctypes.byref(buf)) != 0:
            return None
        return os.fsdecode(buf.f_mntonname)

else:  # Linux and other platforms: ismount() correctly sees device boundaries.

    def _mount_point(abs_path: str) -> str | None:
        return None  # no syscall shortcut; mount_relative() uses the ismount walk


def _walk_up_mount(abs_dir: str, ismount) -> str:
    """Walk up from a directory until ismount() reports a mount boundary."""
    cur = abs_dir
    while not ismount(cur):
        parent = os.path.dirname(cur)
        if parent == cur:  # reached the filesystem root
            break
        cur = parent
    return cur


def _relativize(abs_path: str, mount: str) -> str:
    """The rel_path such that ``os.path.join(mount, rel)`` locates ``abs_path``.

    Usually ``abs_path`` is lexically under ``mount`` and this is a plain strip.
    But on macOS statfs may report a mount the path only *reaches through a
    firmlink* rather than sitting beneath: e.g. ``/Users/x`` lives on the Data
    volume mounted at ``/System/Volumes/Data`` yet is presented at root. A
    lexical relpath there would escape upward (``../../..``) and not recompose.
    Since the Data volume's contents are firmlinked to root, the physical
    location is the whole rooted path rebased under the mount; we verify that
    with ``samefile`` before trusting it, and only fall back to the (escaping)
    lexical form if the rebase doesn't point back at the same file.
    """
    rel = os.path.relpath(abs_path, mount)
    if not (rel == os.pardir or rel.startswith(os.pardir + os.sep)):
        return rel  # abs_path is genuinely under mount -- the normal case
    rebased = os.path.relpath(abs_path, "/")
    try:
        if os.path.samefile(os.path.join(mount, rebased), abs_path):
            return rebased
    except OSError:
        pass
    return rel

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
CREATE TABLE IF NOT EXISTS prescan_summary (
  id          INTEGER PRIMARY KEY CHECK (id = 1),  -- exactly one row, ever
  file_count  INTEGER NOT NULL,
  total_bytes INTEGER NOT NULL,
  roots       TEXT NOT NULL,     -- JSON array of normalized absolute scan roots
  sum_mode    INTEGER NOT NULL,  -- counting context: --sum's standard decision?
  force       INTEGER NOT NULL,  -- counting context: was --force in effect?
  exclude     TEXT NOT NULL,     -- counting context: JSON array of --exclude patterns
  no_ignore   INTEGER NOT NULL,  -- counting context: was --no-ignore in effect?
  created_at  TEXT NOT NULL
);
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


@dataclass
class PrescanSummary:
    """The one-row totals a --prescan --database run leaves for --db-prescan.

    Deliberately aggregate-only, never per-file (CLAUDE.md "--db-prescan"):
    two totals plus the context that determined which files got counted. The
    context fields let --db-prescan refuse totals that answered a different
    question than the current run is asking.
    """
    file_count: int
    total_bytes: int
    roots: list[str]      # normalized absolute scan roots
    sum_mode: bool        # whether --sum's standard mtime decision drove the count
    force: bool           # whether --force was in effect
    exclude: list[str]    # --exclude patterns in effect
    no_ignore: bool       # whether --no-ignore was in effect
    created_at: str       # ISO 8601 UTC


class SQLiteStore:
    """The SQLite backend. Creates its schema on open; idempotent UPSERT by location.

    ``mode`` is the SQLite URI open mode: the default ``rwc`` creates a
    missing database file (the mirror-sink case); ``rw`` and ``ro`` require
    it to already exist (--prune-dirs, which reconciles an existing mirror
    and must never conjure an empty one), with ``ro`` additionally skipping
    schema creation for a strictly read-only open (its -n preview). A
    missing file under rw/ro raises FileNotFoundError.
    """

    def __init__(self, path: str, mode: str = "rwc") -> None:
        try:
            self._conn = sqlite3.connect(f"file:{path}?mode={mode}", uri=True)
            if mode != "ro":
                self._conn.executescript(_SCHEMA_SQL)
        except sqlite3.OperationalError as e:
            if mode != "rwc" and not os.path.exists(path):
                raise FileNotFoundError(f"no such database: {path}") from e
            raise
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

    def iter_file_dirs(self, mount_path: str, rel_prefix: str):
        """Every distinct directory that directly contains a files row under
        the given mountpoint and rel prefix, as sorted (dir_rel, file_count)
        pairs. ``rel_prefix`` of '' means the whole mountpoint.

        Directories are derived from the file rows themselves (the schema
        stores no directory table), which is what makes --prune-dirs need no
        recursion: a vanished parent implies vanished children, and every
        child that held files is independently in this list.
        """
        row = self._conn.execute(
            "SELECT id FROM mountpoints WHERE path = ?",
            (mount_path,)).fetchone()
        if row is None:
            return []
        if rel_prefix:
            prefix = rel_prefix.rstrip("/") + "/"
            cur = self._conn.execute(
                "SELECT rel_path FROM files WHERE mountpoint_id = ? "
                "AND substr(rel_path, 1, ?) = ?",
                (row[0], len(prefix), prefix))
        else:
            cur = self._conn.execute(
                "SELECT rel_path FROM files WHERE mountpoint_id = ?",
                (row[0],))
        counts: dict[str, int] = {}
        for (rel_path,) in cur:
            d = os.path.dirname(rel_path)
            counts[d] = counts.get(d, 0) + 1
        return sorted(counts.items())

    def delete_dir_files(self, mount_path: str, dir_rel: str) -> int:
        """Delete every files row *directly* inside dir_rel (dirname equality,
        never recursive -- see iter_file_dirs); returns the rows deleted.
        Committed immediately, so an interrupted --prune-dirs keeps the
        prunes it completed.
        """
        row = self._conn.execute(
            "SELECT id FROM mountpoints WHERE path = ?",
            (mount_path,)).fetchone()
        if row is None:
            return 0
        if dir_rel:
            prefix = dir_rel + "/"
            cur = self._conn.execute(
                "DELETE FROM files WHERE mountpoint_id = ? "
                "AND substr(rel_path, 1, ?) = ? "
                "AND instr(substr(rel_path, ?), '/') = 0",
                (row[0], len(prefix), prefix, len(prefix) + 1))
        else:
            cur = self._conn.execute(
                "DELETE FROM files WHERE mountpoint_id = ? "
                "AND instr(rel_path, '/') = 0",
                (row[0],))
        self._conn.commit()
        return cur.rowcount

    def save_prescan_summary(self, s: PrescanSummary) -> None:
        """Store the prescan totals, replacing any previous summary (one per db)."""
        self._conn.execute(
            """
            INSERT OR REPLACE INTO prescan_summary
                (id, file_count, total_bytes, roots, sum_mode, force,
                 exclude, no_ignore, created_at)
            VALUES (1, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (s.file_count, s.total_bytes, json.dumps(s.roots),
             int(s.sum_mode), int(s.force), json.dumps(s.exclude),
             int(s.no_ignore), s.created_at),
        )

    def close(self) -> None:
        self._conn.commit()
        self._conn.close()


def open_store(value: str, mode: str = "rwc"):
    """Resolve a ``--database`` value to a Store. Only SQLite is implemented today.

    No scheme  -> SQLite file path. ``scheme://…`` -> a DSN dispatched by scheme;
    ``sqlite://`` is accepted, other schemes are recognized but rejected.
    ``mode`` is passed through to the backend (see SQLiteStore): ``rwc``
    creates a missing database, ``rw``/``ro`` require an existing one.
    """
    if _SCHEME_RE.match(value):
        scheme, _, rest = value.partition("://")
        if scheme == "sqlite":
            return SQLiteStore(rest, mode)
        raise NotImplementedError(
            f"database backend '{scheme}://' is not yet supported")
    return SQLiteStore(value, mode)


def read_prescan_summary(value: str) -> PrescanSummary | None:
    """Read the stored prescan summary from a --database value, side-effect-free.

    Opens SQLite read-only so a missing database file is never created as a
    byproduct (--db-prescan composes with -n, whose contract is no side
    effects anywhere). Returns None when the database file, the table, or the
    row is absent -- the caller turns that into the hard "run --prescan
    --database first" error. Non-SQLite DSNs raise NotImplementedError,
    matching open_store.
    """
    if _SCHEME_RE.match(value):
        scheme, _, rest = value.partition("://")
        if scheme != "sqlite":
            raise NotImplementedError(
                f"database backend '{scheme}://' is not yet supported")
        path = rest
    else:
        path = value
    try:
        conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        row = conn.execute(
            """
            SELECT file_count, total_bytes, roots, sum_mode, force,
                   exclude, no_ignore, created_at
              FROM prescan_summary WHERE id = 1
            """).fetchone()
        conn.close()
    except sqlite3.OperationalError:  # file absent/unopenable or table absent
        return None
    if row is None:
        return None
    return PrescanSummary(
        file_count=row[0], total_bytes=row[1], roots=json.loads(row[2]),
        sum_mode=bool(row[3]), force=bool(row[4]), exclude=json.loads(row[5]),
        no_ignore=bool(row[6]), created_at=row[7])


def mount_relative(path, ismount=os.path.ismount) -> tuple[str, str]:
    """Return ``(mount_point, rel_path)`` for ``path``.

    The mount point is stored separately so the path stays stable across
    remounts (CLAUDE.md "Path strategy"). On macOS it comes from statfs(2),
    which sees APFS firmlink boundaries that ``os.path.ismount`` cannot; on
    other platforms (and as a fallback if statfs fails) it comes from walking
    up with ``ismount`` (injectable for testing) until a mount boundary.
    """
    ap = os.path.abspath(path)
    start = ap if os.path.isdir(ap) else os.path.dirname(ap)
    mount = _mount_point(ap) or _walk_up_mount(start, ismount)
    return mount, _relativize(ap, mount)
