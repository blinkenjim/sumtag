"""Orchestration: walk the tree and stamp, or verify, each file.

Ties together the I/O-free decision logic (:mod:`decide`) with the I/O edges
(:mod:`walk`, :mod:`xattr`, :mod:`hashing`). Returns the process exit code
(CLAUDE.md "main() contract" / EXIT STATUS).
"""

from __future__ import annotations

import os
import sys

from . import decide, hashing, progress as progress_mod, schema, store as store_mod, walk, xattr
from .store import StatData

EXIT_OK = 0
EXIT_CORRUPTION = 1
EXIT_ERRORS = 2


class _Reporter:
    """Honors -q/-qq (quiet count) and -v/-vv (verbose count)."""

    def __init__(self, args) -> None:
        self._quiet = args.quiet
        self._verbose = args.verbose

    def info(self, msg: str) -> None:        # normal output
        if self._quiet == 0:
            print(msg)

    def detail(self, msg: str) -> None:      # only with -v
        if self._verbose >= 1 and self._quiet == 0:
            print(msg)

    def error(self, msg: str) -> None:       # stderr; -qq suppresses
        if self._quiet < 2:
            print(msg, file=sys.stderr)


def _stat_data(st: os.stat_result) -> StatData:
    """Build a StatData from an os.stat_result, handling platform differences."""
    birthtime = None
    if hasattr(st, "st_birthtime"):  # macOS
        birthtime = schema.iso_utc_ns(int(st.st_birthtime * 1_000_000_000))
    return StatData(
        size=st.st_size,
        mode=st.st_mode,
        uid=st.st_uid,
        gid=st.st_gid,
        nlink=st.st_nlink,
        dev=st.st_dev,
        ctime=schema.iso_utc_ns(st.st_ctime_ns),
        atime=schema.iso_utc_ns(st.st_atime_ns),
        birthtime=birthtime,
    )


def _read_meta(path: str) -> dict | None:
    """Return the parsed sumtag xattr, or None if absent/unreadable/malformed."""
    raw = xattr.get(path, schema.XATTR_NAME)
    if raw is None:
        return None
    try:
        return schema.loads(raw)
    except (ValueError, KeyError):
        return None


def run(args) -> int:
    """Entry point from the CLI; dispatches to verify, import, or stamp."""
    rep = _Reporter(args)
    roots = args.directories or ["."]

    if args.verify:
        return _verify(roots, args, rep)

    # A database is opened only when there is something to write to it: never
    # under -n, so --dry-run has no side effect at all (not even creating the
    # file). --import always carries a database (enforced by the CLI).
    store = None
    if args.database and not args.dry_run:
        try:
            store = store_mod.open_store(args.database)
        except NotImplementedError as e:
            rep.error(f"sumtag: {e}")
            return EXIT_ERRORS

    try:
        return _stamp(roots, args, rep, store)
    finally:
        if store is not None:
            store.close()


def _mirror(store, path: str, meta: dict, inode: int,
            stat: StatData | None = None) -> None:
    """Mirror a file's metadata into the database (one row per location)."""
    mount, rel = store_mod.mount_relative(path)
    mp_id = store.ensure_mountpoint(mount)
    for algo, digest in meta["digests"].items():
        store.upsert_file(mp_id, rel, inode, algo, digest, meta["file_mtime"],
                          meta["hashed_at"], meta["run_started_at"], meta["version"],
                          stat=stat)


def _stamp(roots, args, rep: _Reporter, store) -> int:
    """Walk the tree, (re-)hashing and/or mirroring per the given flags.

    Without --database, or with --sum, files are (re-)hashed per the normal
    mtime-based decision (CLAUDE.md "Re-hashing logic"). With only --import
    and/or --locate, hashing is skipped by default -- their job is to
    propagate existing metadata and/or capture stat columns, not compute --
    unless --force overrides that refusal. --locate implies --import: existing
    metadata is mirrored either way; --locate additionally captures stat
    columns and stats files that have no metadata to mirror at all.
    """
    run_started = schema.now_iso()
    current_major = schema.major_of(schema.VERSION)
    exit_code = EXIT_OK
    need_stat = args.locate
    use_standard_decision = store is None or args.sum

    for path in walk.iter_files(roots, respect_ignore=not args.no_ignore,
                                on_warn=rep.error):
        try:
            st = os.stat(path)
            live = schema.iso_utc_ns(st.st_mtime_ns)
            meta = _read_meta(path)

            if use_standard_decision:
                rehash, reason = decide.should_rehash(
                    meta, live, args.force, [schema.ALGO], current_major)
            else:
                rehash = args.force
                reason = "forced" if args.force else "not computing (--import/--locate only)"

            if rehash:
                if args.dry_run:
                    rep.info(f"would hash {path} ({reason})")
                else:
                    rep.info(f"hash {path} ({reason})")
                    ind = progress_mod.make(path, st.st_size, args.progress)
                    digest = hashing.hash_file(path, progress=ind)
                    if ind is not None:
                        ind.finish()
                    meta = schema.build_meta({schema.ALGO: digest}, live, run_started)
                    xattr.set(path, schema.XATTR_NAME, schema.dumps(meta))
            elif meta is not None and meta.get("digests"):
                if use_standard_decision:
                    rep.detail(f"skip   {path} ({reason})")
                else:
                    rep.info(f"import {path}")
            else:
                rep.info(f"skip (no metadata) {path}")

            # Mirror in addition to the xattr: re-hashed and pre-existing
            # metadata both get mirrored, so the database reflects the whole
            # tree, not just changed files. Stat columns are only captured
            # with --locate; a file with neither still gets stat-only if
            # --locate is set (update_stat is a no-op if the row is absent).
            if store is not None and not args.dry_run:
                stat_data = _stat_data(st) if need_stat else None
                if meta is not None and meta.get("digests"):
                    _mirror(store, path, meta, st.st_ino, stat_data)
                elif need_stat:
                    mount, rel = store_mod.mount_relative(path)
                    mp_id = store.ensure_mountpoint(mount)
                    store.update_stat(mp_id, rel, stat_data)
        except OSError as e:
            exit_code = EXIT_ERRORS
            rep.error(f"sumtag: {path}: {e}")

    return exit_code


def _verify(roots, args, rep: _Reporter) -> int:
    any_corruption = False
    any_error = False

    for path in walk.iter_files(roots, respect_ignore=not args.no_ignore,
                                on_warn=rep.error):
        try:
            meta = _read_meta(path)
            st = os.stat(path)
            live = schema.iso_utc_ns(st.st_mtime_ns)

            if meta is None or not meta.get("digests"):
                rep.info(f"unverifiable {path}")
                any_error = True  # the check could not be completed for this file
                continue

            rep.info(f"verify {path}")
            computed = {}
            for algo in meta["digests"]:
                ind = progress_mod.make(path, st.st_size, args.progress)
                computed[algo] = hashing.hash_file(path, progress=ind)
                if ind is not None:
                    ind.finish()
            outcome = decide.classify_verify(meta, live, computed)

            if outcome == decide.CORRUPTION:
                rep.info(f"CORRUPT {path}")
                any_corruption = True
            elif outcome == decide.STALE:
                rep.info(f"stale  {path} (modified since hash; restamp needed)")
            # else: clean verify -- silence means nothing bad happened
        except OSError as e:
            any_error = True
            rep.error(f"sumtag: {path}: {e}")

    if any_corruption:
        return EXIT_CORRUPTION
    if any_error:
        return EXIT_ERRORS
    return EXIT_OK
