"""Orchestration: walk the tree and stamp, or verify, each file.

Ties together the I/O-free decision logic (:mod:`decide`) with the I/O edges
(:mod:`walk`, :mod:`xattr`, :mod:`hashing`). Returns the process exit code
(CLAUDE.md "main() contract" / EXIT STATUS).
"""

from __future__ import annotations

import os
import sys

from . import decide, hashing, schema, walk, xattr

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
    """Entry point from the CLI; dispatches to verify or stamp."""
    if args.database or args.do_import:
        # Database mirror and --import are specified but not wired up yet.
        print("sumtag: --database/--import are not implemented yet", file=sys.stderr)
        return EXIT_ERRORS

    rep = _Reporter(args)
    roots = args.directories or ["."]

    if args.verify:
        return _verify(roots, args, rep)
    return _stamp(roots, args, rep)


def _stamp(roots, args, rep: _Reporter) -> int:
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

            if not rehash:
                rep.detail(f"skip   {path} ({reason})")
                continue
            if args.dry_run:
                rep.info(f"would hash {path} ({reason})")
                continue

            digest = hashing.hash_file(path)
            new_meta = schema.build_meta({schema.ALGO: digest}, live, run_started)
            xattr.set(path, schema.XATTR_NAME, schema.dumps(new_meta))
            rep.info(f"hashed {path} ({reason})")
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
