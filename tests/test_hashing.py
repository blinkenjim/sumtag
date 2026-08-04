"""Unit tests for the streaming hasher (sumtag.hashing.hash_file).

Independent oracles: the publicly known XXH3-64 empty-input digest
(2d06800538d394c2 -- independently recorded in grouper's EMPTY_DIGESTS
constant for the --top empties filter), the stdlib's hashlib.md5 for the
md5 branch, and the one-shot library digest of the whole content for the
streaming contract -- hash_file's job is CHUNKED reading in bounded
memory, so chunked-equals-one-shot across chunk-boundary sizes is exactly
the property that can catch a streaming bug (a dropped or double-fed
chunk) that no single small file would.
"""

from __future__ import annotations

import hashlib
import os
import tempfile
import unittest

import xxhash

from sumtag import hashing, schema

CHUNK = 1 << 20  # the documented 1 MiB read size; boundary cases build on it
EMPTY_XXH3 = "2d06800538d394c2"


class HashFileTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)

    def _file(self, content: bytes) -> str:
        path = os.path.join(self._tmp.name, "f.bin")
        with open(path, "wb") as f:
            f.write(content)
        return path

    def test_empty_file_is_the_known_constant(self):
        self.assertEqual(hashing.hash_file(self._file(b"")), EMPTY_XXH3)

    def test_digest_is_16_lowercase_hex(self):
        # The xattr schema's stated shape: 64-bit XXH3, 16-char lowercase hex.
        digest = hashing.hash_file(self._file(b"hello"))
        self.assertRegex(digest, r"^[0-9a-f]{16}$")

    def test_streaming_equals_one_shot_across_chunk_boundaries(self):
        # Sizes straddling the 1 MiB read size: below, exactly one chunk,
        # one byte over, several chunks plus a tail. A streaming bug that
        # drops, reorders, or double-feeds a chunk shows up here.
        for size in (1, CHUNK - 1, CHUNK, CHUNK + 1, 2 * CHUNK + 12345):
            with self.subTest(size=size):
                content = os.urandom(size)
                self.assertEqual(hashing.hash_file(self._file(content)),
                                 xxhash.xxh3_64_hexdigest(content))

    def test_md5_branch_matches_the_stdlib(self):
        content = b"md5 oracle content"
        self.assertEqual(hashing.hash_file(self._file(content), "md5"),
                         hashlib.md5(content).hexdigest())

    def test_unsupported_algorithm_raises_valueerror(self):
        path = self._file(b"x")
        with self.assertRaises(ValueError):
            hashing.hash_file(path, "sha999")

    def test_default_algorithm_follows_schema_algo_at_call_time(self):
        # The single-switch-point contract: flipping schema.ALGO redirects
        # hash_file without re-importing anything.
        path = self._file(b"switch test")
        original = schema.ALGO
        try:
            schema.ALGO = "md5"
            self.assertEqual(hashing.hash_file(path),
                             hashlib.md5(b"switch test").hexdigest())
        finally:
            schema.ALGO = original
        self.assertEqual(hashing.hash_file(path),
                         xxhash.xxh3_64_hexdigest(b"switch test"))

    def test_progress_callback_reports_cumulative_bytes(self):
        # The callback drives --progress: it must see strictly increasing
        # cumulative totals, one per chunk read, ending at the file size.
        size = 2 * CHUNK + 777
        path = self._file(os.urandom(size))
        seen: list[int] = []
        hashing.hash_file(path, progress=seen.append)
        self.assertEqual(seen, sorted(seen))          # cumulative, increasing
        self.assertEqual(seen[-1], size)              # ends at the full size
        self.assertEqual(len(seen), 3)                # one call per chunk
        self.assertEqual(seen[0], CHUNK)              # bounded-memory reads

    def test_no_progress_calls_for_an_empty_file(self):
        seen: list[int] = []
        hashing.hash_file(self._file(b""), progress=seen.append)
        self.assertEqual(seen, [])

    def test_missing_file_raises_oserror(self):
        with self.assertRaises(OSError):
            hashing.hash_file(os.path.join(self._tmp.name, "ghost"))


if __name__ == "__main__":
    unittest.main()
