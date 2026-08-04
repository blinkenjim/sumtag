"""Unit tests for the store's pure path helpers (sumtag.store).

Independent-oracle tests for mount-relative path computation (CLAUDE.md
"Path strategy: mount-relative").  The governing invariant, straight from
the design: the mount point is recorded separately so the absolute path can
be reconstructed as ``mount_point + rel_path`` -- so every rel_path this
module emits must recompose to the file it came from.  The database mirror
itself (SQLiteStore) is covered end-to-end by the conformance harness and
the db-mode unit tests.
"""

from __future__ import annotations

import os
import sys
import tempfile
import unittest

from unittest import mock

from sumtag.store import (PrescanSummary, SQLiteStore, StatData, _relativize,
                          _walk_up_mount, mount_relative, open_store,
                          read_prescan_summary)


class WalkUpMountTests(unittest.TestCase):
    """Finding the nearest enclosing mount via an injected ismount -- the
    Linux/other-platform strategy ("walk up from the file with
    os.path.ismount(), which detects a mount by the change in st_dev").
    """

    def test_stops_at_the_nearest_mount(self):
        mounts = {"/", "/mnt", "/mnt/backup"}
        self.assertEqual(
            _walk_up_mount("/mnt/backup/photos/2026", mounts.__contains__),
            "/mnt/backup")

    def test_a_mount_itself_is_its_own_answer(self):
        mounts = {"/", "/mnt/backup"}
        self.assertEqual(_walk_up_mount("/mnt/backup", mounts.__contains__),
                         "/mnt/backup")

    def test_plain_root_filesystem(self):
        # No intermediate mounts: everything walks up to /.
        self.assertEqual(_walk_up_mount("/usr/local/bin", "/".__eq__), "/")

    def test_never_loops_when_nothing_reports_a_mount(self):
        # Defensive: even a pathological ismount that never says yes must
        # terminate at the filesystem root rather than spin.
        self.assertEqual(_walk_up_mount("/a/b/c", lambda p: False), "/")


class RelativizeTests(unittest.TestCase):
    """rel_path such that join(mount, rel) locates the path again."""

    def test_path_under_mount_is_a_plain_strip(self):
        self.assertEqual(_relativize("/mnt/backup/photos/img.dng",
                                     "/mnt/backup"),
                         os.path.join("photos", "img.dng"))

    def test_the_mount_itself_relativizes_to_dot(self):
        # relpath(x, x) is '.'; callers normalize '.' to '' for storage.
        self.assertEqual(_relativize("/mnt/backup", "/mnt/backup"), ".")

    def test_unrelated_path_falls_back_to_the_escaping_form(self):
        # A path not under the mount, where the rooted rebase does not
        # recompose either (nothing at mount + rebased in this test
        # environment): the lexical, upward-escaping relpath is all that is
        # left.  It cannot recompose -- which is exactly why the rebase is
        # preferred whenever it verifies.
        with tempfile.TemporaryDirectory() as tmp:
            mount = os.path.join(tmp, "mnt")
            os.makedirs(mount)
            other = os.path.join(tmp, "elsewhere", "f.txt")
            rel = _relativize(other, mount)
            self.assertTrue(rel.startswith(os.pardir))
            self.assertEqual(
                os.path.normpath(os.path.join(mount, rel)),
                os.path.normpath(other))

    def test_rebase_is_used_when_it_recomposes(self):
        # The firmlink shape, reproduced with real directories: abs_path is
        # not lexically under the mount, but the whole rooted path rebased
        # under the mount reaches the same file -- then the rebased (non-
        # escaping) form must win over the escaping lexical one.
        with tempfile.TemporaryDirectory() as tmp:
            target = os.path.join(tmp, "data", "file.bin")
            os.makedirs(os.path.dirname(target))
            with open(target, "w") as f:
                f.write("x")
            # A "mount" that contains a copy of the rooted path to target:
            # mount + relpath(target, '/') is a real file, samefile via link.
            mount = os.path.join(tmp, "mount")
            rebased = os.path.relpath(target, "/")
            shadow = os.path.join(mount, rebased)
            os.makedirs(os.path.dirname(shadow))
            os.link(target, shadow)  # same inode: samefile verifies
            rel = _relativize(target, mount)
            self.assertEqual(rel, rebased)
            self.assertTrue(os.path.samefile(os.path.join(mount, rel), target))

    @unittest.skipUnless(
        sys.platform == "darwin"
        and os.path.isdir("/System/Volumes/Data/Users"),
        "real APFS firmlink layout required")
    def test_real_macos_firmlink_recomposes(self):
        # The motivating case on a live macOS system: /Users/... lives on
        # the Data volume but is not lexically beneath its mount.  The
        # emitted rel must recompose via samefile -- the exact invariant the
        # firmlink bug broke (rel_path escaping as ../../..).
        home = os.path.expanduser("~")
        rel = _relativize(home, "/System/Volumes/Data")
        self.assertFalse(rel.startswith(os.pardir))
        self.assertTrue(
            os.path.samefile(os.path.join("/System/Volumes/Data", rel), home))


class OpenStoreGrammarTests(unittest.TestCase):
    """CLAUDE.md "--database value grammar": no scheme -> SQLite file path;
    scheme:// -> a DSN dispatched by scheme (sqlite:// accepted, others
    recognized and rejected "not yet supported"); the // is required -- a
    bare "mysql:host" is a path, because ':' is legal in filenames.
    """

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)

    def test_plain_path_opens_sqlite(self):
        path = os.path.join(self._tmp.name, "db.sqlite")
        store = open_store(path)
        try:
            self.assertIsInstance(store, SQLiteStore)
        finally:
            store.close()
        self.assertTrue(os.path.exists(path))  # rwc created it

    def test_sqlite_scheme_is_accepted(self):
        path = os.path.join(self._tmp.name, "via-dsn.sqlite")
        store = open_store(f"sqlite://{path}")
        try:
            self.assertIsInstance(store, SQLiteStore)
        finally:
            store.close()
        self.assertTrue(os.path.exists(path))

    def test_foreign_schemes_are_recognized_and_rejected(self):
        for dsn in ("mysql://user@host:3306/db",
                    "postgresql://user@host:5432/db"):
            with self.subTest(dsn=dsn):
                with self.assertRaises(NotImplementedError) as ctx:
                    open_store(dsn)
                self.assertIn("not yet supported", str(ctx.exception))

    def test_colon_without_slashes_is_a_path(self):
        # "mysql:host" has no // -- it is a filename, and rwc creates it.
        path = os.path.join(self._tmp.name, "mysql:host")
        store = open_store(path)
        try:
            self.assertIsInstance(store, SQLiteStore)
        finally:
            store.close()
        self.assertTrue(os.path.exists(path))

    def test_ro_and_rw_require_an_existing_database(self):
        ghost = os.path.join(self._tmp.name, "missing.sqlite")
        for mode in ("ro", "rw"):
            with self.subTest(mode=mode):
                with self.assertRaises(FileNotFoundError):
                    open_store(ghost, mode=mode)
                # The refusal must not create the file as a side effect.
                self.assertFalse(os.path.exists(ghost))


import sqlite3  # noqa: E402  (independent reader for the store tests)


def _read_rows(db_path: str) -> list[tuple]:
    """Read the files table with plain sqlite3 -- the independent reader,
    never the store's own methods (harness-oracle discipline)."""
    conn = sqlite3.connect(db_path)
    try:
        return conn.execute(
            "SELECT m.path, f.rel_path, f.algo, f.digest, f.size, f.uid "
            "FROM files f JOIN mountpoints m ON m.id = f.mountpoint_id "
            "ORDER BY m.path, f.rel_path").fetchall()
    finally:
        conn.close()


def _stat(size: int = 100, uid: int = 501) -> StatData:
    return StatData(size=size, mode=0o100644, uid=uid, gid=20, nlink=1,
                    dev=7, ctime="2026-08-04T00:00:00.000000Z",
                    atime="2026-08-04T00:00:01.000000Z", birthtime=None)


class SQLiteStoreTests(unittest.TestCase):
    """The mirror's write methods, against a real temp database.  Authority:
    CLAUDE.md "Schema" -- identity is (mountpoint_id, rel_path), one row per
    location, UPSERT in place, COALESCE never clobbers locate columns, and
    directory deletes are dirname-equality, never recursive.
    """

    ROW = dict(algo="xxh3", digest="0123456789abcdef",
               file_mtime="2026-08-01T00:00:00.000000Z",
               hashed_at="2026-08-01T00:00:01.000000Z",
               run_started_at="2026-08-01T00:00:00.000000Z",
               version="0.1.0")

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.db = os.path.join(self._tmp.name, "m.sqlite")
        self.store = SQLiteStore(self.db)
        # Tests close explicitly before reading with plain sqlite3; this
        # cleanup is a guarded backstop (close() is not promised to be
        # idempotent, and nothing in sumtag double-closes).
        self.addCleanup(self._close_quietly)

    def _close_quietly(self) -> None:
        try:
            self.store.close()
        except Exception:
            pass

    def _upsert(self, rel: str, mp_id: int | None = None, *,
                digest: str = None, algo: str = None,
                stat: StatData | None = None) -> None:
        kw = dict(self.ROW)
        if digest is not None:
            kw["digest"] = digest
        if algo is not None:
            kw["algo"] = algo
        if mp_id is None:
            mp_id = self.store.ensure_mountpoint("/mnt/t")
        self.store.upsert_file(mp_id, rel, 42, kw["algo"], kw["digest"],
                               kw["file_mtime"], kw["hashed_at"],
                               kw["run_started_at"], kw["version"], stat=stat)

    def test_ensure_mountpoint_inserts_once(self):
        a = self.store.ensure_mountpoint("/mnt/backup")
        b = self.store.ensure_mountpoint("/mnt/backup")
        c = self.store.ensure_mountpoint("/mnt/other")
        self.assertEqual(a, b)
        self.assertNotEqual(a, c)
        self.store.close()
        conn = sqlite3.connect(self.db)
        paths = [r[0] for r in conn.execute(
            "SELECT path FROM mountpoints ORDER BY path")]
        conn.close()
        self.assertEqual(paths, ["/mnt/backup", "/mnt/other"])

    def test_upsert_updates_in_place_by_location(self):
        # Identity is (mountpoint, rel_path): a re-scan must UPDATE the one
        # row, never add a second.
        self._upsert("a/f.bin", digest="1111111111111111")
        self._upsert("a/f.bin", digest="2222222222222222")
        self.store.close()
        rows = _read_rows(self.db)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0][3], "2222222222222222")

    def test_algo_switch_replaces_the_single_digest(self):
        # One row holds one digest per location -- re-stamping under a new
        # algorithm replaces, matching the xattr's one-entry map.
        self._upsert("a/f.bin", algo="xxh3", digest="1111111111111111")
        self._upsert("a/f.bin", algo="md5",
                     digest="d41d8cd98f00b204e9800998ecf8427e")
        self.store.close()
        rows = _read_rows(self.db)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0][2], "md5")

    def test_statless_upsert_preserves_locate_columns(self):
        # The COALESCE contract: a --sum re-scan after a --locate run must
        # not null out what --locate wrote.
        self._upsert("a/f.bin", stat=_stat(size=999, uid=77))
        self._upsert("a/f.bin", digest="2222222222222222", stat=None)
        self.store.close()
        rows = _read_rows(self.db)
        self.assertEqual(rows[0][3], "2222222222222222")  # digest updated...
        self.assertEqual(rows[0][4], 999)                 # ...stat preserved
        self.assertEqual(rows[0][5], 77)

    def test_statful_upsert_overwrites_locate_columns(self):
        self._upsert("a/f.bin", stat=_stat(size=100))
        self._upsert("a/f.bin", stat=_stat(size=222))
        self.store.close()
        self.assertEqual(_read_rows(self.db)[0][4], 222)

    def test_update_stat_is_a_noop_without_a_row(self):
        mp = self.store.ensure_mountpoint("/mnt/t")
        self.store.update_stat(mp, "ghost/f.bin", _stat())
        self.store.close()
        self.assertEqual(_read_rows(self.db), [])  # nothing conjured

    def test_update_stat_fills_an_existing_row(self):
        self._upsert("a/f.bin")
        mp = self.store.ensure_mountpoint("/mnt/t")
        self.store.update_stat(mp, "a/f.bin", _stat(size=555))
        self.store.close()
        rows = _read_rows(self.db)
        self.assertEqual(rows[0][4], 555)
        self.assertEqual(rows[0][3], self.ROW["digest"])  # digest untouched

    def _populate_tree(self) -> int:
        # A small tree: root file, a/ with two files, a/sub with one, b/ one.
        mp = self.store.ensure_mountpoint("/mnt/t")
        for rel in ("root.txt", "a/f1.txt", "a/f2.txt", "a/sub/deep.txt",
                    "b/g.txt"):
            self._upsert(rel, mp)
        return mp

    def test_iter_file_dirs_derives_every_directory_with_counts(self):
        self._populate_tree()
        # Every directory holding rows appears independently -- the property
        # that makes --prune-dirs need no recursion.
        self.assertEqual(self.store.iter_file_dirs("/mnt/t", ""),
                         [("", 1), ("a", 2), ("a/sub", 1), ("b", 1)])

    def test_iter_file_dirs_scopes_to_the_prefix(self):
        self._populate_tree()
        self.assertEqual(self.store.iter_file_dirs("/mnt/t", "a"),
                         [("a", 2), ("a/sub", 1)])
        self.assertEqual(self.store.iter_file_dirs("/unknown", ""), [])

    def test_iter_dir_file_paths_is_direct_residents_only(self):
        self._populate_tree()
        self.assertEqual(self.store.iter_dir_file_paths("/mnt/t", "a"),
                         ["a/f1.txt", "a/f2.txt"])   # not a/sub/deep.txt
        self.assertEqual(self.store.iter_dir_file_paths("/mnt/t", ""),
                         ["root.txt"])               # mount root residents

    def test_delete_dir_files_is_dirname_equality_never_recursive(self):
        self._populate_tree()
        deleted = self.store.delete_dir_files("/mnt/t", "a")
        self.store.close()
        self.assertEqual(deleted, 2)                 # a/f1, a/f2 -- only
        remaining = [r[1] for r in _read_rows(self.db)]
        self.assertIn("a/sub/deep.txt", remaining)   # the child SURVIVES
        self.assertEqual(len(remaining), 3)

    def test_delete_files_removes_exactly_the_named_rows(self):
        self._populate_tree()
        deleted = self.store.delete_files("/mnt/t",
                                          ["a/f1.txt", "b/g.txt", "ghost"])
        self.store.close()
        self.assertEqual(deleted, 2)                 # ghost matched nothing
        remaining = [r[1] for r in _read_rows(self.db)]
        self.assertEqual(remaining, ["a/f2.txt", "a/sub/deep.txt", "root.txt"])

    def test_delete_files_crosses_the_parameter_chunk_boundary(self):
        # SQLite bounds host parameters; the delete must chunk. 1200 rows
        # crosses the 500-per-statement chunk twice.
        mp = self.store.ensure_mountpoint("/mnt/t")
        rels = [f"big/f{i:04d}" for i in range(1200)]
        for rel in rels:
            self._upsert(rel, mp)
        self.assertEqual(self.store.delete_files("/mnt/t", rels), 1200)
        self.store.close()
        self.assertEqual(_read_rows(self.db), [])


class PrescanSummaryStoreTests(unittest.TestCase):
    """The one-row prescan summary: save replaces, read is side-effect-free
    (CLAUDE.md "--prescan" persistence / "--db-prescan").
    """

    def _summary(self, count: int = 137) -> PrescanSummary:
        return PrescanSummary(file_count=count, total_bytes=4200,
                              roots=["/data"], sum_mode=True, force=False,
                              exclude=["*.vob"], no_ignore=False,
                              created_at="2026-08-04T12:00:00.000000Z")

    def test_save_and_read_round_trip(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = os.path.join(tmp, "p.sqlite")
            store = SQLiteStore(db)
            store.save_prescan_summary(self._summary())
            store.close()
            got = read_prescan_summary(db)
            self.assertEqual(got, self._summary())

    def test_second_save_replaces_the_one_row(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = os.path.join(tmp, "p.sqlite")
            store = SQLiteStore(db)
            store.save_prescan_summary(self._summary(count=1))
            store.save_prescan_summary(self._summary(count=2))
            store.close()
            self.assertEqual(read_prescan_summary(db).file_count, 2)
            conn = sqlite3.connect(db)
            n = conn.execute("SELECT COUNT(*) FROM prescan_summary").fetchone()[0]
            conn.close()
            self.assertEqual(n, 1)

    def test_read_missing_is_none_and_creates_nothing(self):
        with tempfile.TemporaryDirectory() as tmp:
            ghost = os.path.join(tmp, "none.sqlite")
            self.assertIsNone(read_prescan_summary(ghost))
            self.assertFalse(os.path.exists(ghost))  # ro open, no side effect
            # A database without the row is also None, not an error.
            db = os.path.join(tmp, "empty.sqlite")
            SQLiteStore(db).close()
            self.assertIsNone(read_prescan_summary(db))

    def test_read_rejects_foreign_schemes(self):
        with self.assertRaises(NotImplementedError):
            read_prescan_summary("mysql://host/db")


class MountRelativeTests(unittest.TestCase):
    """The top-level split: (mount, rel) such that join recomposes to the
    original file, verified samefile -- the invariant everything downstream
    (mirror rows, prune, dedupe) leans on.
    """

    def test_recomposes_for_real_paths(self):
        # The invariant is RECOMPOSITION, not non-escaping: a path whose
        # spelling crosses a symlink (macOS's /var -> private/var, where
        # tempfile.gettempdir() lives) cannot samefile-verify the rebase,
        # and the documented fallback is the escaping lexical form -- which
        # still recomposes, the kernel resolving the .. segments.
        for path in (os.path.expanduser("~"), tempfile.gettempdir(),
                     __file__):
            with self.subTest(path=path):
                mount, rel = mount_relative(path)
                recon = os.path.join(mount, rel) if rel != "." else mount
                self.assertTrue(os.path.samefile(recon, path))

    def test_home_gets_the_non_escaping_form(self):
        # Where the rebase CAN verify (the home directory under the macOS
        # Data-volume firmlink, or any genuinely-under-its-mount path
        # elsewhere), the emitted rel must be the non-escaping one -- the
        # firmlink bug's fix.
        mount, rel = mount_relative(os.path.expanduser("~"))
        self.assertFalse(rel.startswith(os.pardir))

    def test_ismount_walk_fallback(self):
        # With the statfs shortcut disabled (the non-darwin branch's shape),
        # the injected ismount decides the boundary.
        with mock.patch("sumtag.store._mount_point", return_value=None):
            fake_mounts = {"/", os.path.realpath(tempfile.gettempdir())}
            probe = os.path.join(os.path.realpath(tempfile.gettempdir()),
                                 "x", "y.txt")
            mount, rel = mount_relative(probe, ismount=fake_mounts.__contains__)
            self.assertEqual(mount, os.path.realpath(tempfile.gettempdir()))
            self.assertEqual(rel, os.path.join("x", "y.txt"))


if __name__ == "__main__":
    unittest.main()
