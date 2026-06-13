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
    parser.add_argument("-f", "--force", action="store_true",
                        help="re-hash every file, ignoring existing metadata")
    parser.add_argument("--database", metavar="VALUE",
                        help="mirror metadata into a database (SQLite path or "
                             "scheme:// DSN; only SQLite is implemented)")
    parser.add_argument("--import", dest="do_import", action="store_true",
                        help="copy existing xattr metadata into the database "
                             "without computing; requires --database")
    parser.add_argument("--verify", action="store_true",
                        help="recompute and compare against stored checksums "
                             "(read-only); writes nothing")
    parser.add_argument("--no-ignore", action="store_true",
                        help="disregard @sumtag-ignore marker files")
    parser.add_argument("--version", action="version",
                        version=f"%(prog)s {__version__}")
    return parser


def validate(args: argparse.Namespace, parser: argparse.ArgumentParser) -> None:
    """Enforce the flag conflicts documented in CLAUDE.md / sumtag(1)."""
    if args.quiet and args.verbose:
        parser.error("-q/--quiet and -v/--verbose are mutually exclusive")
    if args.force and args.dry_run:
        parser.error("--force and --dry-run are mutually exclusive")
    if args.force and args.do_import:
        parser.error("--force and --import are mutually exclusive")
    if args.do_import and not args.database:
        parser.error("--import requires --database")
    if args.verify:
        if args.database:
            parser.error("--verify cannot be combined with --database")
        if args.force:
            parser.error("--verify cannot be combined with --force")
        if args.do_import:
            parser.error("--verify cannot be combined with --import")
    # --progress vs -q: the later one on the command line wins, with a warning.
    # argparse does not preserve option order, so that is resolved at run time
    # (TODO) rather than here.


def main(argv: list[str] | None = None) -> int:
    """Entry point for both `sumtag` and `python3 -m sumtag`.

    Returns an int exit code (see module constants and sumtag(1) EXIT STATUS).
    """
    parser = build_parser()
    args = parser.parse_args(argv)
    validate(args, parser)

    from . import engine
    return engine.run(args)


if __name__ == "__main__":
    sys.exit(main())
