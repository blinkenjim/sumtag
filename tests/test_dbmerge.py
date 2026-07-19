"""Unit tests for dbmerge.

Covers CLAUDE.md "Experimental companion programs" / dbmerge:
replace-per-mountpoint semantics (idempotent re-merge, prune propagation,
authoritative emptiness, untouched unrelated mountpoints), the guards
(collision, mixed algorithms, target-as-source, duplicate source, not a
sumtag database), grouper-artifact dropping, the -n preview's no-side-
effects contract, and the 0/1/2 exit codes.

Source databases are fabricated directly (store schema + handcrafted rows
under invented mountpoints like /Volumes/tank1) rather than produced by
sumtag runs: two live scans on the same test machine would record the same
real mountpoint and collide by design.
"""

from __future__ import annotations

import contextlib
import io
import os
import sqlite3
import tempfile
import unittest

from sumtag import dbmerge
from sumtag.store import _SCHEMA_SQL


def _run(argv: list[str]) -> tuple[int, str, str]:
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        code = dbmerge.main(argv)
    return code, out.getvalue(), err.getvalue()


def _make_db(path: str, rows: dict[str, list[tuple]]) -> None:
    """Create a sumtag database; rows maps mountpoint path -> a list of
    (rel_path, digest) or (rel_path, digest, algo, size) tuples."""
    conn = sqlite3.connect(path)
    conn.executescript(_SCHEMA_SQL)
    for mount, files in rows.items():
        conn.execute("INSERT INTO mountpoints(path) VALUES (?)", (mount,))
        mp_id = conn.execute("SELECT id FROM mountpoints WHERE path = ?",
                             (mount,)).fetchone()[0]
        for spec in files:
            rel, digest = spec[0], spec[1]
            algo = spec[2] if len(spec) > 2 else "xxh3"
            size = spec[3] if len(spec) > 3 else None
            conn.execute(
                "INSERT INTO files (mountpoint_id, rel_path, inode, algo, "
                "digest, file_mtime, hashed_at, run_started_at, version, size) "
                "VALUES (?, ?, 1, ?, ?, 't', 't', 't', '0.1.0', ?)",
                (mp_id, rel, algo, digest, size))
    conn.commit()
    conn.close()


def _contents(path: str) -> set[tuple[str, str, str]]:
    """The (mountpoint path, rel_path, digest) triples in a database."""
    conn = sqlite3.connect(path)
    rows = {(m, r, d) for m, r, d in conn.execute(
        "SELECT m.path, f.rel_path, f.digest FROM files f "
        "JOIN mountpoints m ON m.id = f.mountpoint_id")}
    conn.close()
    return rows


class MergeGuardTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.dir = self._tmp.name

    def _p(self, name: str) -> str:
        return os.path.join(self.dir, name)

    def test_missing_source(self):
        code, _, err = _run(["--database", self._p("t.db"),
                             self._p("absent.db")])
        self.assertEqual(code, 2)
        self.assertIn("no such database", err)
        # The guard fired before the target could be created.
        self.assertFalse(os.path.exists(self._p("t.db")))

    def test_not_a_sumtag_db(self):
        conn = sqlite3.connect(self._p("junk.db"))
        conn.execute("CREATE TABLE junk (x)")
        conn.close()
        code, _, err = _run(["--database", self._p("t.db"), self._p("junk.db")])
        self.assertEqual(code, 2)
        self.assertIn("not a sumtag database", err)

    def test_target_cannot_be_a_source(self):
        _make_db(self._p("a.db"), {"/Volumes/tank1": [("f", "d1")]})
        code, _, err = _run(["--database", self._p("a.db"), self._p("a.db")])
        self.assertEqual(code, 2)
        self.assertIn("cannot also be a source", err)

    def test_duplicate_source(self):
        _make_db(self._p("a.db"), {"/Volumes/tank1": [("f", "d1")]})
        code, _, err = _run(["--database", self._p("t.db"),
                             self._p("a.db"), self._p("a.db")])
        self.assertEqual(code, 2)
        self.assertIn("same database as source", err)

    def test_mountpoint_collision(self):
        _make_db(self._p("a.db"), {"/Volumes/tank1": [("f", "d1")]})
        _make_db(self._p("b.db"), {"/Volumes/tank1": [("g", "d2")]})
        code, _, err = _run(["--database", self._p("t.db"),
                             self._p("a.db"), self._p("b.db")])
        self.assertEqual(code, 2)
        self.assertIn("recorded in both", err)
        self.assertFalse(os.path.exists(self._p("t.db")))

    def test_mixed_algos_refused_and_overridden(self):
        _make_db(self._p("a.db"), {"/Volumes/tank1": [("f", "d1", "xxh3")]})
        _make_db(self._p("b.db"), {"/Volumes/tank2": [("g", "d2", "md5")]})
        code, _, err = _run(["--database", self._p("t.db"),
                             self._p("a.db"), self._p("b.db")])
        self.assertEqual(code, 2)
        self.assertIn("mixed digest algorithms", err)
        code, _, _ = _run(["--database", self._p("t.db"), "--allow-mixed",
                           self._p("a.db"), self._p("b.db")])
        self.assertEqual(code, 1)
        self.assertEqual(len(_contents(self._p("t.db"))), 2)

    def test_mixed_algo_with_surviving_target_rows(self):
        # The target keeps an md5 mountpoint no source names; merging an
        # xxh3 source alongside it is the same mixed-corpus hazard.
        _make_db(self._p("t.db"), {"/Volumes/old": [("f", "d1", "md5")]})
        _make_db(self._p("a.db"), {"/Volumes/tank1": [("g", "d2", "xxh3")]})
        code, _, err = _run(["--database", self._p("t.db"), self._p("a.db")])
        self.assertEqual(code, 2)
        self.assertIn("mixed digest algorithms", err)
        # But replacing the md5 mountpoint itself is fine: nothing survives.
        _make_db(self._p("b.db"), {"/Volumes/old": [("f", "d1", "xxh3")]})
        code, _, _ = _run(["--database", self._p("t.db"),
                           self._p("a.db"), self._p("b.db")])
        self.assertEqual(code, 1)


class MergeFlowTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.dir = self._tmp.name
        self.target = os.path.join(self.dir, "merged.db")
        self.a = os.path.join(self.dir, "a.db")
        self.b = os.path.join(self.dir, "b.db")
        _make_db(self.a, {"/Volumes/tank1": [("x/f1", "d1"), ("x/f2", "d2")]})
        _make_db(self.b, {"/Volumes/tank2": [("y/f3", "d3")],
                          "/Volumes/tank3": [("z/f4", "d4")]})

    def test_basic_merge(self):
        code, out, _ = _run(["--database", self.target, self.a, self.b])
        self.assertEqual(code, 1)
        self.assertEqual(_contents(self.target), {
            ("/Volumes/tank1", "x/f1", "d1"),
            ("/Volumes/tank1", "x/f2", "d2"),
            ("/Volumes/tank2", "y/f3", "d3"),
            ("/Volumes/tank3", "z/f4", "d4"),
        })
        self.assertIn("merge:", out)
        self.assertIn("4 rows, 3 mountpoints, 2 sources", out)

    def test_remerge_is_idempotent(self):
        _run(["--database", self.target, self.a, self.b])
        before = _contents(self.target)
        code, out, _ = _run(["--database", self.target, self.a, self.b])
        self.assertEqual(code, 1)  # replacement always modifies
        self.assertEqual(_contents(self.target), before)
        self.assertRegex(out, r"replace:\s+4 rows")

    def test_remerge_propagates_prunes(self):
        _run(["--database", self.target, self.a, self.b])
        conn = sqlite3.connect(self.a)
        conn.execute("DELETE FROM files WHERE rel_path = 'x/f2'")
        conn.commit()
        conn.close()
        code, _, _ = _run(["--database", self.target, self.a])
        self.assertEqual(code, 1)
        self.assertNotIn(("/Volumes/tank1", "x/f2", "d2"),
                         _contents(self.target))

    def test_empty_source_mountpoint_is_authoritative(self):
        _run(["--database", self.target, self.a])
        conn = sqlite3.connect(self.a)
        conn.execute("DELETE FROM files")
        conn.commit()
        conn.close()
        code, _, _ = _run(["--database", self.target, self.a])
        self.assertEqual(code, 1)  # 2 rows replaced, 0 merged
        self.assertEqual(_contents(self.target), set())

    def test_unrelated_target_mountpoint_survives(self):
        _make_db(self.target, {"/Volumes/keep": [("k", "dk")]})
        code, _, _ = _run(["--database", self.target, self.a])
        self.assertEqual(code, 1)
        self.assertIn(("/Volumes/keep", "k", "dk"), _contents(self.target))

    def test_locate_columns_travel(self):
        _make_db(os.path.join(self.dir, "c.db"),
                 {"/Volumes/tank4": [("s", "d5", "xxh3", 4096)]})
        _run(["--database", self.target, os.path.join(self.dir, "c.db")])
        conn = sqlite3.connect(self.target)
        size = conn.execute("SELECT size FROM files WHERE rel_path = 's'"
                            ).fetchone()[0]
        conn.close()
        self.assertEqual(size, 4096)

    def test_grouper_artifacts_dropped(self):
        _run(["--database", self.target, self.a])
        conn = sqlite3.connect(self.target)
        conn.execute("CREATE TABLE dirs (id INTEGER PRIMARY KEY)")
        conn.execute("CREATE TABLE dir_pairs (x)")
        conn.close()
        code, out, _ = _run(["--database", self.target, self.b])
        self.assertEqual(code, 1)
        self.assertIn("drop grouper artifacts", out)
        conn = sqlite3.connect(self.target)
        names = {r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table'")}
        conn.close()
        self.assertNotIn("dirs", names)
        self.assertNotIn("dir_pairs", names)

    def test_empty_sources_exit_zero(self):
        empty = os.path.join(self.dir, "empty.db")
        _make_db(empty, {})
        code, out, _ = _run(["--database", self.target, empty])
        self.assertEqual(code, 0)
        self.assertIn("merge:", out)  # the headline prints even at zero

    def test_dry_run_has_no_side_effects(self):
        code, out, _ = _run(["--database", self.target, "-n",
                             self.a, self.b])
        self.assertEqual(code, 1)  # would modify
        self.assertFalse(os.path.exists(self.target))  # not created
        self.assertIn("would merge", out)
        self.assertIn("4 rows, 3 mountpoints, 2 sources", out)

    def test_dry_run_previews_replacement(self):
        _run(["--database", self.target, self.a])
        before = _contents(self.target)
        code, out, _ = _run(["--database", self.target, "-n", self.a])
        self.assertEqual(code, 1)
        self.assertIn("would replace: 2 rows", out)
        self.assertEqual(_contents(self.target), before)  # untouched

    def test_dry_run_previews_artifact_drop(self):
        _run(["--database", self.target, self.a])
        conn = sqlite3.connect(self.target)
        conn.execute("CREATE TABLE grouper_meta (x)")
        conn.close()
        code, out, _ = _run(["--database", self.target, "-n", self.a])
        self.assertEqual(code, 1)
        self.assertIn("would drop grouper artifacts", out)
        conn = sqlite3.connect(self.target)
        names = {r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table'")}
        conn.close()
        self.assertIn("grouper_meta", names)  # still there


if __name__ == "__main__":
    unittest.main()
