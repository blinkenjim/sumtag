"""Live within-file progress indicator for --progress.

Triggered by *time*, not file size: a modest file on a slow network mount is
just as worth watching as a huge one on fast local storage, and a huge file
that finishes quickly needs no indicator at all. An Indicator renders nothing
until THRESHOLD_SECONDS have elapsed since it was constructed; only then does
it start redrawing a single stderr line in place.

Line format and field widths follow CLAUDE.md's "--progress indicator" /
"Line format" section:

    {size:>8}  {rate:>10}  [{bar}] {pct:>3}%  {elapsed:>8}  ETA {eta:<7}

The path is deliberately not part of this line -- it was already printed by
the per-file announcement (CLAUDE.md "Status lines") before hashing began.
"""

from __future__ import annotations

import sys
import time
from typing import Optional

#: Seconds a single file's checksum must run before the indicator appears.
THRESHOLD_SECONDS = 5.0

#: Minimum seconds between redraws once triggered, so a fast disk doesn't
#: flood the terminal with a rewrite per 1 MiB chunk.
_RENDER_INTERVAL = 0.2

#: Target line width the fixed-width fields are budgeted against. Only the
#: bar stretches to absorb whatever width is left, so the numbers never
#: jitter. Reacting to the terminal's actual width (SIGWINCH) is future work.
_LINE_WIDTH = 80

_SIZE_W = 8
_RATE_W = 10
_PCT_W = 3
_ELAPSED_W = 8
_ETA_W = 7

_FIXED_WIDTH = (
    _SIZE_W + 2                  # size, separator
    + _RATE_W + 2                # rate, separator
    + 1 + 1                      # "[" "]"
    + 1 + _PCT_W + 1             # separator, pct, "%"
    + 2 + _ELAPSED_W             # separator, elapsed
    + 2 + len("ETA ") + _ETA_W   # separator, "ETA ", eta
)
_BAR_WIDTH = max(_LINE_WIDTH - _FIXED_WIDTH, 1)

_BINARY_UNITS = ("B", "KiB", "MiB", "GiB", "TiB", "PiB")
_SI_UNITS = ("B", "kB", "MB", "GB", "TB", "PB")


def human_size(n: float, si: bool) -> str:
    """Render a byte count (or byte rate) human-readably.

    Binary (powers-of-1024) by default; decimal (powers-of-1000) with --si
    (CLAUDE.md "Flags").
    """
    base = 1000.0 if si else 1024.0
    units = _SI_UNITS if si else _BINARY_UNITS
    value = float(n)
    idx = 0
    while value >= base and idx < len(units) - 1:
        value /= base
        idx += 1
    if idx == 0:
        return f"{int(value)}{units[idx]}"
    return f"{value:.1f}{units[idx]}"


def _format_elapsed(seconds: float) -> str:
    """Render elapsed time as H:MM:SS."""
    total = int(seconds)
    h, rem = divmod(total, 3600)
    m, s = divmod(rem, 60)
    return f"{h}:{m:02d}:{s:02d}"


def _format_eta(seconds: float) -> str:
    """Render a remaining-time estimate as a compact human duration.

    Deliberately distinct from elapsed's clock style, since one is a fact
    and the other an estimate (CLAUDE.md "Line format").
    """
    total = int(seconds)
    if total < 60:
        return f"{total}s"
    if total < 3600:
        m, s = divmod(total, 60)
        return f"{m}m{s:02d}s"
    h, rem = divmod(total, 3600)
    m = rem // 60
    return f"{h}h{m:02d}m"


def _render_bar(frac: float, width: int) -> str:
    """Render a pv-style bar: '=' fill, '>' leading edge, spaces remaining."""
    frac = min(max(frac, 0.0), 1.0)
    filled = int(round(frac * width))
    if filled <= 0:
        return ">" + " " * (width - 1)
    if filled >= width:
        return "=" * width
    return "=" * (filled - 1) + ">" + " " * (width - filled)


class Indicator:
    """A per-file progress callback for :func:`sumtag.hashing.hash_file`.

    Call ``finish()`` once hashing completes; it clears the line if anything
    was ever shown, so a file that finishes under the threshold leaves no
    trace at all.
    """

    def __init__(self, total: int, si: bool = False) -> None:
        self._total = total
        self._si = si
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

        elapsed = now - self._start
        rate = read / elapsed if elapsed > 0 else 0.0
        frac = (read / self._total) if self._total else 1.0

        size_str = human_size(self._total, self._si)
        rate_str = human_size(rate, self._si) + "/s"
        bar = _render_bar(frac, _BAR_WIDTH)
        elapsed_str = _format_elapsed(elapsed)
        eta_str = _format_eta((self._total - read) / rate) if rate > 0 else "--"

        line = (
            f"{size_str:>{_SIZE_W}}  {rate_str:>{_RATE_W}}  [{bar}] "
            f"{frac * 100:>{_PCT_W}.0f}%  {elapsed_str:>{_ELAPSED_W}}  "
            f"ETA {eta_str:<{_ETA_W}}"
        )
        sys.stderr.write("\r" + line + "\033[K")
        sys.stderr.flush()

    def finish(self) -> None:
        if self._shown:
            sys.stderr.write("\r\033[K")
            sys.stderr.flush()


def make(total: int, enabled: bool, si: bool = False) -> Optional[Indicator]:
    """Return an Indicator for a file of ``total`` bytes, or None if progress
    should not run.

    Suppressed when not requested, and when stderr is not a terminal --
    redrawing a line with carriage returns would just corrupt a redirected
    log or pipe rather than showing anything useful.
    """
    if not enabled or not sys.stderr.isatty():
        return None
    return Indicator(total, si)
