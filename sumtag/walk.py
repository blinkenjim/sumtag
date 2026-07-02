"""Directory traversal with @sumtag-ignore pruning.

Yields the path of every file to be processed, pruning any directory that
contains an ``@sumtag-ignore`` marker (and its whole subtree) before descending.
Pruning is a traversal-level exclusion, so it applies in every mode (CLAUDE.md
"Ignore markers").
"""

from __future__ import annotations

import os
from typing import Callable, Iterator

#: Marker filename that prunes a directory subtree.
IGNORE_MARKER = "@sumtag-ignore"


def _same_path(a: str, b: str) -> bool:
    return os.path.normpath(a) == os.path.normpath(b)


def iter_files(
    roots: list[str],
    *,
    respect_ignore: bool = True,
    on_warn: Callable[[str], None] | None = None,
) -> Iterator[str]:
    """Yield each file path under ``roots``, honoring @sumtag-ignore markers.

    With ``respect_ignore`` true, a directory holding the marker is pruned: its
    files are not yielded and it is not descended into. A marker on an explicit
    scan root is honored but draws a warning via ``on_warn`` (CLAUDE.md
    "Precedence and overrides"). The marker file itself is never yielded.
    """
    for start in roots:
        if os.path.isfile(start):
            yield start
            continue
        for dirpath, dirs, files in os.walk(start, topdown=True):
            if respect_ignore and IGNORE_MARKER in files:
                if on_warn is not None and _same_path(dirpath, start):
                    on_warn(f"sumtag: {start}: @sumtag-ignore on scan root; skipping")
                dirs[:] = []  # prune the subtree
                continue       # and skip this directory's files
            for name in files:
                if name == IGNORE_MARKER:
                    continue   # the marker is never hashed or stamped
                yield os.path.join(dirpath, name)
