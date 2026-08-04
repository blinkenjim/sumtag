"""Directory traversal with @sumtag-ignore pruning and --exclude filtering.

Yields the path of every file to be processed, pruning any directory that
contains an ``@sumtag-ignore`` marker (and its whole subtree) before descending,
and skipping anything whose basename matches an ``--exclude`` glob pattern.
Both are traversal-level exclusions, so they apply in every mode (CLAUDE.md
"Ignore markers", "--exclude").
"""

from __future__ import annotations

import fnmatch
import os
from typing import Callable, Iterator, Sequence

#: Marker filename that prunes a directory subtree.
IGNORE_MARKER = "@sumtag-ignore"


def _name_key(name: str) -> tuple[str, str]:
    """Case-insensitive sort key; the raw name breaks case-only ties so the
    order stays deterministic ('Readme' vs 'readme')."""
    # Primary: the casefolded name (Finder-style, case-insensitive).
    # Secondary: the raw name, so case-only twins order deterministically
    # and the key stays total (distinct names never compare equal).
    return (name.casefold(), name)


def _same_path(a: str, b: str) -> bool:
    return os.path.normpath(a) == os.path.normpath(b)


def _excluded_by(name: str, patterns: Sequence[str]) -> str | None:
    """Return the first pattern ``name`` matches, or None.

    Matching is fnmatchcase: case-sensitive on every platform, no normcase
    surprises -- the same name matches the same pattern on macOS and Linux.
    """
    # First match wins (which pattern gets reported is cosmetic; matching at
    # all is the contract). A pattern containing '/' can never match: a
    # basename never contains one.
    for pat in patterns:
        if fnmatch.fnmatchcase(name, pat):
            return pat
    return None


def iter_files(
    roots: list[str],
    *,
    respect_ignore: bool = True,
    exclude: Sequence[str] | None = None,
    on_warn: Callable[[str], None] | None = None,
    on_dir: Callable[[str], None] | None = None,
) -> Iterator[str]:
    """Yield each file path under ``roots``, honoring @sumtag-ignore markers.

    With ``respect_ignore`` true, a directory holding the marker is pruned: its
    files are not yielded and it is not descended into. A marker on an explicit
    scan root is honored but draws a warning via ``on_warn`` (CLAUDE.md
    "Precedence and overrides"). The marker file itself is never yielded.

    ``exclude`` is a sequence of glob patterns matched (case-sensitively)
    against basenames only: a matching file is skipped, a matching directory
    is pruned like a marker (not descended, not announced via ``on_dir``).
    Excludes are independent of ``respect_ignore`` -- ``--no-ignore`` governs
    markers only -- and, like the marker, apply in every mode. A scan root
    whose own basename matches is skipped with an ``on_warn`` warning, same
    as a marker on a scan root.

    ``on_dir`` is called with each directory actually visited -- after the
    prune check, before any of its files are yielded -- so a caller can
    announce the directory ahead of reading anything inside it. Pruned
    directories and file roots draw no call.

    Traversal is deterministic: within each directory, files are yielded in
    ascending case-insensitive alphabetical order (casefolded; the raw name
    breaks case-only ties) and subdirectories are recursed into in the same
    order (files first, then subdirectories -- os.walk's shape), so the
    processing order tracks a Finder window or `ls` listing. Roots are
    processed in the order given.
    """
    patterns = list(exclude) if exclude else []
    for start in roots:
        # Root-level gates first: an excluded root is skipped with a warning
        # (the user explicitly named it, so silence would be confusing), and
        # an explicit FILE root is the user's explicit claim -- yielded
        # as-is, no walk, no on_dir.
        pat = _excluded_by(os.path.basename(os.path.normpath(start)), patterns)
        if pat is not None:
            if on_warn is not None:
                on_warn(f"sumtag: {start}: matches --exclude {pat!r} "
                        f"on scan root; skipping")
            continue
        if os.path.isfile(start):
            yield start
            continue

        for dirpath, dirs, files in os.walk(start, topdown=True):
            # Sorting both lists in place is what makes the traversal
            # deterministic: files stream out in this order, and os.walk
            # recurses into dirs in the (sorted) order we leave behind.
            dirs.sort(key=_name_key)
            files.sort(key=_name_key)

            # The marker check runs before ANYTHING in this directory is
            # touched or announced; the whole subtree is pruned by emptying
            # dirs. Only the explicit scan root earns a warning.
            if respect_ignore and IGNORE_MARKER in files:
                if on_warn is not None and _same_path(dirpath, start):
                    on_warn(f"sumtag: {start}: @sumtag-ignore on scan root; skipping")
                dirs[:] = []
                continue

            # Excluded subdirectories are pruned exactly like marked ones:
            # never descended, never announced.
            if patterns:
                dirs[:] = [d for d in dirs if _excluded_by(d, patterns) is None]

            if on_dir is not None:
                on_dir(dirpath)  # visited-only, ahead of its files

            for name in files:
                if name == IGNORE_MARKER:
                    continue  # the marker itself is never hashed or stamped
                if patterns and _excluded_by(name, patterns) is not None:
                    continue
                path = os.path.join(dirpath, name)
                # Symlinks are not content (CLAUDE.md "Symbolic links"):
                # os.walk lists symlinks-to-files here, but yielding one
                # would hash THROUGH the link -- planting the xattr on the
                # target, possibly outside the tree -- or error on a broken
                # one. Skipped silently in every mode, like every
                # traversal-level exclusion.
                if os.path.islink(path):
                    continue
                yield path
