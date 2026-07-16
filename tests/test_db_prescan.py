"""Unit tests for the action-flag requirement and --db-prescan.

Covers CLAUDE.md "Flags" (an action is required; --sum is database-optional)
and "--db-prescan" (one-row summary persisted by --prescan --database,
read back filesystem-free, match-or-error on the full counting context).
"""

from __future__ import annotations

import contextlib
import io
import os
import tempfile
import unittest

from sumtag import cli, store


def _make_tree(root: str, names: dict[str, int]) -> None:
    for rel, size in names.items():
        path = os.path.join(root, rel)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "wb") as f:
            f.write(os.urandom(size))


def _run(argv: list[str]) -> tuple[int, str, str]:
    """Run cli.main capturing both channels; returns (exit, stdout, stderr)."""
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        code = cli.main(argv)
    return code, out.getvalue(), err.getvalue()


class ActionFlagTests(unittest.TestCase):
    """Every run requires an explicit action (decided 2026-07-13)."""

    def test_bare_run_is_an_error(self):
        with self.assertRaises(SystemExit) as ctx:
            _run(["/nonexistent"])
        self.assertEqual(ctx.exception.code, 2)

    def test_modifier_alone_is_an_error(self):
        for argv in (["--prescan", "."], ["-n", "."], ["--force", "."]):
            with self.assertRaises(SystemExit):
                _run(argv)

    def test_sum_no_longer_requires_database(self):
        with tempfile.TemporaryDirectory() as root:
            _make_tree(root, {"a.bin": 64})
            code, out, _ = _run(["--sum", "-q", root])
            self.assertEqual(code, 0)
            from sumtag import xattr, schema
            self.assertIsNotNone(xattr.get(os.path.join(root, "a.bin"),
                                           schema.XATTR_NAME))


class DbPrescanCliTests(unittest.TestCase):
    def test_requires_database(self):
        with self.assertRaises(SystemExit):
            _run(["--sum", "--db-prescan", "."])

    def test_conflicts_with_prescan_and_remove(self):
        with self.assertRaises(SystemExit):
            _run(["--sum", "--database", "x", "--db-prescan", "--prescan", "."])
        with self.assertRaises(SystemExit):
            _run(["--remove", "--database", "x", "--db-prescan", "."])


class SummaryStoreTests(unittest.TestCase):
    def test_roundtrip_and_replacement(self):
        with tempfile.TemporaryDirectory() as d:
            db = os.path.join(d, "db.sqlite")
            s = store.open_store(db)
            s.save_prescan_summary(store.PrescanSummary(
                3, 999, ["/data"], True, False, ["*.vob"], False, "t1"))
            s.save_prescan_summary(store.PrescanSummary(
                7, 111, ["/data"], True, True, [], True, "t2"))
            s.close()
            got = store.read_prescan_summary(db)
            self.assertEqual((got.file_count, got.total_bytes), (7, 111))
            self.assertEqual((got.force, got.no_ignore), (True, True))
            self.assertEqual(got.created_at, "t2")

    def test_read_missing_file_and_missing_table(self):
        with tempfile.TemporaryDirectory() as d:
            missing = os.path.join(d, "nope.sqlite")
            self.assertIsNone(store.read_prescan_summary(missing))
            self.assertFalse(os.path.exists(missing),
                             "read-only open must not create the file")
            # A pre-summary-era database: valid SQLite, no prescan_summary table.
            import sqlite3
            old = os.path.join(d, "old.sqlite")
            sqlite3.connect(old).close()
            self.assertIsNone(store.read_prescan_summary(old))


class DbPrescanFlowTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tree = os.path.join(self._tmp.name, "tree")
        self.db = os.path.join(self._tmp.name, "db.sqlite")
        _make_tree(self.tree, {"a.bin": 100, "b.bin": 200, "sub/c.bin": 300})

    def test_missing_row_is_exit_2_before_any_work(self):
        code, _, err = _run(["--sum", "--database", self.db,
                             "--db-prescan", self.tree])
        self.assertEqual(code, 2)
        self.assertIn("no stored prescan totals", err)

    def test_dry_run_prescan_does_not_persist(self):
        code, _, _ = _run(["--sum", "--database", self.db, "--prescan",
                           "-n", "-q", self.tree])
        self.assertEqual(code, 0)
        self.assertIsNone(store.read_prescan_summary(self.db))

    def test_persist_then_consume_with_announcement(self):
        code, _, _ = _run(["--sum", "--database", self.db, "--prescan",
                           "-q", self.tree])
        self.assertEqual(code, 0)
        got = store.read_prescan_summary(self.db)
        self.assertEqual(got.file_count, 3)
        self.assertEqual(got.total_bytes, 600)
        # Consume: everything is stamped now, so the run ends below mmm.
        code, out, _ = _run(["--sum", "--database", self.db,
                             "--db-prescan", self.tree])
        self.assertEqual(code, 0)
        self.assertIn("using stored prescan totals: 3 files", out)

    def test_counter_prefix_comes_from_stored_totals(self):
        _run(["--sum", "--database", self.db, "--prescan", "-q", self.tree])
        _make_tree(self.tree, {"new.bin": 50})
        code, out, _ = _run(["--sum", "--database", self.db,
                             "--db-prescan", self.tree])
        self.assertEqual(code, 0)
        self.assertIn("1/3", out)  # nnn counts reality against the stored mmm

    def test_mismatches_are_exit_2(self):
        _run(["--sum", "--database", self.db, "--prescan", "-q", self.tree])
        cases = [
            (["--sum", "--force", "--database", self.db, "--db-prescan",
              self.tree], "--force differs"),
            (["--sum", "--database", self.db, "--db-prescan",
              "--exclude", "*.bin", self.tree], "--exclude patterns differ"),
            (["--sum", "--database", self.db, "--db-prescan",
              "--no-ignore", self.tree], "--no-ignore differs"),
            (["--sum", "--database", self.db, "--db-prescan",
              self._tmp.name], "different scan roots"),
        ]
        for argv, expected in cases:
            code, _, err = _run(argv)
            self.assertEqual(code, 2, f"expected exit 2 for {expected}")
            self.assertIn(expected, err)

    def test_roots_compared_normalized(self):
        _run(["--sum", "--database", self.db, "--prescan", "-q", self.tree])
        # Same directory spelled differently: trailing slash and a dot segment.
        code, _, err = _run(["--sum", "--database", self.db, "--db-prescan",
                             self.tree + os.sep,
                             os.path.join(self.tree, ".")])
        self.assertEqual(code, 0, f"normalized respelling should match: {err}")


if __name__ == "__main__":
    unittest.main()
