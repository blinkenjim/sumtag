"""Unit tests for --prune-dirs.

Covers CLAUDE.md "Pruning stale database rows": directory-level
reconciliation of the database against a changed filesystem -- dirname-
equality deletes, no recursion (vanished subdirectories are found by their
own checks), the unmounted-drive guard, -n preview, and the verify-style
0/1/2 exit codes.
"""

from __future__ import annotations

import contextlib
import io
import os
import shutil
import sqlite3
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


def _db_rel_paths(db: str) -> set[str]:
    conn = sqlite3.connect(db)
    rows = {r[0] for r in conn.execute("SELECT rel_path FROM files")}
    conn.close()
    return rows


class PruneDirsCliTests(unittest.TestCase):
    """The flag-conflict matrix (CLAUDE.md "Flags")."""

    def test_requires_database(self):
        with self.assertRaises(SystemExit):
            _run(["--prune-dirs", "."])

    def test_conflicts(self):
        for extra in (["--sum"], ["--import"], ["--locate"], ["--verify"],
                      ["--remove"], ["--force"], ["--prescan"],
                      ["--db-prescan"], ["--exclude", "*.vob"],
                      ["--no-ignore"]):
            with self.assertRaises(SystemExit, msg=f"expected error: {extra}"):
                _run(["--prune-dirs", "--database", "x"] + extra + ["."])

    def test_counts_as_the_required_action(self):
        # Reaches the engine (missing db -> exit 2), not an argparse error.
        code, _, err = _run(["--prune-dirs", "--database", "/nonexistent.db",
                             "."])
        self.assertEqual(code, 2)
        self.assertIn("no such database", err)


class PruneDirsFlowTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tree = os.path.join(self._tmp.name, "tree")
        self.db = os.path.join(self._tmp.name, "db.sqlite")
        _make_tree(self.tree, {
            "keep/a.bin": 10, "keep/b.bin": 10,
            "gone/c.bin": 10,
            "parent/d.bin": 10, "parent/child/e.bin": 10,
        })
        code, _, _ = _run(["--sum", "-q", "--database", self.db, self.tree])
        self.assertEqual(code, 0)

    def _rel(self, sub: str) -> str:
        """The stored rel_path prefix for a subpath of the test tree."""
        mount, rel = store.mount_relative(os.path.join(self.tree, sub))
        return rel

    def test_nothing_stale_is_exit_0(self):
        code, _, _ = _run(["--prune-dirs", "-q", "--database", self.db,
                           self.tree])
        self.assertEqual(code, 0)
        self.assertEqual(len(_db_rel_paths(self.db)), 5)

    def test_deleted_dir_pruned_exit_1(self):
        shutil.rmtree(os.path.join(self.tree, "gone"))
        code, out, _ = _run(["--prune-dirs", "--database", self.db,
                             self.tree])
        self.assertEqual(code, 1)
        rels = _db_rel_paths(self.db)
        self.assertEqual(len(rels), 4)
        self.assertFalse(any(r.endswith("c.bin") for r in rels))
        self.assertIn("pruned:", out)
        self.assertIn("1 directory, 1 file row", out)
        # A second run finds nothing stale.
        code, _, _ = _run(["--prune-dirs", "-q", "--database", self.db,
                           self.tree])
        self.assertEqual(code, 0)

    def test_deleted_parent_prunes_children_via_their_own_checks(self):
        shutil.rmtree(os.path.join(self.tree, "parent"))
        code, out, _ = _run(["--prune-dirs", "--database", self.db,
                             self.tree])
        self.assertEqual(code, 1)
        rels = _db_rel_paths(self.db)
        self.assertEqual(len(rels), 3)  # keep/ and gone/ survive
        self.assertFalse(any("parent" in r for r in rels))
        self.assertIn("2 directories, 2 file rows", out)

    def test_dir_replaced_by_file_is_pruned(self):
        shutil.rmtree(os.path.join(self.tree, "gone"))
        with open(os.path.join(self.tree, "gone"), "w") as f:
            f.write("now a file")
        code, _, _ = _run(["--prune-dirs", "-q", "--database", self.db,
                           self.tree])
        self.assertEqual(code, 1)
        self.assertEqual(len(_db_rel_paths(self.db)), 4)

    def test_dry_run_previews_without_deleting(self):
        shutil.rmtree(os.path.join(self.tree, "gone"))
        code, out, _ = _run(["--prune-dirs", "-n", "--database", self.db,
                             "-v", self.tree])
        self.assertEqual(code, 1)  # would-prune gates the same way
        self.assertIn("would prune", out)
        self.assertEqual(len(_db_rel_paths(self.db)), 5)  # nothing deleted

    def test_dry_run_never_creates_the_database(self):
        missing = os.path.join(self._tmp.name, "nope.sqlite")
        code, _, err = _run(["--prune-dirs", "-n", "--database", missing,
                             self.tree])
        self.assertEqual(code, 2)
        self.assertIn("no such database", err)
        self.assertFalse(os.path.exists(missing))

    def test_missing_root_is_exit_2_before_any_deletion(self):
        shutil.rmtree(os.path.join(self.tree, "gone"))
        code, _, err = _run(["--prune-dirs", "--database", self.db,
                             os.path.join(self._tmp.name, "not-mounted")])
        self.assertEqual(code, 2)
        self.assertIn("is the drive mounted?", err)
        self.assertEqual(len(_db_rel_paths(self.db)), 5)  # untouched

    def test_root_with_no_rows_warns_and_exits_0(self):
        empty = os.path.join(self._tmp.name, "elsewhere")
        os.makedirs(empty)
        code, _, err = _run(["--prune-dirs", "--database", self.db, empty])
        self.assertEqual(code, 0)
        self.assertIn("no database rows under this root", err)

    def test_prune_scoped_to_the_given_root(self):
        # Deleting gone/ but scanning only keep/ must not prune gone's rows.
        shutil.rmtree(os.path.join(self.tree, "gone"))
        code, _, _ = _run(["--prune-dirs", "-q", "--database", self.db,
                           os.path.join(self.tree, "keep")])
        self.assertEqual(code, 0)
        self.assertEqual(len(_db_rel_paths(self.db)), 5)

    def test_summary_reports_checked_and_scanned(self):
        code, out, _ = _run(["--prune-dirs", "--database", self.db,
                             self.tree])
        self.assertEqual(code, 0)
        self.assertIn("checked:", out)
        self.assertIn("4 directories", out)  # keep, gone, parent, parent/child
        self.assertIn("database:", out)
        self.assertIn("scanned:", out)


class PruneAllTests(unittest.TestCase):
    """--prune-all: the directory pass plus per-file staleness."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tree = os.path.join(self._tmp.name, "tree")
        self.db = os.path.join(self._tmp.name, "db.sqlite")
        _make_tree(self.tree, {
            "keep/a.bin": 10, "keep/b.bin": 10,
            "gone/c.bin": 10,
        })
        code, _, _ = _run(["--sum", "-q", "--database", self.db, self.tree])
        self.assertEqual(code, 0)

    def test_counts_as_the_required_action_and_requires_database(self):
        with self.assertRaises(SystemExit):
            _run(["--prune-all", self.tree])
        code, _, err = _run(["--prune-all", "--database", "/nonexistent.db",
                             self.tree])
        self.assertEqual(code, 2)
        self.assertIn("no such database", err)

    def test_conflicts_match_prune_dirs(self):
        for extra in (["--sum"], ["--verify"], ["--remove"], ["--force"],
                      ["--prescan"], ["--exclude", "*.vob"], ["--no-ignore"]):
            with self.assertRaises(SystemExit, msg=f"expected error: {extra}"):
                _run(["--prune-all", "--database", "x"] + extra + [self.tree])

    def test_redundant_with_prune_dirs_is_allowed(self):
        code, _, _ = _run(["--prune-all", "--prune-dirs", "-q",
                           "--database", self.db, self.tree])
        self.assertEqual(code, 0)

    def test_deleted_file_in_surviving_dir_is_pruned(self):
        # The exact case --prune-dirs deliberately misses.
        os.unlink(os.path.join(self.tree, "keep", "a.bin"))
        code, out, _ = _run(["--prune-dirs", "-q", "--database", self.db,
                             self.tree])
        self.assertEqual(code, 0, "--prune-dirs must not notice it")
        self.assertEqual(len(_db_rel_paths(self.db)), 3)
        code, out, _ = _run(["--prune-all", "--database", self.db,
                             self.tree])
        self.assertEqual(code, 1)
        rels = _db_rel_paths(self.db)
        self.assertEqual(len(rels), 2)
        self.assertFalse(any(r.endswith("a.bin") for r in rels))
        self.assertTrue(any(r.endswith("b.bin") for r in rels))
        self.assertIn("0 directories, 1 file row", out)

    def test_deleted_dir_still_handled_by_the_dir_pass(self):
        shutil.rmtree(os.path.join(self.tree, "gone"))
        code, out, _ = _run(["--prune-all", "--database", self.db,
                             self.tree])
        self.assertEqual(code, 1)
        self.assertEqual(len(_db_rel_paths(self.db)), 2)
        self.assertIn("1 directory, 1 file row", out)

    def test_file_replaced_by_directory_is_pruned(self):
        path = os.path.join(self.tree, "keep", "a.bin")
        os.unlink(path)
        os.mkdir(path)  # same name, no longer a regular file
        code, _, _ = _run(["--prune-all", "-q", "--database", self.db,
                           self.tree])
        self.assertEqual(code, 1)
        self.assertEqual(len(_db_rel_paths(self.db)), 2)

    def test_dry_run_previews_without_deleting(self):
        os.unlink(os.path.join(self.tree, "keep", "a.bin"))
        code, out, _ = _run(["--prune-all", "-n", "-v", "--database", self.db,
                             self.tree])
        self.assertEqual(code, 1)
        self.assertIn("would prune", out)
        self.assertIn("(file row)", out)
        self.assertEqual(len(_db_rel_paths(self.db)), 3)

    def test_nothing_stale_is_exit_0_and_summary_counts_files(self):
        code, out, _ = _run(["--prune-all", "--database", self.db,
                             self.tree])
        self.assertEqual(code, 0)
        self.assertEqual(len(_db_rel_paths(self.db)), 3)
        self.assertIn("checked:", out)
        self.assertIn("2 directories, 3 files", out)


if __name__ == "__main__":
    unittest.main()
