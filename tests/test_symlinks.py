"""Symlinks are not content (CLAUDE.md "Symbolic links", fixed 2026-07-16).

The walker used to yield symlinks: broken ones surfaced as per-file errors
(the --remove "file not found" bug), and live ones were stamped *through*
the link, planting the xattr on the target. Now every mode skips them at
traversal level, silently.
"""

from __future__ import annotations

import contextlib
import io
import os
import tempfile
import unittest

from sumtag import cli, schema, walk, xattr


def _run(argv: list[str]) -> tuple[int, str, str]:
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        code = cli.main(argv)
    return code, out.getvalue(), err.getvalue()


class SymlinkTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tree = os.path.join(self._tmp.name, "tree")
        os.makedirs(os.path.join(self.tree, "sub"))
        self.real = os.path.join(self.tree, "real.bin")
        with open(self.real, "wb") as f:
            f.write(b"content")
        os.symlink("real.bin", os.path.join(self.tree, "live-link"))
        os.symlink("no/such/target", os.path.join(self.tree, "broken-link"))
        os.symlink("sub", os.path.join(self.tree, "dir-link"))

    def test_walker_yields_only_the_real_file(self):
        got = list(walk.iter_files([self.tree]))
        self.assertEqual(got, [self.real])

    def test_sum_and_remove_never_touch_symlinks(self):
        code, _, err = _run(["--sum", "-q", self.tree])
        self.assertEqual(code, 0)
        self.assertEqual(err, "", "broken symlink must not surface as an error")
        self.assertIsNotNone(xattr.get(self.real, schema.XATTR_NAME))
        code, _, err = _run(["--remove", "-q", self.tree])
        self.assertEqual(code, 0)
        self.assertEqual(err, "")
        self.assertIsNone(xattr.get(self.real, schema.XATTR_NAME))

    def test_verify_ignores_symlinks(self):
        _run(["--sum", "-q", self.tree])
        code, out, err = _run(["--verify", self.tree])
        self.assertEqual(code, 0, f"expected clean verify: {out}{err}")
        self.assertNotIn("link", out + err)


if __name__ == "__main__":
    unittest.main()
