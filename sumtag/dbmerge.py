#!/usr/bin/env python3
"""dbmerge -- combine per-volume sumtag databases into one.

Sumtag's schema was always multi-volume (the mountpoints table exists so one
database can describe files across many filesystems); one-database-per-volume
is an operational choice that buys parallel scanning, SQLite being a
single-writer store. dbmerge closes the loop: it folds any number of
per-volume SOURCE databases into one TARGET database, so cross-volume
dedupe/grouper runs get the one database they consume -- with zero changes
to those tools. The per-volume databases stay the scanning targets; the
merged database is a rebuildable analysis artifact, re-merged whenever the
sources move on.

Replace-per-mountpoint (the load-bearing semantic, settled 2026-07-19):

    For each mountpoint present in a source, the target's existing rows for
    that mountpoint are DELETED and the source's rows inserted fresh. A
    plain row-level upsert would carry updates over but never remove target
    rows whose source rows were pruned -- the merged database would
    accumulate ghosts forever. Replacement makes the merge idempotent and
    each source authoritative for its own mountpoints, including an empty
    one: a source mountpoint with zero rows still clears the target's rows
    for it (authoritative emptiness). Target mountpoints no source mentions
    are left untouched, so one volume can be re-merged without feeding all
    of them.

    The corollary is the collision refusal: the same mountpoint path
    recorded in two sources is an error, because the second source's
    replacement would delete the first's just-merged rows. Sources must
    partition by mountpoint -- exactly what one-database-per-volume
    produces. (This doubles as the guard for the CLAUDE.md known
    limitation: a mount path is not a globally stable filesystem identity,
    and two different filesystems recorded under one path must not be
    silently interleaved.)

What is copied, and what never is:

    Only the mountpoints and files tables flow. Grouper's derived tables
    (dirs, dir_files, dir_pairs, groups, group_dirs, grouper_meta) are
    never copied -- dir_files references files.rowid, which the merge
    renumbers -- and any such artifacts already in the TARGET are dropped:
    a merge that changed the files table has invalidated them, and
    stale-but-present artifacts are the documented trap (rerun grouper
    --prep on the merged database; --clean-db's VACUUM is available if the
    space matters). The prescan_summary row is likewise never copied: it
    answers a per-volume question about a filesystem walk, not a question
    about this database. A summary already in the TARGET is left alone
    for the same reason -- it describes a walk, which the merge does not
    invalidate.

    Sources are opened read-only, always, and are never modified.

Guards, all before the target is opened for writing:

    - every source must exist and carry the sumtag tables;
    - the target must not be among the sources, and no source may be given
      twice (checked under realpath, so symlinks cannot disguise overlap);
    - the mountpoint-collision refusal above;
    - more than one digest algorithm across the sources (and the target's
      surviving rows) is refused without --allow-mixed -- the CLAUDE.md
      mixed-algorithm hazard: cross-algorithm duplicates silently fail to
      match in the merged corpus.

-n/--dry-run previews everything -- per-mountpoint would-merge lines with
row counts, would-replace, would-drop -- with no side effects anywhere: the
target is opened read-only if it exists and is not created if it does not
(sumtag's -n contract).

--progress shows one aggregate bar across all sources: rows-merged /
rows-total (the denominator is known up front -- COUNT(*) per source costs
nothing), rendered by the house CountIndicator with rate, percent, elapsed,
and ETA; the usual conventions apply (opt-in, 2-second threshold, stderr,
cleared on completion, suppressed when stderr is not a terminal).

Commits are per mountpoint, so an interrupted run keeps the mountpoints it
completed and the summary stays honest (counters only count committed
work); Ctrl-C prints the same summary and exits 130, sumtag's convention.

Exit status (house 0/1/2): 0 = nothing to do (no rows merged, none
replaced -- in practice only empty sources, since replacement always
rewrites); 1 = the target was (or, under -n, would be) modified -- the
normal successful merge; 2 = errors or a refusal.

Usage:
    dbmerge --database merged.sqlite tank1.sqlite tank2.sqlite ...
    dbmerge --database merged.sqlite -n tank1.sqlite ...   # preview
    dbmerge --database merged.sqlite --progress tank*.sqlite
"""

from __future__ import annotations

import argparse
import os
import sqlite3
import sys

from sumtag import progress
from sumtag.store import _SCHEMA_SQL  # the package's schema; shared, not store-private

#: Grouper's derived tables: never copied, dropped from the target when a
#: merge changes the files table out from under them.
GROUPER_TABLES = ("dirs", "dir_files", "dir_pairs", "groups", "group_dirs",
                  "grouper_meta")

#: Rows per executemany batch when streaming a mountpoint's rows across.
CHUNK = 5000

#: The files columns that travel (everything except mountpoint_id, which is
#: remapped to the target's id for that mountpoint).
_COPY_COLS = ("rel_path", "inode", "algo", "digest", "file_mtime",
              "hashed_at", "run_started_at", "version", "size", "mode",
              "uid", "gid", "nlink", "dev", "ctime", "atime", "birthtime")

EXIT_NOTHING = 0        # nothing to do (no rows merged, none replaced)
EXIT_MERGED = 1         # target modified (or would be, under -n)
EXIT_ERRORS = 2         # errors or a refusal
EXIT_INTERRUPTED = 130  # Ctrl-C (128 + SIGINT, the shell convention)


class Source:
    """One source database, opened read-only, with its mountpoint census."""

    def __init__(self, given: str) -> None:
        self.given = given
        self.conn = sqlite3.connect(f"file:{given}?mode=ro", uri=True)
        if not _is_sumtag_db(self.conn):  # before the census queries it
            self.conn.close()
            raise ValueError(
                f"{given}: not a sumtag database (no mountpoints/files tables)")
        # (path, source mountpoint id, row count), sorted by path so the
        # merge order is deterministic.
        self.mounts: list[tuple[str, int, int]] = [
            (path, mp_id, self.conn.execute(
                "SELECT COUNT(*) FROM files WHERE mountpoint_id = ?",
                (mp_id,)).fetchone()[0])
            for mp_id, path in self.conn.execute(
                "SELECT id, path FROM mountpoints ORDER BY path")]
        self.algos: set[str] = {r[0] for r in self.conn.execute(
            "SELECT DISTINCT algo FROM files")}

    def close(self) -> None:
        self.conn.close()


def _is_sumtag_db(conn: sqlite3.Connection) -> bool:
    names = {r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type = 'table'")}
    return {"mountpoints", "files"} <= names


def _open_sources(args) -> list[Source] | str:
    """Open every source read-only and run the source-side guards.
    Returns the sources or an error message."""
    seen: dict[str, str] = {}       # realpath -> the spelling first given
    target_real = os.path.realpath(args.database)
    sources = []
    for given in args.sources:
        real = os.path.realpath(given)
        if real == target_real:
            _close_all(sources)
            return f"{given}: the target database cannot also be a source"
        if real in seen:
            _close_all(sources)
            return f"{given}: same database as source {seen[real]}"
        seen[real] = given
        if not os.path.exists(given):
            _close_all(sources)
            return f"{given}: no such database"
        try:
            src = Source(given)
        except ValueError as e:
            _close_all(sources)
            return str(e)
        except sqlite3.OperationalError as e:
            _close_all(sources)
            return f"{given}: {e}"
        sources.append(src)

    owner: dict[str, str] = {}      # mountpoint path -> source that carries it
    for src in sources:
        for path, _, _ in src.mounts:
            if path in owner:
                _close_all(sources)
                return (f"mountpoint {path} is recorded in both {owner[path]} "
                        f"and {src.given}; sources must partition by "
                        f"mountpoint (the second replacement would delete "
                        f"the first's rows)")
            owner[path] = src.given
    return sources


def _close_all(sources: list[Source]) -> None:
    for src in sources:
        src.close()


def _algo_guard(sources: list[Source], target: sqlite3.Connection | None,
                allow_mixed: bool) -> str | None:
    """The mixed-algorithm refusal, over the merged corpus the target will
    hold: every source plus whatever target rows survive the merge."""
    algos: set[str] = set()
    for src in sources:
        algos |= src.algos
    if target is not None and _is_sumtag_db(target):
        merged_paths = {p for src in sources for p, _, _ in src.mounts}
        for algo, path in target.execute(
                "SELECT DISTINCT f.algo, m.path FROM files f "
                "JOIN mountpoints m ON m.id = f.mountpoint_id"):
            if path not in merged_paths:  # replaced rows don't survive
                algos.add(algo)
    if len(algos) > 1 and not allow_mixed:
        return (f"mixed digest algorithms across these databases "
                f"({', '.join(sorted(algos))}): cross-algorithm duplicates "
                f"cannot match in the merged corpus; pass --allow-mixed to "
                f"proceed anyway")
    return None


class Run:
    """Counters and output for one merge pass. Committed counters only ever
    reflect committed work, so the Ctrl-C summary is honest."""

    def __init__(self, dry_run: bool) -> None:
        self.dry_run = dry_run
        self.merged = 0         # rows inserted (committed)
        self.replaced = 0       # target rows deleted (committed)
        self.mounts = 0         # mountpoints merged (committed)
        self.dropped = False    # grouper artifacts dropped (or would be)
        self.bar = None

    def say(self, verb: str, rest: str) -> None:
        if self.bar is not None:
            self.bar.interrupt()
        mood = "would " if self.dry_run else ""
        print(f"{mood}{verb} {rest}")


def _merge_mount(run: Run, target: sqlite3.Connection, src: Source,
                 path: str, src_id: int, count: int, done: int) -> int:
    """Replace one mountpoint's rows in the target with the source's.
    One transaction, committed here; returns rows streamed for the bar."""
    run.say("merge", f"{path} ({count} row{'s' if count != 1 else ''} "
                     f"from {src.given})")
    if run.dry_run:
        return 0
    target.execute("INSERT OR IGNORE INTO mountpoints(path) VALUES (?)",
                   (path,))
    tgt_id = target.execute("SELECT id FROM mountpoints WHERE path = ?",
                            (path,)).fetchone()[0]
    replaced = target.execute(
        "DELETE FROM files WHERE mountpoint_id = ?", (tgt_id,)).rowcount
    cols = ", ".join(_COPY_COLS)
    select = src.conn.execute(
        f"SELECT {cols} FROM files WHERE mountpoint_id = ?", (src_id,))
    insert = (f"INSERT INTO files (mountpoint_id, {cols}) "
              f"VALUES ({', '.join('?' * (len(_COPY_COLS) + 1))})")
    streamed = 0
    while True:
        rows = select.fetchmany(CHUNK)
        if not rows:
            break
        target.executemany(insert, [(tgt_id, *row) for row in rows])
        streamed += len(rows)
        if run.bar is not None:
            run.bar(done + streamed)
    target.commit()  # per mountpoint: an interrupted run keeps completed ones
    run.merged += streamed
    run.replaced += replaced
    run.mounts += 1
    return streamed


def _target_replaced_preview(args, sources: list[Source]) -> int:
    """How many target rows the merge would delete, for the -n summary."""
    if not os.path.exists(args.database):
        return 0
    try:
        conn = sqlite3.connect(f"file:{args.database}?mode=ro", uri=True)
    except sqlite3.OperationalError:
        return 0
    replaced = 0
    if _is_sumtag_db(conn):
        for src in sources:
            for path, _, _ in src.mounts:
                row = conn.execute(
                    "SELECT COUNT(*) FROM files f JOIN mountpoints m "
                    "ON m.id = f.mountpoint_id WHERE m.path = ?",
                    (path,)).fetchone()
                replaced += row[0]
    conn.close()
    return replaced


def _grouper_artifacts_present(conn: sqlite3.Connection) -> bool:
    names = {r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type = 'table'")}
    return any(t in names for t in GROUPER_TABLES)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="dbmerge",
        description="Combine per-volume sumtag databases into one, so "
                    "cross-volume dedupe/grouper runs have a single database "
                    "to consume. For each mountpoint in a source, the "
                    "target's rows are replaced by the source's.")
    parser.add_argument("--database", required=True, metavar="TARGET",
                        help="the merged database to write (created if "
                             "missing; existing mountpoints not named by any "
                             "source are left untouched)")
    parser.add_argument("sources", metavar="SOURCE", nargs="+",
                        help="per-volume sumtag databases to merge in, "
                             "opened read-only")
    parser.add_argument("-n", "--dry-run", action="store_true",
                        help="report what would be merged without writing "
                             "anything (the target is not created if missing)")
    parser.add_argument("--progress", action="store_true",
                        help="show one aggregate rows-merged/rows-total bar "
                             "across all sources (stderr; appears after 2s)")
    parser.add_argument("--allow-mixed", dest="allow_mixed",
                        action="store_true",
                        help="proceed even when the merged corpus would span "
                             "more than one digest algorithm (cross-algorithm "
                             "duplicates silently fail to match)")
    args = parser.parse_args(argv)

    opened = _open_sources(args)
    if isinstance(opened, str):
        print(f"dbmerge: {opened}", file=sys.stderr)
        return EXIT_ERRORS
    sources = opened

    run = Run(args.dry_run)
    target: sqlite3.Connection | None = None
    interrupted = False
    try:
        # Target: rwc for a live run (created if missing); ro or absent
        # under -n, which must have no side effects anywhere.
        target_exists = os.path.exists(args.database)
        if not args.dry_run:
            try:
                target = sqlite3.connect(f"file:{args.database}?mode=rwc",
                                         uri=True)
                target.executescript(_SCHEMA_SQL)
            except sqlite3.OperationalError as e:
                print(f"dbmerge: {args.database}: {e}", file=sys.stderr)
                return EXIT_ERRORS
        elif target_exists:
            target = sqlite3.connect(f"file:{args.database}?mode=ro",
                                     uri=True)

        err = _algo_guard(sources, target, args.allow_mixed)
        if err is not None:
            print(f"dbmerge: {err}", file=sys.stderr)
            return EXIT_ERRORS

        total = sum(count for src in sources for _, _, count in src.mounts)
        if not args.dry_run:
            run.bar = progress.make_count(total, args.progress, unit="rows")

        if target is not None and _grouper_artifacts_present(target):
            run.say("drop", "grouper artifacts (stale after a merge; rerun "
                            "grouper --prep)")
            run.dropped = True
            if not args.dry_run:
                for t in GROUPER_TABLES:
                    target.execute(f"DROP TABLE IF EXISTS {t}")
                target.commit()

        done = 0
        for src in sources:
            for path, src_id, count in src.mounts:
                done += _merge_mount(run, target, src, path, src_id, count,
                                     done)
    except KeyboardInterrupt:
        interrupted = True  # commits are per mountpoint; counters are honest
    finally:
        if run.bar is not None:
            run.bar.finish()
        # close() without commit rolls back any partial mountpoint.
        if target is not None:
            target.close()
        _close_all(sources)

    if args.dry_run:
        # Nothing was streamed; the totals are the sources' own census.
        run.merged = sum(c for src in sources for _, _, c in src.mounts)
        run.mounts = sum(len(src.mounts) for src in sources)
        run.replaced = _target_replaced_preview(args, sources)

    if interrupted:
        print("interrupted")
    mood = "would " if args.dry_run else ""
    pairs = [(f"{mood}merge", f"{run.merged} row{'s' if run.merged != 1 else ''}, "
              f"{run.mounts} mountpoint{'s' if run.mounts != 1 else ''}, "
              f"{len(sources)} source{'s' if len(sources) != 1 else ''}")]
    if run.replaced:
        pairs.append((f"{mood}replace",
                      f"{run.replaced} row{'s' if run.replaced != 1 else ''}"))
    if run.dropped:
        pairs.append((f"{mood}drop", "grouper artifacts (rerun grouper --prep)"))
    pairs.append(("database", args.database))
    pairs.append(("sources", ", ".join(args.sources)))
    width = max(len(label) for label, _ in pairs) + 1
    for label, value in pairs:
        print(f"{label + ':':<{width}} {value}")

    if interrupted:
        return EXIT_INTERRUPTED
    modified = run.merged or run.replaced or run.dropped
    return EXIT_MERGED if modified else EXIT_NOTHING


if __name__ == "__main__":
    raise SystemExit(main())
