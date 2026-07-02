"""Run the sumtag conformance harness: ``python -m tests.harness``."""

from __future__ import annotations

import argparse
import sys
import tempfile
from pathlib import Path

from . import xattrio
from .report import report
from .runner import DEFAULT_SUMTAG_CMD, run_all
from .scenarios import catalog


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m tests.harness",
        description="End-to-end conformance harness for sumtag.",
    )
    parser.add_argument("-k", "--filter", metavar="SUBSTR",
                        help="only run scenarios whose name contains SUBSTR")
    parser.add_argument("-l", "--list", action="store_true",
                        help="list scenario names and exit")
    parser.add_argument("-v", "--verbose", action="store_true",
                        help="show descriptions and captured exit/stderr on failure")
    parser.add_argument("--keep", action="store_true",
                        help="keep each scenario's temp tree for inspection")
    parser.add_argument("--workdir", metavar="DIR",
                        help="parent dir for temp trees (default: system temp)")
    parser.add_argument("--sumtag", metavar="CMD",
                        help="override the sumtag command (space-separated)")
    args = parser.parse_args(argv)

    scenarios = catalog()
    if args.filter:
        scenarios = [s for s in scenarios if args.filter in s.name]

    if args.list:
        for s in scenarios:
            print(f"{s.name}\t{s.description}")
        return 0

    if not scenarios:
        print("no scenarios matched", file=sys.stderr)
        return 1

    workdir = Path(args.workdir) if args.workdir else Path(tempfile.gettempdir())
    if not xattrio.supported(workdir):
        print(f"error: the filesystem at {workdir} does not support extended "
              f"attributes; pick another --workdir.", file=sys.stderr)
        return 2

    sumtag_cmd = args.sumtag.split() if args.sumtag else DEFAULT_SUMTAG_CMD
    outcomes = run_all(scenarios, sumtag_cmd=sumtag_cmd, workdir=workdir, keep=args.keep)
    return report(outcomes, verbose=args.verbose)


if __name__ == "__main__":
    sys.exit(main())
