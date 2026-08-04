"""End-to-end mode-flow tests run in-process (sumtag.cli.main).

Test-only coverage for documented behaviors no suite pinned yet: the
--remove flow, --verify's stale/unverifiable outcomes and exit codes, the
bare-path/-v announcement split, and the run-summary block.  Authorities:
CLAUDE.md "Removing stamps", "Verification", "Status lines", "Run
summary".  Alignment padding inside lines is cosmetic (the spec's
whitespace note); assertions match labels, paths, and wordings, not
column spacing.
"""

from __future__ import annotations

import contextlib
import io
import os
import tempfile
import unittest

from sumtag import cli, schema, xattr


def run(argv: list[str]) -> tuple[int, str, str]:
    """Run sumtag in-process; return (exit_code, stdout, stderr)."""
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        code = cli.main(argv)
    return code, out.getvalue(), err.getvalue()


def squeeze(text: str) -> str:
    """Collapse runs of spaces: summary labels are padded to a common
    column, and that padding is cosmetic, not contractual."""
    import re
    return re.sub(r" {2,}", " ", text)


class FlowFixture(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = self._tmp.name
        probe = os.path.join(self.root, "probe")
        with open(probe, "w") as f:
            f.write("x")
        try:
            xattr.set(probe, schema.XATTR_NAME, b"p")
            xattr.remove(probe, schema.XATTR_NAME)
        except OSError as e:
            self.skipTest(f"no xattr support: {e}")
        os.remove(probe)

    def _file(self, name: str, content: bytes = b"content") -> str:
        path = os.path.join(self.root, name)
        with open(path, "wb") as f:
            f.write(content)
        return path

    def _stamp(self, *extra: str) -> None:
        code, _, _ = run(["--sum", *extra, self.root])
        assert code == 0

    def _corrupt_silently(self, path: str) -> None:
        ns = os.stat(path).st_mtime_ns
        with open(path, "wb") as f:
            f.write(b"CORRUPTED")
        os.utime(path, ns=(ns, ns))


class RemoveFlowTests(FlowFixture):
    """--remove: strip the xattr; skips gated behind -v; -n previews."""

    def test_remove_strips_stamps_and_reports(self):
        a, b = self._file("a.bin"), self._file("b.bin")
        self._stamp()
        code, out, _ = run(["--remove", self.root])
        self.assertEqual(code, 0)
        self.assertIsNone(xattr.get(a, schema.XATTR_NAME))
        self.assertIsNone(xattr.get(b, schema.XATTR_NAME))
        self.assertIn("removed: 2 stamps", squeeze(out))

    def test_bare_path_announcements_by_default_verbs_with_v(self):
        a = self._file("a.bin")
        self._stamp()
        _, out, _ = run(["--remove", "-v", self.root])
        self.assertIn(f"remove {a}", out)
        # And by default: the bare path, no verb before it.
        self._stamp()
        _, out, _ = run(["--remove", self.root])
        self.assertIn(a, out)
        self.assertNotIn(f"remove {a}", out)

    def test_unstamped_file_skips_silently_unless_verbose(self):
        a = self._file("plain.bin")
        code, out, _ = run(["--remove", self.root])
        self.assertEqual(code, 0)
        self.assertNotIn(a, out)                       # silent by default
        self.assertIn("removed: 0 stamps", squeeze(out))  # headline even at zero
        code, out, _ = run(["--remove", "-v", self.root])
        self.assertIn("skip", out)                     # -v shows the skip
        self.assertIn(a, out)

    def test_dry_run_previews_without_touching(self):
        a = self._file("a.bin")
        self._stamp()
        code, out, _ = run(["--remove", "-n", "-v", self.root])
        self.assertEqual(code, 0)
        self.assertIn(f"would remove {a}", out)
        self.assertIn("would remove: 1 stamp", squeeze(out))  # singular
        self.assertIsNotNone(xattr.get(a, schema.XATTR_NAME))


class VerifyFlowTests(FlowFixture):
    """The stale and unverifiable outcomes (corruption/exit-1 is pinned by
    the conformance harness).
    """

    def test_intact_tree_is_quiet_and_exit_zero(self):
        a = self._file("a.bin")
        self._stamp()
        code, out, _ = run(["--verify", self.root])
        self.assertEqual(code, 0)
        # The announcement is the record; a clean verify adds nothing.
        self.assertIn(a, out)
        self.assertNotIn("CORRUPT", out)
        self.assertIn("verified: 1 file", squeeze(out))

    def test_unverifiable_is_reported_and_exit_two(self):
        a = self._file("unstamped.bin")
        code, out, _ = run(["--verify", self.root])
        self.assertEqual(code, 2)          # the check could not complete
        self.assertIn(f"unverifiable {a}", out)
        self.assertIn("unverifiable: 1 file", squeeze(out))

    def test_stale_is_flagged_not_corruption(self):
        a = self._file("edited.bin")
        self._stamp()
        # A legitimate edit: content and mtime both change.
        with open(a, "wb") as f:
            f.write(b"legitimately edited, longer than before")
        code, out, _ = run(["--verify", self.root])
        self.assertEqual(code, 0)          # stale is not corruption
        self.assertIn("modified since hash; restamp needed", out)
        self.assertIn("stale: 1 file", squeeze(out))

    def test_silent_corruption_line_is_unconditional(self):
        a = self._file("victim.bin")
        self._stamp()
        self._corrupt_silently(a)
        code, out, _ = run(["--verify", self.root])   # no -v: still labeled
        self.assertEqual(code, 1)
        self.assertIn(f"CORRUPT {a}", out)
        self.assertIn("CORRUPT: 1 file", squeeze(out))


class StatusLineTests(FlowFixture):
    """The bare-path/-v split for the stamp pass (CLAUDE.md "Status lines")."""

    def test_default_announcement_is_the_bare_path(self):
        a = self._file("a.bin")
        _, out, _ = run(["--sum", self.root])
        self.assertIn(a + "\n", out)
        self.assertNotIn("hash ", out)

    def test_verbose_announcement_carries_verb_and_reason(self):
        a = self._file("a.bin")
        _, out, _ = run(["--sum", "-v", self.root])
        self.assertIn(f"hash {a} (no usable metadata)", out)

    def test_dry_run_verbose_says_would_hash(self):
        a = self._file("a.bin")
        _, out, _ = run(["--sum", "-n", "-v", self.root])
        self.assertIn(f"would hash {a} (no usable metadata)", out)

    def test_repeat_run_is_completely_silent_by_default(self):
        self._file("a.bin")
        self._stamp()
        _, out, _ = run(["--sum", self.root])
        # Only the summary block remains; no per-file lines at all.
        self.assertNotIn(self.root + os.sep, out.replace("scanned:", ""))
        _, out, _ = run(["--sum", "-v", self.root])
        self.assertIn("skip", out)
        self.assertIn("up-to-date", out)


class SummaryBlockTests(FlowFixture):
    """The end-of-run block: headline even at zero, deviation lines only
    when nonzero, scanned: closes, -q suppresses everything.
    """

    def test_headline_prints_even_at_zero(self):
        code, out, _ = run(["--sum", self.root])   # empty tree
        self.assertEqual(code, 0)
        self.assertIn("hashed: 0 files", squeeze(out))
        self.assertIn(f"scanned: {self.root}", squeeze(out))

    def test_deviation_lines_only_when_nonzero(self):
        self._file("a.bin")
        _, out, _ = run(["--sum", self.root])
        self.assertNotIn("skipped:", out)
        self.assertNotIn("errors:", out)
        _, out, _ = run(["--sum", self.root])      # second run: 1 skip
        self.assertIn("skipped:", out)
        self.assertIn("1 file", out)

    def test_quiet_suppresses_all_normal_output(self):
        self._file("a.bin")
        code, out, err = run(["--sum", "-q", self.root])
        self.assertEqual(code, 0)
        self.assertEqual(out, "")
        self.assertEqual(err, "")

    def test_scanned_lists_roots_as_given(self):
        # The summary echoes the command-line spelling, not a normalization.
        sub = os.path.join(self.root, "sub")
        os.makedirs(sub)
        rel_spelling = os.path.relpath(sub)
        _, out, _ = run(["--sum", rel_spelling])
        self.assertIn(rel_spelling, out)


if __name__ == "__main__":
    unittest.main()
