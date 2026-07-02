#!/usr/bin/env python3
"""Independently cross-check sumtag's stamped MD5 digests against md5sum.

A standalone verification tool, deliberately separate from sumtag's own code
(same spirit as tests/harness/'s oracle): it reads the raw user.sumtag xattr
without importing anything from the sumtag package, and gets its "truth"
digest by shelling out to the OS md5sum binary rather than hashing in Python.
Two independent code paths agreeing is a much stronger signal than one path
checking itself.

This only compares the "md5" entry in the xattr's digests map. sumtag's
shipped default is XXH3 (see CLAUDE.md); to produce MD5-stamped files, flip
sumtag.schema.ALGO to "md5" for the run you want to cross-check, then point
this script at the same directory afterward.

Usage:
    python3 scripts/md5_crosscheck.py [-v] DIRECTORY...

Exit codes (mirrors sumtag --verify's convention):
    0   every file with a stored md5 digest matched
    1   one or more mismatches found
    2   files with no usable md5 digest to check, or read errors
"""

from __future__ import annotations

import argparse
import errno
import json
import os
import shutil
import subprocess
import sys

XATTR_NAME = "user.sumtag"
IGNORE_MARKER = "@sumtag-ignore"

_IS_MACOS = sys.platform == "darwin"
_ENOATTR = {getattr(errno, "ENODATA", None), getattr(errno, "ENOATTR", None)} - {None}

EXIT_OK = 0
EXIT_MISMATCH = 1
EXIT_ERRORS = 2


def read_xattr(path: str) -> bytes | None:
    """Read the raw user.sumtag xattr, independently of sumtag's own module.

    macOS has no stdlib xattr access, so this shells out to the built-in
    `xattr` command (hex-exchanged, to sidestep text-encoding pitfalls) rather
    than reimplementing sumtag's ctypes bindings. Linux uses the stdlib
    os.getxattr directly -- a thin syscall wrapper with no sumtag logic to
    share a bug with.
    """
    if _IS_MACOS:
        r = subprocess.run(["xattr", "-px", XATTR_NAME, path],
                          capture_output=True, text=True)
        if r.returncode != 0:
            if "No such xattr" in r.stderr:
                return None
            raise RuntimeError(f"xattr -px failed: {r.stderr.strip()}")
        return bytes.fromhex("".join(r.stdout.split()))
    else:
        try:
            return os.getxattr(path, XATTR_NAME)
        except OSError as e:
            if e.errno in _ENOATTR:
                return None
            raise


def md5sum(path: str) -> str:
    """Return the lowercase-hex MD5 digest of path, via the OS md5sum binary."""
    r = subprocess.run(["md5sum", path], capture_output=True, text=True, check=True)
    return r.stdout.split()[0].lower()


def iter_files(roots: list[str]):
    """Yield file paths under roots, pruning @sumtag-ignore subtrees.

    Mirrors sumtag's own traversal rule (CLAUDE.md "Ignore markers") so this
    script never expects a stamp on a file sumtag itself would have skipped.
    """
    for root in roots:
        if os.path.isfile(root):
            yield root
            continue
        for dirpath, dirnames, filenames in os.walk(root):
            if IGNORE_MARKER in filenames:
                dirnames[:] = []
                print(f"md5_crosscheck: {dirpath}: @sumtag-ignore present, pruned",
                     file=sys.stderr)
                continue
            for name in filenames:
                if name == IGNORE_MARKER:
                    continue
                yield os.path.join(dirpath, name)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                    formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("directories", nargs="+", metavar="DIRECTORY")
    parser.add_argument("-v", "--verbose", action="store_true",
                        help="also print a line for every matching file")
    args = parser.parse_args(argv)

    if shutil.which("md5sum") is None:
        print("md5_crosscheck: md5sum not found on PATH", file=sys.stderr)
        return EXIT_ERRORS

    matched = mismatched = unusable = errors = 0

    for path in iter_files(args.directories):
        try:
            raw = read_xattr(path)
            if raw is None:
                print(f"NO-XATTR   {path}")
                unusable += 1
                continue

            try:
                meta = json.loads(raw.decode("utf-8"))
                digests = meta["digests"]
            except (ValueError, KeyError, AttributeError):
                print(f"MALFORMED  {path}")
                unusable += 1
                continue

            stored = digests.get("md5")
            if stored is None:
                print(f"NO-MD5     {path} (digests present: {sorted(digests)})")
                unusable += 1
                continue

            computed = md5sum(path)
            if computed == stored.lower():
                matched += 1
                if args.verbose:
                    print(f"match      {path}")
            else:
                print(f"MISMATCH   {path} (stored {stored}, computed {computed})")
                mismatched += 1
        except (OSError, RuntimeError, subprocess.CalledProcessError) as e:
            print(f"ERROR      {path}: {e}", file=sys.stderr)
            errors += 1

    total = matched + mismatched + unusable + errors
    print(f"\n{matched} matched, {mismatched} mismatched, {unusable} unusable, "
         f"{errors} errors, {total} total", file=sys.stderr)

    if mismatched:
        return EXIT_MISMATCH
    if unusable or errors:
        return EXIT_ERRORS
    return EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
