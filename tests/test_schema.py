"""Unit tests for the xattr schema module (sumtag.schema).

Independent-oracle tests, same discipline as tests/test_decide.py: expected
values are hand-derived from CLAUDE.md ("Extended attribute schema",
"Timestamp precision") and first principles -- Unix-epoch arithmetic and the
stdlib's own datetime/json act as oracles where a computation is needed --
never transcribed from the module under test.
"""

from __future__ import annotations

import json
import re
import unittest
from datetime import datetime, timezone

import sumtag
from sumtag import schema

#: The documented stamp format: ISO 8601 UTC, exactly six fractional digits,
#: 'Z' suffix (CLAUDE.md "Timestamp precision", e.g. 2026-06-03T10:00:00.123456Z).
ISO_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{6}Z$")


class ConstantsTests(unittest.TestCase):
    """The schema's fixed names, straight from CLAUDE.md."""

    def test_xattr_name(self):
        # "Attribute name: user.sumtag" -- the same literal on both platforms.
        self.assertEqual(schema.XATTR_NAME, "user.sumtag")

    def test_active_algorithm_is_xxh3(self):
        # Today only xxh3 is computed; the constant is the single switch point.
        self.assertEqual(schema.ALGO, "xxh3")

    def test_version_mirrors_the_package(self):
        # The stamped version is the software version (mirrors pyproject).
        self.assertEqual(schema.VERSION, sumtag.__version__)
        self.assertIsInstance(schema.major_of(schema.VERSION), int)


class MajorOfTests(unittest.TestCase):
    """Semver major = the integer before the first dot."""

    def test_hand_cases(self):
        for version, major in [("0.1.0", 0), ("1.2.3", 1), ("10.20.30", 10),
                               ("2.0", 2)]:
            with self.subTest(version=version):
                self.assertEqual(schema.major_of(version), major)


class IsoUtcNsTests(unittest.TestCase):
    """Formatting an epoch-nanosecond time as the stored microsecond stamp."""

    def test_epoch_zero(self):
        # By definition of Unix time: 0 ns is midnight UTC, 1 Jan 1970.
        self.assertEqual(schema.iso_utc_ns(0),
                         "1970-01-01T00:00:00.000000Z")

    def test_documented_example_instant(self):
        # CLAUDE.md's own example stamp: 2026-06-03T10:00:00.123456Z.  The
        # whole-second epoch value comes from the stdlib (an oracle
        # independent of sumtag); the fraction is appended by hand, with
        # trailing nanoseconds that must truncate away (789 ns < 1 us).
        sec = int(datetime(2026, 6, 3, 10, 0, 0,
                           tzinfo=timezone.utc).timestamp())
        ns = sec * 1_000_000_000 + 123_456_789
        self.assertEqual(schema.iso_utc_ns(ns),
                         "2026-06-03T10:00:00.123456Z")

    def test_truncates_never_rounds(self):
        # "Mtime comparisons truncate both ... to the same precision"
        # (CLAUDE.md "Timestamp precision").  999 ns is 0 whole microseconds:
        # rounding up to .000001 would break symmetry with a stored stamp.
        self.assertEqual(schema.iso_utc_ns(999),
                         "1970-01-01T00:00:00.000000Z")
        self.assertEqual(schema.iso_utc_ns(1_999),
                         "1970-01-01T00:00:00.000001Z")

    def test_fixed_width_format(self):
        # Fixed-width stamps are what make lexicographic comparison order as
        # time does (relied on by the re-hash decision).
        for ns in (0, 1, 999_999_999, 1_750_000_000_123_456_789):
            with self.subTest(ns=ns):
                stamp = schema.iso_utc_ns(ns)
                self.assertRegex(stamp, ISO_RE)
                self.assertEqual(len(stamp), 27)

    def test_sub_microsecond_difference_formats_identically(self):
        # Two mtimes differing only below the stored precision must format
        # to the SAME stamp -- the truncation-before-compare rule's basis.
        base = 1_750_000_000_123_456_000
        self.assertEqual(schema.iso_utc_ns(base),
                         schema.iso_utc_ns(base + 999))

    def test_ordering_survives_formatting(self):
        # If a >= 1 us gap separates two instants, their stamps must compare
        # in the same order lexicographically.
        a = schema.iso_utc_ns(1_750_000_000_123_456_000)
        b = schema.iso_utc_ns(1_750_000_000_123_457_000)  # +1 us
        self.assertLess(a, b)


class NowIsoTests(unittest.TestCase):
    def test_format_and_recency(self):
        before = datetime.now(timezone.utc)
        stamp = schema.now_iso()
        after = datetime.now(timezone.utc)
        self.assertRegex(stamp, ISO_RE)
        parsed = datetime.strptime(stamp, "%Y-%m-%dT%H:%M:%S.%fZ").replace(
            tzinfo=timezone.utc)
        # Truncation may floor the stamp below `before` by <1 us; allow 1 s.
        self.assertLessEqual(abs((parsed - before).total_seconds()), 1.0)
        self.assertLessEqual(abs((after - parsed).total_seconds()), 1.0)


class BuildMetaTests(unittest.TestCase):
    T = "2026-06-03T10:00:00.123456Z"
    R = "2026-06-03T09:59:59.000000Z"

    def test_document_shape(self):
        # The five required keys of the CLAUDE.md schema, and only those.
        doc = schema.build_meta({"xxh3": "0123456789abcdef"}, self.T, self.R)
        self.assertEqual(set(doc), {"version", "digests", "file_mtime",
                                    "hashed_at", "run_started_at"})
        self.assertEqual(doc["version"], schema.VERSION)
        self.assertEqual(doc["digests"], {"xxh3": "0123456789abcdef"})
        self.assertEqual(doc["file_mtime"], self.T)      # verbatim passthrough
        self.assertEqual(doc["run_started_at"], self.R)  # verbatim passthrough
        self.assertRegex(doc["hashed_at"], ISO_RE)

    def test_digests_are_copied_not_aliased(self):
        # A fresh document must not alias caller state: mutating the input
        # map afterwards cannot silently rewrite what gets stamped.
        digests = {"xxh3": "0123456789abcdef"}
        doc = schema.build_meta(digests, self.T, self.R)
        digests["xxh3"] = "clobbered"
        self.assertEqual(doc["digests"], {"xxh3": "0123456789abcdef"})


class DumpsLoadsTests(unittest.TestCase):
    def _doc(self) -> dict:
        return schema.build_meta({"xxh3": "0123456789abcdef"},
                                 "2026-06-03T10:00:00.123456Z",
                                 "2026-06-03T09:59:59.000000Z")

    def test_dumps_is_utf8_json_of_the_document(self):
        # "Value: UTF-8 encoded JSON" -- verified with the stdlib as the
        # independent parser.  Byte-exact layout is deliberately NOT pinned.
        # (Build the document ONCE: hashed_at is stamped at build time, so
        # two builds are two different documents.)
        doc = self._doc()
        raw = schema.dumps(doc)
        self.assertIsInstance(raw, bytes)
        self.assertEqual(json.loads(raw.decode("utf-8")), doc)

    def test_round_trip(self):
        doc = self._doc()
        self.assertEqual(schema.loads(schema.dumps(doc)), doc)

    def test_loads_rejects_malformed_bytes(self):
        # A malformed xattr must be distinguishable from a valid one so the
        # caller can treat it exactly like an absent xattr ("absent or
        # unreadable" in the re-hash rules).  ValueError is the contract
        # (UnicodeDecodeError and json's decode error are its subclasses).
        good = self._doc()
        bad_inputs = [
            b"not json at all",
            b"\xff\xfe\x00garbage",          # not UTF-8
            b"[1, 2, 3]",                    # JSON, but not an object
            b'"just a string"',
            schema.dumps({k: v for k, v in good.items() if k != "version"}),
            schema.dumps({k: v for k, v in good.items() if k != "digests"}),
            schema.dumps({k: v for k, v in good.items() if k != "file_mtime"}),
            schema.dumps({k: v for k, v in good.items() if k != "hashed_at"}),
            schema.dumps({k: v for k, v in good.items()
                          if k != "run_started_at"}),
            schema.dumps({**good, "digests": "not-a-map"}),
        ]
        for raw in bad_inputs:
            with self.subTest(raw=raw[:40]):
                with self.assertRaises(ValueError):
                    schema.loads(raw)


if __name__ == "__main__":
    unittest.main()
