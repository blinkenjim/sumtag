"""Live within-file progress indicator for --progress.

Triggered by *time*, not file size: a modest file on a slow network mount is
just as worth watching as a huge one on fast local storage, and a huge file
that finishes quickly needs no indicator at all. An Indicator renders nothing
until THRESHOLD_SECONDS have elapsed since it was constructed; only then does
it start redrawing a single stderr line in place.
"""

from __future__ import annotations

import sys
import time

#: Seconds a single file's checksum must run before the indicator appears.
THRESHOLD_SECONDS = 5.0

#: Minimum seconds between redraws once triggered, so a fast disk doesn't
#: flood the terminal with a rewrite per 1 MiB chunk.
_RENDER_INTERVAL = 0.2

_MiB = 1 << 20


class Indicator:
    """A per-file progress callback for :func:`sumtag.hashing.hash_file`.

    Call ``finish()`` once hashing completes; it clears the line if anything
    was ever shown, so a file that finishes under the threshold leaves no
    trace at all.
    """

    def __init__(self, path: str, total: int) -> None:
        self._path = path
        self._total = total
        self._start = time.monotonic()
        self._last_render = 0.0
        self._shown = False

    def __call__(self, read: int) -> None:
        now = time.monotonic()
        if not self._shown:
            if now - self._start < THRESHOLD_SECONDS:
                return
            self._shown = True
        elif now - self._last_render < _RENDER_INTERVAL:
            return
        self._last_render = now
        pct = (read / self._total * 100) if self._total else 100.0
        sys.stderr.write(
            f"\rhashing {self._path}: {read / _MiB:.1f} / {self._total / _MiB:.1f} MiB ({pct:.0f}%)\033[K"
        )
        sys.stderr.flush()

    def finish(self) -> None:
        if self._shown:
            sys.stderr.write("\r\033[K")
            sys.stderr.flush()


def make(path: str, total: int, enabled: bool) -> Indicator | None:
    """Return an Indicator for ``path``, or None if progress should not run.

    Suppressed when not requested, and when stderr is not a terminal --
    redrawing a line with carriage returns would just corrupt a redirected
    log or pipe rather than showing anything useful.
    """
    if not enabled or not sys.stderr.isatty():
        return None
    return Indicator(path, total)
