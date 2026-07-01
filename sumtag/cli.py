"""Command-line interface for sumtag.

Early stub: the argument parser and the conflict validation reflect the design
in CLAUDE.md and sumtag(1), but the traversal, hashing, xattr I/O, database,
and verify logic are not implemented yet. ``main()`` returns an int exit code
(see EXIT STATUS in sumtag(1)).
"""

from __future__ import annotations

import argparse
import sys

from . import __version__

# Exit codes. Used primarily by --verify; see sumtag(1) EXIT STATUS.
EXIT_OK = 0           # all verified intact / normal success
EXIT_CORRUPTION = 1   # --verify: one or more checksum mismatches
EXIT_ERRORS = 2       # unreadable files or errors prevented completion


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="sumtag",
        description="Stamp files with an XXH3 checksum and metadata stored in "
                    "the user.sumtag extended attribute; optionally mirror to a "
                    "database and verify against stored checksums.",
    )
    parser.add_argument("directories", nargs="*", metavar="DIRECTORY",
                        help="directories to scan (default: current directory)")

    parser.add_argument("-n", "--dry-run", action="store_true",
                        help="report what would be done; write nothing")
    parser.add_argument("-q", "--quiet", action="count", default=0,
                        help="suppress normal output; -qq also suppresses errors")
    parser.add_argument("-v", "--verbose", action="count", default=0,
                        help="explain each decision; -vv adds deep internals")
    parser.add_argument("--progress", action="store_true",
                        help="show within-file progress for large files")
    parser.add_argument("--si", action="store_true",
                        help="display --progress sizes/rates in decimal (SI) "
                             "units instead of binary (KiB/MiB/GiB)")
    parser.add_argument("-f", "--force", action="store_true",
                        help="re-hash every file, ignoring existing metadata")
    parser.add_argument("--database", metavar="VALUE",
                        help="the database to act on (SQLite path or scheme:// "
                             "DSN; only SQLite is implemented). Requires at least "
                             "one of --sum, --import, --locate")
    parser.add_argument("--sum", action="store_true",
                        help="compute/re-hash per the normal mtime-based decision "
                             "and mirror the result into --database")
    parser.add_argument("--import", dest="do_import", action="store_true",
                        help="copy existing xattr metadata into the database "
                             "without computing (unless --force overrides); "
                             "requires --database")
    parser.add_argument("--verify", action="store_true",
                        help="recompute and compare against stored checksums "
                             "(read-only); writes nothing")
    parser.add_argument("--remove", action="store_true",
                        help="remove the user.sumtag xattr from every file in "
                             "the tree (a testing/reset utility)")
    parser.add_argument("--no-ignore", action="store_true",
                        help="disregard @sumtag-ignore marker files")
    parser.add_argument("--locate", action="store_true",
                        help="stat every file and write filesystem metadata to the "
                             "database, implying --import (propagates existing "
                             "metadata too); requires --database")
    parser.add_argument("--version", action="version",
                        version=f"%(prog)s {__version__}")
    return parser


def validate(args: argparse.Namespace, parser: argparse.ArgumentParser) -> None:
    """Enforce the flag conflicts documented in CLAUDE.md / sumtag(1)."""
    if args.quiet and args.verbose:
        parser.error("-q/--quiet and -v/--verbose are mutually exclusive")
    if args.force and args.dry_run:
        parser.error("--force and --dry-run are mutually exclusive")
    if args.sum and not args.database:
        parser.error("--sum requires --database")
    if args.do_import and not args.database:
        parser.error("--import requires --database")
    if args.locate and not args.database:
        parser.error("--locate requires --database")
    if args.database and not (args.sum or args.do_import or args.locate):
        parser.error("--database requires at least one of --sum, --import, --locate")
    if args.verify:
        if args.database:
            parser.error("--verify cannot be combined with --database")
        if args.sum:
            parser.error("--verify cannot be combined with --sum")
        if args.force:
            parser.error("--verify cannot be combined with --force")
        if args.do_import:
            parser.error("--verify cannot be combined with --import")
        if args.locate:
            parser.error("--verify cannot be combined with --locate")
    if args.remove:
        if args.database:
            parser.error("--remove cannot be combined with --database")
        if args.sum:
            parser.error("--remove cannot be combined with --sum")
        if args.do_import:
            parser.error("--remove cannot be combined with --import")
        if args.locate:
            parser.error("--remove cannot be combined with --locate")
        if args.verify:
            parser.error("--remove cannot be combined with --verify")
        if args.force:
            parser.error("--remove cannot be combined with --force")
    # --progress vs -q is resolved by command-line order, not rejected here --
    # see _resolve_progress_quiet, which needs the raw argv argparse discards.


def _resolve_progress_quiet(raw: list[str], args: argparse.Namespace) -> None:
    """Resolve --progress vs -q by command-line order (CLAUDE.md "Flags").

    Only matters when both are actually in effect. Whichever appears later
    wins outright -- the loser's flag is discarded, not just the narrow
    overlap between them -- and a warning is printed to stderr either way.
    """
    if not (args.progress and args.quiet):
        return
    last_progress = last_quiet = -1
    for i, tok in enumerate(raw):
        if tok == "--progress":
            last_progress = i
        elif tok in ("-q", "--quiet"):
            last_quiet = i
        elif tok.startswith("-") and not tok.startswith("--") and "q" in tok[1:]:
            last_quiet = i  # a bundled short cluster, e.g. -fq or -qq
    if last_progress > last_quiet:
        print("sumtag: --progress overrides -q (appears later on the command line)",
              file=sys.stderr)
        args.quiet = 0
    else:
        print("sumtag: -q overrides --progress (appears later on the command line)",
              file=sys.stderr)
        args.progress = False


def main(argv: list[str] | None = None) -> int:
    """Entry point for both `sumtag` and `python3 -m sumtag`.

    Returns an int exit code (see module constants and sumtag(1) EXIT STATUS).
    """
    raw = list(argv) if argv is not None else sys.argv[1:]
    parser = build_parser()
    args = parser.parse_args(raw)
    validate(args, parser)
    _resolve_progress_quiet(raw, args)

    from . import engine
    return engine.run(args)


if __name__ == "__main__":
    sys.exit(main())
