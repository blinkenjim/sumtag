#!/usr/bin/env python3
"""grouper.py -- a playground for grouping files recorded in a sumtag database.

Sumtag stamps files with an XXH3 digest and mirrors that metadata into an
optional SQLite database (see CLAUDE.md "Database storage"). Byte-identical
files therefore share a digest, so duplicate detection is just a GROUP BY away.
This script is the sandbox for that "intent #2" tooling -- deliberately small
and read-only for now, meant to be hacked on.

Usage:
    python3 grouper.py DATABASE            # list duplicate groups
    python3 grouper.py DATABASE --min N    # only groups of >= N files (default 2)

Nothing here writes to the database or the filesystem; it only reads and reports.
"""

from __future__ import annotations

import argparse
import os
import sqlite3
import sys


def connect(path: str) -> sqlite3.Connection:
    """Open the sumtag SQLite database read-only, failing loudly if it's absent."""
    if not os.path.exists(path):
        sys.exit(f"grouper: no such database: {path}")
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
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
        description="Group files in a sumtag database by shared digest (find duplicates).",
    )
    parser.add_argument("database", help="path to the sumtag SQLite database")
    parser.add_argument("--min", type=int, default=2, metavar="N",
                        help="only report groups of at least N files (default: 2)")
    args = parser.parse_args(argv)

    conn = connect(args.database)
    try:
        report(conn, args.min)
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
