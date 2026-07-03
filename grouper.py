#!/usr/bin/env python3
"""grouper.py -- a playground for grouping files recorded in a sumtag database.

Sumtag stamps files with an XXH3 digest and mirrors that metadata into an
optional SQLite database (see CLAUDE.md "Database storage"). Byte-identical
files therefore share a digest, so duplicate detection is just a GROUP BY away.
This script is the sandbox for that "intent #2" tooling -- deliberately small
and meant to be hacked on.

Usage:
    python3 grouper.py DATABASE                # list duplicate groups
    python3 grouper.py DATABASE --min N        # only groups of >= N files
    python3 grouper.py DATABASE --index        # (re)build the directory index
    python3 grouper.py DATABASE --ls DIR       # list files in one directory

Directory index
---------------
SQL has no collection objects, so "the files inside a directory" is modelled
as two derived tables grouper builds (and owns) inside the sumtag database:

    dirs      -- the distillation of every files.rel_path down to the distinct
                 directories that directly contain at least one stamped file,
                 identified the same way files are: (mountpoint_id, rel_path).
                 Its integer id is the directory's key.
    dir_files -- dir_id -> files.rowid, indexed on dir_id, so selecting all
                 files in a directory is one indexed lookup by key.

These are derived data, rebuilt wholesale by --index from the files table.
They go stale when sumtag rescans (new files won't appear until the next
--index), and they reference files by rowid, which VACUUM can renumber --
both fine for an index you can rebuild in one command. Only --index writes;
every other mode opens the database read-only.
"""

from __future__ import annotations

import argparse
import os
import sqlite3
import sys

# --- Directory index (grouper-owned derived tables) -------------------------

DIR_INDEX_SQL = """
DROP TABLE IF EXISTS dir_files;
DROP TABLE IF EXISTS dirs;

CREATE TABLE dirs (
  id            INTEGER PRIMARY KEY,
  mountpoint_id INTEGER NOT NULL REFERENCES mountpoints(id),
  rel_path      TEXT NOT NULL,          -- '' is the mount point itself
  UNIQUE (mountpoint_id, rel_path)
);

CREATE TABLE dir_files (
  file_id INTEGER PRIMARY KEY,          -- files.rowid; a file has one directory
  dir_id  INTEGER NOT NULL REFERENCES dirs(id)
);

-- The key that makes "all files in this directory" fast: without it the
-- dir_id -> files mapping would be a full scan of dir_files.
CREATE INDEX idx_dir_files_dir ON dir_files(dir_id);
"""


def build_dir_index(conn: sqlite3.Connection) -> tuple[int, int]:
    """(Re)build dirs/dir_files from the files table; returns (dirs, files).

    A full drop-and-rebuild: the tables are pure derivations of files, so
    incremental maintenance isn't worth its complexity in a playground.
    """
    conn.executescript(DIR_INDEX_SQL)
    dir_ids: dict[tuple[int, str], int] = {}
    files = conn.execute(
        "SELECT rowid, mountpoint_id, rel_path FROM files").fetchall()
    for f in files:
        key = (f["mountpoint_id"], os.path.dirname(f["rel_path"]))
        dir_id = dir_ids.get(key)
        if dir_id is None:
            cur = conn.execute(
                "INSERT INTO dirs(mountpoint_id, rel_path) VALUES (?, ?)", key)
            dir_id = cur.lastrowid
            dir_ids[key] = dir_id
        conn.execute(
            "INSERT INTO dir_files(file_id, dir_id) VALUES (?, ?)",
            (f["rowid"], dir_id))
    conn.commit()
    return len(dir_ids), len(files)


def resolve_dir(conn: sqlite3.Connection, path: str):
    """Map a directory path (absolute or mount-relative) to its dirs row.

    An absolute path that exists locally is split with sumtag's own
    firmlink-aware mount_relative(), so lookups agree exactly with how the
    rows were stored. Otherwise the value is tried as a mount-relative
    rel_path directly -- the detached-database case, where the drive the
    rows describe isn't currently mounted here.
    """
    candidates: list[tuple[str | None, str]] = []
    if os.path.isdir(path):
        from sumtag.store import mount_relative
        mount, rel = mount_relative(path)
        candidates.append((mount, "" if rel == "." else rel))
    candidates.append((None, path.rstrip("/")))

    for mount, rel in candidates:
        if mount is not None:
            row = conn.execute(
                """
                SELECT d.id, d.rel_path, m.path AS mount
                  FROM dirs d JOIN mountpoints m ON m.id = d.mountpoint_id
                 WHERE m.path = ? AND d.rel_path = ?
                """, (mount, rel)).fetchone()
        else:
            row = conn.execute(
                """
                SELECT d.id, d.rel_path, m.path AS mount
                  FROM dirs d JOIN mountpoints m ON m.id = d.mountpoint_id
                 WHERE d.rel_path = ?
                """, (rel,)).fetchone()
        if row is not None:
            return row
    return None


def ls(conn: sqlite3.Connection, path: str) -> int:
    """List the files in one directory via the index -- one lookup by key."""
    d = resolve_dir(conn, path)
    if d is None:
        print(f"grouper: directory not in index: {path} (run --index?)",
              file=sys.stderr)
        return 1
    rows = conn.execute(
        """
        SELECT f.rel_path, f.size, f.algo, f.digest
          FROM files f JOIN dir_files df ON df.file_id = f.rowid
         WHERE df.dir_id = ?
      ORDER BY f.rel_path
        """, (d["id"],)).fetchall()
    print(f"{os.path.join(d['mount'], d['rel_path'])}  ({len(rows)} file(s))")
    for r in rows:
        size = "?" if r["size"] is None else str(r["size"])
        print(f"    {r['algo']}:{r['digest']}  {size:>10}  "
              f"{os.path.basename(r['rel_path'])}")
    return 0


# --- Duplicate report --------------------------------------------------------

def connect(path: str, writable: bool = False) -> sqlite3.Connection:
    """Open the sumtag SQLite database, read-only unless building the index."""
    if not os.path.exists(path):
        sys.exit(f"grouper: no such database: {path}")
    mode = "rw" if writable else "ro"
    conn = sqlite3.connect(f"file:{path}?mode={mode}", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def abspath(row: sqlite3.Row) -> str:
    """Reconstruct a file's absolute path from mountpoint + rel_path.

    The database stores paths mount-relative so they survive remounts
    (CLAUDE.md "Path strategy"); we glue the mount point back on for display.
    """
    return os.path.join(row["mount"], row["rel_path"])


def find_duplicate_groups(conn: sqlite3.Connection, min_count: int):
    """Yield (algo, digest, [rows]) for every digest shared by >= min_count files.

    Grouping is by (algo, digest): a digest only means "same bytes" within one
    algorithm, so two files stamped under different algos won't group together
    even if identical -- the documented mixed-algorithm hazard (CLAUDE.md
    Database storage). We surface the algo in the output so that's visible.
    """
    dup_keys = conn.execute(
        """
        SELECT algo, digest, COUNT(*) AS n
          FROM files
      GROUP BY algo, digest
        HAVING n >= ?
      ORDER BY n DESC, digest
        """,
        (min_count,),
    ).fetchall()

    for key in dup_keys:
        rows = conn.execute(
            """
            SELECT f.rel_path, f.inode, f.size, m.path AS mount
              FROM files f
              JOIN mountpoints m ON m.id = f.mountpoint_id
             WHERE f.algo = ? AND f.digest = ?
          ORDER BY m.path, f.rel_path
            """,
            (key["algo"], key["digest"]),
        ).fetchall()
        yield key["algo"], key["digest"], rows


def report(conn: sqlite3.Connection, min_count: int) -> None:
    groups = list(find_duplicate_groups(conn, min_count))
    if not groups:
        print("no duplicate groups found")
        return

    total_files = 0
    for algo, digest, rows in groups:
        total_files += len(rows)
        # Distinct inodes reveal real copies vs hard links to the same data:
        # same inode = same bytes on disk, deleting one frees nothing.
        inodes = {r["inode"] for r in rows}
        hardlink_note = "" if len(inodes) == len(rows) else \
            f"  ({len(inodes)} distinct inode(s) -- some are hard links)"
        print(f"\n{algo}:{digest}  x{len(rows)}{hardlink_note}")
        for r in rows:
            print(f"    [ino {r['inode']}]  {abspath(r)}")

    print(f"\n{len(groups)} group(s), {total_files} file(s) total")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="grouper.py",
        description="Group files in a sumtag database: duplicates by digest, "
                    "files by directory.",
    )
    parser.add_argument("database", help="path to the sumtag SQLite database")
    parser.add_argument("--min", type=int, default=2, metavar="N",
                        help="only report groups of at least N files (default: 2)")
    parser.add_argument("--index", action="store_true",
                        help="(re)build the dirs/dir_files directory index")
    parser.add_argument("--ls", metavar="DIR",
                        help="list the files in DIR (absolute or mount-relative) "
                             "using the directory index")
    args = parser.parse_args(argv)

    conn = connect(args.database, writable=args.index)
    try:
        if args.index:
            n_dirs, n_files = build_dir_index(conn)
            print(f"indexed {n_files} file(s) across {n_dirs} directorie(s)")
        if args.ls is not None:
            return ls(conn, args.ls)
        if not args.index:
            report(conn, args.min)
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
