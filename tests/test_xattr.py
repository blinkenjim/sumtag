"""Unit tests for the platform-abstracted xattr layer (sumtag.xattr).

Independent-oracle tests against a real temporary file.  The contract, from
CLAUDE.md "Language & dependencies" and the layer's documented surface: a
byte-oriented get/set/remove identical on both platforms, where an absent
attribute reads back as None and remove() reports whether there was
anything to delete.  The tests are platform-neutral -- they exercise
whichever branch (macOS ctypes / Linux os.*xattr) this machine runs.
"""

from __future__ import annotations

import os
import tempfile
import unittest

from sumtag import xattr
from sumtag.schema import XATTR_NAME


class XattrTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.path = os.path.join(self._tmp.name, "f.bin")
        with open(self.path, "wb") as f:
            f.write(b"content")
        # Skip (never fail) where the filesystem has no xattr support.
        try:
            xattr.set(self.path, XATTR_NAME, b"probe")
            xattr.remove(self.path, XATTR_NAME)
        except OSError as e:
            self.skipTest(f"no xattr support on this filesystem: {e}")

    def test_absent_attribute_reads_none(self):
        self.assertIsNone(xattr.get(self.path, XATTR_NAME))

    def test_set_get_round_trip(self):
        xattr.set(self.path, XATTR_NAME, b'{"k": "v"}')
        self.assertEqual(xattr.get(self.path, XATTR_NAME), b'{"k": "v"}')

    def test_binary_safety(self):
        # Byte-oriented means BYTES: no encoding assumptions, embedded NULs
        # and non-UTF-8 sequences survive verbatim.
        blob = b"\x00\xff\xfe" + bytes(range(32)) + b"\x00tail"
        xattr.set(self.path, XATTR_NAME, blob)
        self.assertEqual(xattr.get(self.path, XATTR_NAME), blob)

    def test_set_overwrites(self):
        xattr.set(self.path, XATTR_NAME, b"first")
        xattr.set(self.path, XATTR_NAME, b"second, longer than first")
        self.assertEqual(xattr.get(self.path, XATTR_NAME),
                         b"second, longer than first")
        xattr.set(self.path, XATTR_NAME, b"3rd")  # shorter, too
        self.assertEqual(xattr.get(self.path, XATTR_NAME), b"3rd")

    def test_empty_value_is_distinct_from_absent(self):
        # A zero-length value is a present attribute (b""), not None.
        xattr.set(self.path, XATTR_NAME, b"")
        self.assertEqual(xattr.get(self.path, XATTR_NAME), b"")

    def test_remove_present_returns_true_and_deletes(self):
        xattr.set(self.path, XATTR_NAME, b"x")
        self.assertIs(xattr.remove(self.path, XATTR_NAME), True)
        self.assertIsNone(xattr.get(self.path, XATTR_NAME))

    def test_remove_absent_returns_false(self):
        # The --remove skip case: nothing there is a report, not an error.
        self.assertIs(xattr.remove(self.path, XATTR_NAME), False)

    def test_missing_file_raises_oserror(self):
        ghost = os.path.join(self._tmp.name, "no-such-file")
        with self.assertRaises(OSError):
            xattr.get(ghost, XATTR_NAME)
        with self.assertRaises(OSError):
            xattr.set(ghost, XATTR_NAME, b"x")
        with self.assertRaises(OSError):
            xattr.remove(ghost, XATTR_NAME)

    def test_non_ascii_path(self):
        # Paths go through os.fsencode on the ctypes branch; a non-ASCII
        # filename must work identically.
        path = os.path.join(self._tmp.name, "påth-日本語.bin")
        with open(path, "wb") as f:
            f.write(b"x")
        xattr.set(path, XATTR_NAME, b"v")
        self.assertEqual(xattr.get(path, XATTR_NAME), b"v")
        self.assertIs(xattr.remove(path, XATTR_NAME), True)


if __name__ == "__main__":
    unittest.main()
