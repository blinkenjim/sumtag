"""Unit tests for the engine's small helpers (sumtag.engine).

Independent-oracle tests for the I/O-light pieces the mode flows lean on.
Authorities: CLAUDE.md "--prescan" (the counter-prefix format, including
its own worked example line), "--db-prescan" (match-or-error wording), the
locate-columns schema, and the re-hash rules.  The mode flows themselves
(_stamp/_verify/_prune_dirs) stay covered by the flow suites and the
conformance harness -- per the re-code plan they are test-only, never
rewritten.
"""

from __future__ import annotations

import argparse
import os
import tempfile
import unittest

from sumtag import engine, schema, xattr
from sumtag.store import PrescanSummary


class PctTests(unittest.TestCase):
    """CLAUDE.md "--prescan": every fraction carries its whole-number
    percentage in parens, right-padded to three digits -- (  0%) through
    (100%) -- so the token keeps one width; under --db-prescan drift it may
    pass 100 and simply widen.
    """

    def test_fixed_width_band(self):
        self.assertEqual(engine._pct(0, 100), "(  0%)")
        self.assertEqual(engine._pct(5, 100), "(  5%)")
        self.assertEqual(engine._pct(31, 100), "( 31%)")
        self.assertEqual(engine._pct(100, 100), "(100%)")

    def test_documented_example_fraction(self):
        # The doc's own line reads "042/137 ( 31%)": 42/137 = 30.66,
        # rendered as the whole number 31.
        self.assertEqual(engine._pct(42, 137), "( 31%)")

    def test_zero_total_reads_complete(self):
        # Same convention as the progress bar: nothing to do is 100% done.
        self.assertEqual(engine._pct(0, 0), "(100%)")

    def test_drift_past_100_widens_harmlessly(self):
        self.assertEqual(engine._pct(150, 100), "(150%)")


class PrescanPrefixTests(unittest.TestCase):
    """The full counter prefix: nnn zero-padded to mmm's width, both
    fractions with their percentages, two-space separators, trailing
    separator before the announcement.
    """

    def test_reproduces_the_documented_example_line(self):
        # CLAUDE.md's worked example: "042/137 ( 31%)  118.2MiB/4.2GiB
        # (  3%)  <announcement>".  Byte values chosen to render exactly
        # those human sizes; their ratio is 2.7%, shown as 3.
        so_far = int(118.2 * 1024**2)
        total = int(4.2 * 1024**3)
        prefix = engine._prescan_prefix(42, 137, so_far, total, si=False)
        self.assertEqual(prefix, "042/137 ( 31%)  118.2MiB/4.2GiB (  3%)  ")

    def test_first_file_reads_zero_bytes(self):
        # bytes-so-far sums COMPLETED files, "so it reads 0B on the very
        # first file".
        prefix = engine._prescan_prefix(1, 9, 0, 1024, si=False)
        self.assertEqual(prefix, "1/9 ( 11%)  0B/1.0KiB (  0%)  ")

    def test_ordinal_zero_pads_to_the_total_width(self):
        self.assertTrue(engine._prescan_prefix(7, 137, 0, 1, si=False)
                        .startswith("007/137 "))
        self.assertTrue(engine._prescan_prefix(7, 9, 0, 1, si=False)
                        .startswith("7/9 "))

    def test_si_flag_flows_to_the_byte_figures(self):
        prefix = engine._prescan_prefix(1, 1, 1000, 1000, si=True)
        self.assertIn("1.0kB/1.0kB", prefix)


class StatDataTests(unittest.TestCase):
    """os.stat_result -> the locate columns, timestamps in the schema's ISO
    form, birthtime only where the platform provides it.
    """

    def test_maps_a_real_stat(self):
        with tempfile.NamedTemporaryFile() as f:
            f.write(b"x" * 321)
            f.flush()
            st = os.stat(f.name)
            sd = engine._stat_data(st)
        self.assertEqual(sd.size, 321)
        self.assertEqual(sd.mode, st.st_mode)
        self.assertEqual(sd.uid, st.st_uid)
        self.assertEqual(sd.gid, st.st_gid)
        self.assertEqual(sd.nlink, st.st_nlink)
        self.assertEqual(sd.dev, st.st_dev)
        # Times are the schema's own formatting of the ns fields.
        self.assertEqual(sd.ctime, schema.iso_utc_ns(st.st_ctime_ns))
        self.assertEqual(sd.atime, schema.iso_utc_ns(st.st_atime_ns))
        if hasattr(st, "st_birthtime"):   # macOS
            self.assertIsNotNone(sd.birthtime)
            self.assertRegex(sd.birthtime,
                             r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{6}Z$")
        else:                             # Linux and others
            self.assertIsNone(sd.birthtime)


class ReadMetaTests(unittest.TestCase):
    """The absent/unreadable/malformed collapse: every failure mode reads
    as None, so the decision layer sees exactly two shapes -- a valid
    document or nothing.
    """

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.path = os.path.join(self._tmp.name, "f.bin")
        with open(self.path, "w") as f:
            f.write("x")
        try:
            xattr.set(self.path, schema.XATTR_NAME, b"probe")
            xattr.remove(self.path, schema.XATTR_NAME)
        except OSError as e:
            self.skipTest(f"no xattr support: {e}")

    def test_absent_is_none(self):
        self.assertIsNone(engine._read_meta(self.path))

    def test_valid_document_round_trips(self):
        doc = schema.build_meta({"xxh3": "0123456789abcdef"},
                                "2026-08-01T00:00:00.000000Z",
                                "2026-08-01T00:00:00.000000Z")
        xattr.set(self.path, schema.XATTR_NAME, schema.dumps(doc))
        self.assertEqual(engine._read_meta(self.path), doc)

    def test_malformed_bytes_are_none(self):
        for raw in (b"not json", b"[1,2]", b'{"version": "0.1.0"}'):
            with self.subTest(raw=raw):
                xattr.set(self.path, schema.XATTR_NAME, raw)
                self.assertIsNone(engine._read_meta(self.path))


class NormalizedRootsTests(unittest.TestCase):
    """Roots as a sorted, deduplicated list of absolute paths -- so /data,
    /data/, and a relative respelling never read as different roots
    (CLAUDE.md "--db-prescan" match rule).
    """

    def test_sorted_deduped_absolute(self):
        self.assertEqual(engine._normalized_roots(["/data/", "/data",
                                                   "/backup"]),
                         ["/backup", "/data"])

    def test_relative_respelling_matches_absolute(self):
        cwd = os.getcwd()
        self.assertEqual(engine._normalized_roots(["."]),
                         engine._normalized_roots([cwd]))


def _args(**overrides) -> argparse.Namespace:
    base = dict(directories=["/data"], sum=True, force=False,
                exclude=[], no_ignore=False)
    base.update(overrides)
    return argparse.Namespace(**base)


def _summary(**overrides) -> PrescanSummary:
    base = dict(file_count=137, total_bytes=4200,
                roots=["/data"], sum_mode=True, force=False, exclude=[],
                no_ignore=False, created_at="2026-08-04T00:00:00.000000Z")
    base.update(overrides)
    return PrescanSummary(**base)


class SummaryMismatchTests(unittest.TestCase):
    """--db-prescan's match-or-error: the stored totals are used only if
    every field that changed which files got counted equals this run's --
    each mismatch named with its documented wording.
    """

    def test_full_match_is_none(self):
        self.assertIsNone(engine._summary_mismatch(_summary(), _args()))

    def test_root_respellings_still_match(self):
        # /data vs /data/ "never falsely mismatches".
        self.assertIsNone(engine._summary_mismatch(
            _summary(roots=["/data"]), _args(directories=["/data/"])))

    def test_each_differing_field_is_named(self):
        cases = [
            (_summary(roots=["/other"]), _args(), "different scan roots"),
            (_summary(sum_mode=False), _args(), "different action"),
            (_summary(force=True), _args(), "--force differs"),
            (_summary(exclude=["*.vob"]), _args(),
             "--exclude patterns differ"),
            (_summary(no_ignore=True), _args(), "--no-ignore differs"),
        ]
        for summary, args, expected in cases:
            with self.subTest(expected=expected):
                self.assertEqual(engine._summary_mismatch(summary, args),
                                 expected)

    def test_exclude_comparison_is_order_insensitive(self):
        # The same patterns in a different order counted the same files.
        self.assertIsNone(engine._summary_mismatch(
            _summary(exclude=["*.vob", "*.iso"]),
            _args(exclude=["*.iso", "*.vob"])))


class HashDecisionTests(unittest.TestCase):
    """The one branch the stamp pass and --prescan's counting pass must
    agree on: standard mode delegates to the re-hash rules; import/locate-
    only mode never computes unless --force overrides.
    """

    LIVE = "2026-08-01T00:00:00.000000Z"

    def test_standard_mode_delegates_to_should_rehash(self):
        # meta=None must re-hash in standard mode (the no-metadata rule).
        rehash, _ = engine._hash_decision(None, self.LIVE, _args(force=False),
                                          0, use_standard_decision=True)
        self.assertTrue(rehash)

    def test_import_only_mode_refuses_to_compute(self):
        # Even a file with NO metadata is not hashed: --import's whole job
        # is to never read contents.
        rehash, reason = engine._hash_decision(
            None, self.LIVE, _args(force=False), 0,
            use_standard_decision=False)
        self.assertFalse(rehash)
        self.assertEqual(reason, "not computing (--import/--locate only)")

    def test_force_overrides_the_import_refusal(self):
        # "--force --import re-hashes every file and mirrors the result."
        rehash, reason = engine._hash_decision(
            None, self.LIVE, _args(force=True), 0,
            use_standard_decision=False)
        self.assertTrue(rehash)
        self.assertEqual(reason, "forced")


if __name__ == "__main__":
    unittest.main()
