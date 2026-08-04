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

from sumtag.store import (SQLiteStore, _relativize, _walk_up_mount,
                          open_store)


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


if __name__ == "__main__":
    unittest.main()
