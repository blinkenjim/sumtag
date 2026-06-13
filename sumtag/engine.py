"""Orchestration: walk the tree and stamp, or verify, each file.

Ties together the I/O-free decision logic (:mod:`decide`) with the I/O edges
(:mod:`walk`, :mod:`xattr`, :mod:`hashing`). Returns the process exit code
(CLAUDE.md "main() contract" / EXIT STATUS).
"""

from __future__ import annotations

import os
import sys

from . import decide, hashing, schema, store as store_mod, walk, xattr

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
        if args.do_import:
            return _import(roots, args, rep, store)
        return _stamp(roots, args, rep, store)
    finally:
        if store is not None:
            store.close()


def _mirror(store, path: str, meta: dict) -> None:
    """Mirror a file's metadata into the database (one row per location)."""
    mount, rel = store_mod.mount_relative(path)
    mp_id = store.ensure_mountpoint(mount)
    for algo, digest in meta["digests"].items():
        store.upsert_file(mp_id, rel, algo, digest, meta["file_mtime"],
                          meta["hashed_at"], meta["run_started_at"], meta["version"])


def _stamp(roots, args, rep: _Reporter, store) -> int:
    run_started = schema.now_iso()
    current_major = schema.major_of(schema.VERSION)
    exit_code = EXIT_OK

    for path in walk.iter_files(roots, respect_ignore=not args.no_ignore,
                                on_warn=rep.error):
        try:
            live = schema.iso_utc_ns(os.stat(path).st_mtime_ns)
            meta = _read_meta(path)
            rehash, reason = decide.should_rehash(
                meta, live, args.force, [schema.ALGO], current_major)

            if rehash:
                if args.dry_run:
                    rep.info(f"would hash {path} ({reason})")
                    continue
                digest = hashing.hash_file(path)
                meta = schema.build_meta({schema.ALGO: digest}, live, run_started)
                xattr.set(path, schema.XATTR_NAME, schema.dumps(meta))
                rep.info(f"hashed {path} ({reason})")
            else:
                rep.detail(f"skip   {path} ({reason})")

            # Mirror in addition to the xattr: re-hashed files carry fresh
            # metadata, skipped (up-to-date) ones carry their existing metadata,
            # so the database reflects the whole tree, not just changed files.
            if store is not None:
                _mirror(store, path, meta)
        except OSError as e:
            exit_code = EXIT_ERRORS
            rep.error(f"sumtag: {path}: {e}")

    return exit_code


def _import(roots, args, rep: _Reporter, store) -> int:
    """Copy existing xattr metadata into the database without computing anything."""
    exit_code = EXIT_OK
    for path in walk.iter_files(roots, respect_ignore=not args.no_ignore,
                                on_warn=rep.error):
        try:
            meta = _read_meta(path)
            if meta is None or not meta.get("digests"):
                rep.info(f"skipped (no metadata) {path}")
                continue
            if args.dry_run:
                rep.info(f"would import {path}")
                continue
            _mirror(store, path, meta)
            rep.info(f"imported {path}")
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
            live = schema.iso_utc_ns(os.stat(path).st_mtime_ns)

            if meta is None or not meta.get("digests"):
                rep.info(f"unverifiable {path}")
                any_error = True  # the check could not be completed for this file
                continue

            computed = {algo: hashing.hash_file(path) for algo in meta["digests"]}
            outcome = decide.classify_verify(meta, live, computed)

            if outcome == decide.CORRUPTION:
                rep.info(f"CORRUPT {path}")
                any_corruption = True
            elif outcome == decide.STALE:
                rep.detail(f"stale  {path} (modified since hash; restamp needed)")
            else:
                rep.detail(f"ok     {path}")
        except OSError as e:
            any_error = True
            rep.error(f"sumtag: {path}: {e}")

    if any_corruption:
        return EXIT_CORRUPTION
    if any_error:
        return EXIT_ERRORS
    return EXIT_OK
