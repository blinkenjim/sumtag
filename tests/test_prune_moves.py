"""Flow tests for --prune-dirs move/rename detection (TODO.md design,
decided 2026-08-05, folded into --prune-dirs itself).

A vanished directory whose recorded residents reappear elsewhere agreeing
on (inode, basename, microsecond mtime) has its rows' rel_path rewritten in
place instead of deleted: renames-in-place resolve from the parent scan
alone (tier 1, nearly free); arbitrary relocation is found by the announced
tree-wide walk (tier 2). Anything less than full signature agreement falls
back to prune-plus-rescan; hard-link ambiguity is refused with exit 2.
"""

from __future__ import annotations

import contextlib
import io
import os
import shutil
import sqlite3
import tempfile
import unittest

from sumtag import cli


def _run(argv: list[str]) -> tuple[int, str, str]:
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        code = cli.main(argv)
    return code, out.getvalue(), err.getvalue()


def _db_rel_paths(db: str) -> set[str]:
    conn = sqlite3.connect(db)
    rows = {r[0] for r in conn.execute("SELECT rel_path FROM files")}
    conn.close()
    return rows


class MoveFixture(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = self._tmp.name
        self.db = os.path.join(self._tmp.name + "-db.sqlite")
        self.addCleanup(lambda: os.path.exists(self.db) and os.remove(self.db))

    def _make(self, *rels: str) -> None:
        for rel in rels:
            path = os.path.join(self.root, rel)
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "wb") as f:
                f.write(rel.encode() * 4)

    def _stamp_and_mirror(self) -> None:
        code, _, _ = _run(["--sum", "--database", self.db, self.root])
        assert code == 0

    def _rel(self, db_rel: str) -> str:
        """A db rel_path suffix check helper: rel_paths are mount-relative,
        so assert by suffix under this test root's basename."""
        return db_rel

    def _rels_under_root(self) -> set[str]:
        # Strip everything up to the root's own directory name so the
        # assertions read as tree-relative paths.
        marker = os.path.basename(self.root) + "/"
        out = set()
        for rel in _db_rel_paths(self.db):
            i = rel.find(marker)
            out.add(rel[i + len(marker):] if i >= 0 else rel)
        return out

    def _prune(self, *extra: str) -> tuple[int, str, str]:
        return _run(["--prune-dirs", "--database", self.db, *extra, self.root])


class RenameInPlaceTests(MoveFixture):
    def test_rename_detected_and_rows_rewritten(self):
        self._make("a/f1.bin", "a/f2.bin", "keep/k.bin")
        self._stamp_and_mirror()
        os.rename(os.path.join(self.root, "a"),
                  os.path.join(self.root, "a-renamed"))
        code, out, err = self._prune()
        self.assertEqual(code, 1, f"stale-found should exit 1: {out}{err}")
        rels = self._rels_under_root()
        self.assertIn("a-renamed/f1.bin", rels)
        self.assertIn("a-renamed/f2.bin", rels)
        self.assertNotIn("a/f1.bin", rels)
        self.assertIn("keep/k.bin", rels)          # untouched neighbor
        self.assertIn(" -> ", out)                 # the move announcement
        self.assertIn("moved:", out)               # the summary line

    def test_rename_resolves_in_tier_one_without_the_walk(self):
        self._make("a/f1.bin")
        self._stamp_and_mirror()
        os.rename(os.path.join(self.root, "a"),
                  os.path.join(self.root, "b"))
        code, out, _ = self._prune()
        self.assertEqual(code, 1)
        self.assertNotIn("walking roots", out,
                         "a parent-scan rename must not trigger the walk")

    def test_verbose_move_announcement_names_rows(self):
        self._make("a/f1.bin", "a/f2.bin")
        self._stamp_and_mirror()
        os.rename(os.path.join(self.root, "a"),
                  os.path.join(self.root, "b"))
        _, out, _ = self._prune("-v")
        self.assertIn("move", out)
        self.assertIn("(2 file rows)", out)


class TreeWalkTests(MoveFixture):
    def test_relocation_found_by_the_announced_walk(self):
        self._make("a/f1.bin", "nest/existing.bin")
        self._stamp_and_mirror()
        dest_parent = os.path.join(self.root, "nest", "deeper")
        os.makedirs(dest_parent)
        os.rename(os.path.join(self.root, "a"),
                  os.path.join(dest_parent, "moved-a"))
        code, out, _ = self._prune()
        self.assertEqual(code, 1)
        self.assertIn("walking roots", out)        # announced, by consent
        rels = self._rels_under_root()
        self.assertIn("nest/deeper/moved-a/f1.bin", rels)
        self.assertNotIn("a/f1.bin", rels)

    def test_moved_tree_resolves_directory_by_directory(self):
        # A moved TREE: parent and child are independently lost, found, and
        # matched (the non-recursive composition).
        self._make("a/f1.bin", "a/sub/f2.bin")
        self._stamp_and_mirror()
        os.rename(os.path.join(self.root, "a"),
                  os.path.join(self.root, "b"))
        code, _, _ = self._prune()
        self.assertEqual(code, 1)
        rels = self._rels_under_root()
        self.assertIn("b/f1.bin", rels)
        self.assertIn("b/sub/f2.bin", rels)
        self.assertNotIn("a/f1.bin", rels)
        self.assertNotIn("a/sub/f2.bin", rels)


class FallbackTests(MoveFixture):
    def test_modified_after_move_falls_back_to_prune(self):
        # The moved file's mtime changed: the signature no longer agrees, so
        # the safe fallback is prune (a later rescan re-adds the new rows).
        self._make("a/f1.bin")
        self._stamp_and_mirror()
        os.rename(os.path.join(self.root, "a"),
                  os.path.join(self.root, "b"))
        moved = os.path.join(self.root, "b", "f1.bin")
        st = os.stat(moved)
        os.utime(moved, ns=(st.st_atime_ns, st.st_mtime_ns + 5_000_000_000))
        code, out, _ = self._prune()
        self.assertEqual(code, 1)
        rels = self._rels_under_root()
        self.assertNotIn("b/f1.bin", rels)         # no move recorded...
        self.assertNotIn("a/f1.bin", rels)         # ...and old rows pruned

    def test_plain_deletion_still_prunes(self):
        self._make("a/f1.bin", "keep/k.bin")
        self._stamp_and_mirror()
        shutil.rmtree(os.path.join(self.root, "a"))
        code, out, _ = self._prune()
        self.assertEqual(code, 1)
        self.assertNotIn("a/f1.bin", self._rels_under_root())
        self.assertIn("keep/k.bin", self._rels_under_root())
        self.assertNotIn("moved:", out)

    def test_nothing_stale_is_still_exit_zero_and_walkless(self):
        self._make("a/f1.bin")
        self._stamp_and_mirror()
        code, out, _ = self._prune()
        self.assertEqual(code, 0)
        self.assertNotIn("walking roots", out)


class DryRunTests(MoveFixture):
    def test_preview_says_would_move_and_changes_nothing(self):
        self._make("a/f1.bin")
        self._stamp_and_mirror()
        os.rename(os.path.join(self.root, "a"),
                  os.path.join(self.root, "b"))
        code, out, _ = self._prune("-n", "-v")
        self.assertEqual(code, 1)
        self.assertIn("would move", out)
        rels = self._rels_under_root()
        self.assertIn("a/f1.bin", rels)            # database untouched
        self.assertNotIn("b/f1.bin", rels)


class AmbiguityTests(MoveFixture):
    def test_hard_link_farm_is_refused_with_exit_2(self):
        # h1/f and h2/f are hard links: identical inode AND mtime, so after
        # renaming both directories the evidence cannot say which went
        # where. Refuse: rows kept, warning, exit 2.
        self._make("h1/f.bin")
        os.makedirs(os.path.join(self.root, "h2"))
        os.link(os.path.join(self.root, "h1", "f.bin"),
                os.path.join(self.root, "h2", "f.bin"))
        self._stamp_and_mirror()
        os.rename(os.path.join(self.root, "h1"),
                  os.path.join(self.root, "n1"))
        os.rename(os.path.join(self.root, "h2"),
                  os.path.join(self.root, "n2"))
        code, out, err = self._prune()
        self.assertEqual(code, 2, f"ambiguity must exit 2: {out}{err}")
        self.assertIn("ambiguous", err)
        rels = self._rels_under_root()
        self.assertIn("h1/f.bin", rels)            # nothing deleted...
        self.assertIn("h2/f.bin", rels)            # ...nothing guessed


class ExclusionCompositionTests(MoveFixture):
    def test_exclude_and_no_ignore_are_now_accepted(self):
        # The candidate search genuinely traverses, so the traversal flags
        # stop being CLI errors with the prune family.
        self._make("a/f1.bin")
        self._stamp_and_mirror()
        code, _, _ = self._prune("--exclude", "*.zzz")
        self.assertEqual(code, 0)
        code, _, _ = self._prune("--no-ignore")
        self.assertEqual(code, 0)

    def test_excluded_candidate_is_invisible_to_matching(self):
        # The moved directory's new name matches --exclude: the candidate
        # search must not see it, so the rows fall back to prune.
        self._make("a/f1.bin")
        self._stamp_and_mirror()
        os.rename(os.path.join(self.root, "a"),
                  os.path.join(self.root, "a.skip"))
        code, out, _ = self._prune("--exclude", "*.skip")
        self.assertEqual(code, 1)
        rels = self._rels_under_root()
        self.assertNotIn("a.skip/f1.bin", rels)    # not moved...
        self.assertNotIn("a/f1.bin", rels)         # ...pruned instead


if __name__ == "__main__":
    unittest.main()
