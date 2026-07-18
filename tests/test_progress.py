"""Unit tests for the --progress indicator's screen discipline.

Covers the two ways a bar fragment could be stranded onscreen (bug fixed
2026-07-17; see BUGS.md):

1. A redraw longer than the terminal wraps onto a second row, after which
   ``\\r`` and EL only reach the continuation row -- so every redraw is
   hard-clamped to the terminal width. Fields *can* overflow their fixed
   budgets (binary human_size renders 9 chars for values in the 1000-1023.9
   band of a unit, vs the 8-wide size / 10-wide rate fields), so the clamp
   is what actually guarantees fit.
2. A read failing mid-hash skipped ``finish()``, leaving the bar onscreen
   with the error line appended to it -- so the engine clears in a finally.
"""

from __future__ import annotations

import io
import os
import sys
import tempfile
import unittest
from unittest import mock

from sumtag import cli, hashing
from sumtag import progress as progress_mod


class _FakeTTY(io.StringIO):
    def isatty(self) -> bool:
        return True


def _drawn_lines(raw: str) -> list[str]:
    """Extract each redrawn line's visible text from captured stderr."""
    lines = []
    for chunk in raw.split("\r"):
        if chunk:
            lines.append(chunk.replace("\033[K", ""))
    return lines


class _WidthFixture(unittest.TestCase):
    """Pin the cached terminal width so tests are terminal-independent."""

    def _pin_width(self, columns: int) -> None:
        self._saved = (progress_mod._line_width, progress_mod._width_stale)
        progress_mod._line_width = columns
        progress_mod._width_stale = False
        self.addCleanup(self._restore)

    def _restore(self) -> None:
        progress_mod._line_width, progress_mod._width_stale = self._saved


class IndicatorClampTests(_WidthFixture):
    def _render(self, total: int, read: int, columns: int = 80) -> str:
        """Force one Indicator redraw past the time threshold; return it."""
        self._pin_width(columns)
        ind = progress_mod.Indicator(total)
        ind._start -= progress_mod.THRESHOLD_SECONDS + 1  # threshold passed
        buf = io.StringIO()
        with mock.patch.object(sys, "stderr", buf):
            ind(read)
        lines = _drawn_lines(buf.getvalue())
        self.assertEqual(len(lines), 1)
        return lines[0]

    def test_normal_line_fills_terminal_exactly(self):
        line = self._render(500 * 1024**2, 250 * 1024**2)
        self.assertEqual(len(line), 80)

    def test_overflowing_size_field_is_clamped_not_wrapped(self):
        # 1010.0MiB renders 9 chars in the 8-wide size field; unclamped,
        # the line would be 81 chars and wrap on an 80-column terminal.
        line = self._render(1010 * 1024**2, 100 * 1024**2)
        self.assertEqual(len(line), 80)

    def test_terminal_narrower_than_fixed_widths_is_clamped(self):
        line = self._render(500 * 1024**2, 250 * 1024**2, columns=40)
        self.assertEqual(len(line), 40)


class CountIndicatorClampTests(_WidthFixture):
    def test_narrow_terminal_is_clamped(self):
        self._pin_width(40)
        ind = progress_mod.CountIndicator(100, "dirs")
        ind._start -= progress_mod.THRESHOLD_SECONDS + 1
        buf = io.StringIO()
        with mock.patch.object(sys, "stderr", buf):
            ind(50)
        (line,) = _drawn_lines(buf.getvalue())
        self.assertEqual(len(line), 40)


class MidHashErrorClearsTests(_WidthFixture):
    """A file whose read fails after the bar has appeared must still get the
    bar cleared before the error line prints (finish() in a finally)."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = self._tmp.name
        self.path = os.path.join(self.root, "a.bin")
        with open(self.path, "wb") as f:
            f.write(b"x" * 1024)
        self._pin_width(80)

    def _run_with_failing_hash(self, argv: list[str]) -> str:
        def fail_mid_read(path, progress=None):
            if progress is not None:
                progress(512)  # force the bar onscreen, then die mid-read
            raise OSError("Input/output error")

        fake_err = _FakeTTY()
        with mock.patch.object(progress_mod, "THRESHOLD_SECONDS", 0.0), \
             mock.patch.object(hashing, "hash_file", fail_mid_read), \
             mock.patch.object(sys, "stderr", fake_err), \
             mock.patch("builtins.print", lambda *a, **k: (
                 fake_err.write(str(a[0]) + "\n")
                 if k.get("file") is fake_err else None)):
            code = cli.main(argv)
        self.assertEqual(code, 2)
        return fake_err.getvalue()

    def _assert_cleared_before_error(self, err: str) -> None:
        self.assertIn("Input/output error", err)
        bar_end = err.rindex("ETA")
        clear = err.find("\r\033[K", bar_end)
        error_at = err.index("sumtag: ", bar_end)
        self.assertTrue(0 <= clear < error_at,
                        "bar was not cleared before the error line:\n"
                        + repr(err))

    def test_stamp_pass_clears_bar_on_read_error(self):
        err = self._run_with_failing_hash(
            ["--sum", "--progress", self.root])
        self._assert_cleared_before_error(err)

    def test_verify_pass_clears_bar_on_read_error(self):
        # Stamp for real first so verify has a digest to check.
        self.assertEqual(cli.main(["--sum", "-q", self.root]), 0)
        err = self._run_with_failing_hash(
            ["--verify", "--progress", self.root])
        self._assert_cleared_before_error(err)


if __name__ == "__main__":
    unittest.main()
