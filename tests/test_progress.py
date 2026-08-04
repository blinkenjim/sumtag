"""Unit tests for the --progress indicator's screen discipline.

Covers the two ways a bar fragment could be stranded onscreen (bug fixed
2026-07-17; see BUGS.md):

1. A redraw longer than the terminal wraps onto a second row, after which
   ``\\r`` and EL only reach the continuation row -- so every redraw is
   hard-clamped to the terminal width. Belt and suspenders since
   2026-07-19: human_size now drops the decimal on a four-digit mantissa
   (the 1000-1023.9 band of a binary unit, e.g. a DVD VOB's 1023.8MiB),
   so the size/rate fields always fit their fixed budgets and a normal
   line never needs the clamp -- which remains the hard guarantee for
   terminals narrower than the ~52-char fixed budget (HumanSizeTests
   pins the field-fit half).
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
from sumtag.progress import _RATE_W, _SIZE_W, human_size


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

    def test_vob_band_size_fits_without_the_clamp(self):
        # 1010.0MiB used to render 9 chars in the 8-wide size field and
        # need the clamp; human_size now drops the decimal (1010MiB), so
        # the line comes out at exactly terminal width on its own.
        line = self._render(1010 * 1024**2, 100 * 1024**2)
        self.assertEqual(len(line), 80)
        self.assertTrue(line.startswith(" 1010MiB"), line)
        self.assertIn("ETA", line)  # tail not truncated away

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


class HumanSizeTests(unittest.TestCase):
    """human_size must fit the fixed field widths (the 2026-07-19 half of
    the stale-bar fix: with every size and rate inside its budget, a normal
    redraw never relies on the clamp and the ETA tail is never truncated)."""

    def test_ordinary_values_keep_one_decimal(self):
        self.assertEqual(human_size(40.1 * 1024**2, si=False), "40.1MiB")
        self.assertEqual(human_size(99.3 * 1024**3, si=False), "99.3GiB")
        self.assertEqual(human_size(999.9 * 1024**2, si=False), "999.9MiB")

    def test_bytes_have_no_decimal(self):
        self.assertEqual(human_size(0, si=False), "0B")
        self.assertEqual(human_size(1023, si=False), "1023B")

    def test_four_digit_mantissa_drops_decimal(self):
        # The VOB band: >= 1000 of a binary unit but below the 1024
        # promotion point.
        self.assertEqual(human_size(1023.8 * 1024**2, si=False), "1024MiB")
        self.assertEqual(human_size(1000.0 * 1024**2, si=False), "1000MiB")
        self.assertEqual(human_size(1010.3 * 1024**3, si=False), "1010GiB")

    def test_rounding_up_to_four_digits_drops_decimal(self):
        # 999.96MiB renders as "1000.0" under .1f; the width check must
        # catch the *formatted* text, not the pre-rounding value.
        self.assertEqual(human_size(999.96 * 1024**2, si=False), "1000MiB")

    def test_si_units_unaffected(self):
        # Base-1000 promotes at 1000, so SI mantissas never reach 4 digits.
        self.assertEqual(human_size(999.9 * 1000**2, si=True), "999.9MB")
        self.assertEqual(human_size(1000.0 * 1000**2, si=True), "1.0GB")

    def test_fits_fixed_field_widths(self):
        # The invariant the display depends on: every size fits _SIZE_W
        # and every rate (size + "/s") fits _RATE_W, across the whole
        # range up to the PiB promotion point.
        step = 511  # prime-ish stride for coverage without a slow loop
        for mib in range(0, 1024 * 1024, step):
            for si in (False, True):
                text = human_size(mib * 1024**2, si=si)
                self.assertLessEqual(len(text), _SIZE_W, text)
                self.assertLessEqual(len(text) + 2, _RATE_W, text + "/s")


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


class HumanSizeUnitTableTests(unittest.TestCase):
    """Independent-oracle additions (re-code batch 3): the unit tables and
    promotion points, hand-derived from CLAUDE.md "--si" / "Line format" --
    binary powers-of-1024 KiB/MiB/GiB by default, decimal powers-of-1000
    kB/MB/GB with --si.  (HumanSizeTests above already pins the doc's own
    example values and the four-digit-band rule.)
    """

    def test_binary_unit_progression(self):
        # Promotion happens exactly at each power of 1024.
        cases = [
            (1, "1B"),
            (1023, "1023B"),
            (1024, "1.0KiB"),
            (1536, "1.5KiB"),          # 1.5 * 1024, exact
            (1024**2, "1.0MiB"),
            (1024**3, "1.0GiB"),
            (1024**4, "1.0TiB"),
            (1024**5, "1.0PiB"),
        ]
        for n, expected in cases:
            with self.subTest(n=n):
                self.assertEqual(human_size(n, si=False), expected)

    def test_si_unit_progression(self):
        # Base-1000 promotion, lowercase k per SI.
        cases = [
            (999, "999B"),
            (1000, "1.0kB"),
            (1500, "1.5kB"),
            (1000**2, "1.0MB"),
            (1000**3, "1.0GB"),
            (1000**4, "1.0TB"),
            (1000**5, "1.0PB"),
        ]
        for n, expected in cases:
            with self.subTest(n=n):
                self.assertEqual(human_size(n, si=True), expected)

    def test_top_unit_never_promotes_past_the_table(self):
        # There is no unit above PiB/PB: the mantissa just grows (and the
        # four-digit rule drops its decimal).
        self.assertEqual(human_size(1024**6, si=False), "1024PiB")
        self.assertEqual(human_size(1000**6, si=True), "1000PB")


class FormatElapsedTests(unittest.TestCase):
    """CLAUDE.md "Line format": elapsed is H:MM:SS (a fact, clock-styled).
    Hand-derived arithmetic; fractional seconds truncate (a stopwatch shows
    whole seconds elapsed, never rounds a second up before it has passed).
    """

    def test_hand_cases(self):
        cases = [
            (0, "0:00:00"),
            (59, "0:00:59"),
            (60, "0:01:00"),
            (65, "0:01:05"),
            (312, "0:05:12"),          # the CLAUDE.md example line's 0:05:12
            (3599, "0:59:59"),
            (3600, "1:00:00"),
            (5 * 3600 + 7 * 60 + 9, "5:07:09"),
        ]
        for seconds, expected in cases:
            with self.subTest(seconds=seconds):
                self.assertEqual(progress_mod._format_elapsed(seconds),
                                 expected)

    def test_fractional_seconds_truncate(self):
        self.assertEqual(progress_mod._format_elapsed(59.94), "0:00:59")


class FormatEtaTests(unittest.TestCase):
    """CLAUDE.md "Line format": eta is a compact human duration -- its own
    three documented forms "45s", "5m12s", "1h05m" -- deliberately distinct
    from elapsed's clock style.  Boundaries hand-derived: the minute form
    takes over at 60s, the hour form at 3600s; sub-hour seconds and
    sub-day minutes are zero-padded to two digits (the doc's "1h05m" shows
    the padding; "5m12s" is consistent with it).
    """

    def test_documented_forms(self):
        self.assertEqual(progress_mod._format_eta(45), "45s")
        self.assertEqual(progress_mod._format_eta(5 * 60 + 12), "5m12s")
        self.assertEqual(progress_mod._format_eta(3600 + 5 * 60), "1h05m")

    def test_boundaries(self):
        cases = [
            (0, "0s"),
            (59, "59s"),
            (60, "1m00s"),
            (3599, "59m59s"),
            (3600, "1h00m"),
            (26 * 3600 + 30 * 60, "26h30m"),  # no day unit; hours just grow
        ]
        for seconds, expected in cases:
            with self.subTest(seconds=seconds):
                self.assertEqual(progress_mod._format_eta(seconds), expected)


class RenderBarTests(unittest.TestCase):
    """CLAUDE.md "Line format": pv-style bar -- '=' fill, '>' leading edge,
    spaces for the remainder.  The bar is ephemeral tty output, so these are
    structural properties (hand-derived from that description), not pixel
    pins: exact rounding of the fill boundary is pinned separately as
    characterization.
    """

    def test_empty_and_full(self):
        # No fill: the leading edge sits at the far left.  Full: solid
        # '=' wall, no edge left to lead with.
        self.assertEqual(progress_mod._render_bar(0.0, 10), ">" + " " * 9)
        self.assertEqual(progress_mod._render_bar(1.0, 10), "=" * 10)

    def test_structure_at_every_width_and_fraction(self):
        for width in (1, 2, 5, 28, 80):
            for pct in range(0, 101, 7):
                frac = pct / 100
                with self.subTest(width=width, frac=frac):
                    bar = progress_mod._render_bar(frac, width)
                    self.assertEqual(len(bar), width)         # exact width
                    self.assertRegex(bar, r"^=*>? *$")        # =s, edge, spaces
                    self.assertLessEqual(bar.count(">"), 1)   # one edge at most

    def test_fill_is_monotonic_in_frac(self):
        # More progress can never render as less bar: the width of the
        # non-space prefix (fill plus edge) never shrinks as frac grows.
        widths = [len(progress_mod._render_bar(p / 100, 28).rstrip())
                  for p in range(0, 101, 5)]
        self.assertEqual(widths, sorted(widths))

    def test_out_of_range_fractions_clamp(self):
        # Defensive: a rate hiccup must never render a torn bar.
        self.assertEqual(progress_mod._render_bar(-0.5, 10),
                         progress_mod._render_bar(0.0, 10))
        self.assertEqual(progress_mod._render_bar(1.5, 10),
                         progress_mod._render_bar(1.0, 10))

    def test_rounding_characterization(self):
        # CHARACTERIZATION ONLY: the fill boundary rounds to nearest
        # (int(round(frac * width))).  Nothing documents this choice; it is
        # pinned so a re-code keeps redraws visually identical, not because
        # nearest-rounding is more correct than floor.
        self.assertEqual(progress_mod._render_bar(0.84, 28),
                         "=" * 23 + ">" + " " * 4)   # 23.52 -> 24 filled
        self.assertEqual(progress_mod._render_bar(0.05, 10),
                         ">" + " " * 9)              # 0.5 -> round-half-even 0


if __name__ == "__main__":
    unittest.main()
