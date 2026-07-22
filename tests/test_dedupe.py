"""Unit tests for dedupe (the experimental companion program).

Covers the synced flat walk, the trust-model vetoes, the safety refusals,
the preview/arm split, the empty-directory carve-out, symlink rules, and
the placeholder cull root.
"""

from __future__ import annotations

import contextlib
import io
import os
import shlex
import shutil
import sqlite3
import tempfile
import unittest
from unittest import mock

from sumtag import cli, dedupe, schema, xattr


def _write(root: str, rel: str, data: bytes) -> str:
    path = os.path.join(root, rel)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as f:
        f.write(data)
    return path


def _stamp(db: str, *roots: str) -> None:
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        code = cli.main(["--sum", "-q", "--database", db, *roots])
    assert code == 0, err.getvalue()


def _run(argv: list[str]) -> tuple[int, str, str]:
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        code = dedupe.main(argv)
    return code, out.getvalue(), err.getvalue()


class DedupeTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.actual = os.path.join(self._tmp.name, "actual")
        self.cull = os.path.join(self._tmp.name, "cull")
        self.db = os.path.join(self._tmp.name, "db.sqlite")
        os.makedirs(self.actual)
        os.makedirs(self.cull)

    def _dedupe(self, *extra: str) -> tuple[int, str, str]:
        return _run(["--database", self.db, self.actual, self.cull, *extra])

    def test_preview_finds_but_touches_nothing(self):
        _write(self.actual, "a.txt", b"same")
        _write(self.cull, "renamed.txt", b"same")
        _stamp(self.db, self.actual, self.cull)
        code, out, _ = self._dedupe()
        self.assertEqual(code, 1)
        self.assertIn("would delete", out)
        self.assertTrue(os.path.exists(os.path.join(self.cull, "renamed.txt")))

    def test_delete_kills_renamed_duplicates_keeps_unique(self):
        _write(self.actual, "a.txt", b"same")
        _write(self.cull, "renamed.txt", b"same")
        _write(self.cull, "copy2.txt", b"same")     # kill them all
        _write(self.cull, "unique.txt", b"only here")
        _stamp(self.db, self.actual, self.cull)
        code, out, _ = self._dedupe("--delete")
        self.assertEqual(code, 1)
        self.assertFalse(os.path.exists(os.path.join(self.cull, "renamed.txt")))
        self.assertFalse(os.path.exists(os.path.join(self.cull, "copy2.txt")))
        self.assertTrue(os.path.exists(os.path.join(self.cull, "unique.txt")))
        self.assertTrue(os.path.isdir(self.cull), "cull root blocked by unique")
        # The kills' database rows went with them; the survivor's remains.
        conn = sqlite3.connect(self.db)
        rels = {r[0] for r in conn.execute("SELECT rel_path FROM files")}
        conn.close()
        self.assertFalse(any(r.endswith(("renamed.txt", "copy2.txt")) for r in rels))
        self.assertTrue(any(r.endswith("unique.txt") for r in rels))

    def test_multiple_culls_deduped_against_one_actual(self):
        # One ACTUAL, two CULL trees in a single invocation: each cull is
        # deduped against the same actual, uniques survive, and the summary
        # names both culls.
        cull2 = os.path.join(self._tmp.name, "cull2")
        os.makedirs(cull2)
        _write(self.actual, "a.txt", b"same")
        _write(self.cull, "renamed.txt", b"same")
        _write(self.cull, "only1.txt", b"unique to cull1")
        _write(cull2, "other.txt", b"same")
        _write(cull2, "only2.txt", b"unique to cull2")
        _stamp(self.db, self.actual, self.cull, cull2)
        code, out, _ = _run(["--database", self.db, self.actual,
                             self.cull, cull2, "--delete"])
        self.assertEqual(code, 1)
        self.assertFalse(os.path.exists(os.path.join(self.cull, "renamed.txt")))
        self.assertFalse(os.path.exists(os.path.join(cull2, "other.txt")))
        self.assertTrue(os.path.exists(os.path.join(self.cull, "only1.txt")))
        self.assertTrue(os.path.exists(os.path.join(cull2, "only2.txt")))
        self.assertIn("deleted:", out)
        self.assertIn("2 files", out)          # one from each cull
        self.assertIn(self.cull, out)          # both culls named in summary
        self.assertIn(cull2, out)

    def test_actual_may_not_appear_among_the_culls(self):
        # The one guarantee that matters: ACTUAL disjoint from *every* cull,
        # even when it is the second of several.
        _write(self.actual, "a.txt", b"x")
        _write(self.cull, "b.txt", b"x")
        _stamp(self.db, self.actual, self.cull)
        code, _, err = _run(["--database", self.db, self.actual,
                             self.cull, self.actual])
        self.assertEqual(code, 2)
        self.assertIn("same directory", err)

    def test_echoes_shell_quoted_command_line(self):
        # Just before the summary, the run echoes a copy-pasteable command
        # line; a path with whitespace must stay a single quoted argument.
        spaced = os.path.join(self._tmp.name, "cull with space")
        os.makedirs(spaced)
        _write(self.actual, "a.txt", b"same")
        _write(spaced, "x.txt", b"same")
        _stamp(self.db, self.actual, spaced)
        argv = ["--database", self.db, self.actual, spaced]
        code, out, _ = _run(argv)
        self.assertEqual(code, 1)
        self.assertIn("command line:", out)
        expected = "dedupe " + " ".join(shlex.quote(t) for t in argv)
        self.assertIn(expected, out)
        self.assertIn(shlex.quote(spaced), out)  # not split on the space

    def test_action_lines_carry_ls_f_type_indicators(self):
        # Each action path ends with an ls -F style indicator: * executable,
        # @ symlink, / directory; a plain file gets none.
        _write(self.actual, "run.sh", b"#!/bin/sh\ntrue\n")
        exe = _write(self.cull, "run.sh", b"#!/bin/sh\ntrue\n")
        os.chmod(exe, 0o755)
        _write(self.actual, "plain.txt", b"same")
        plain = _write(self.cull, "plain.txt", b"same")
        # A cull-only subdir holding only a relative symlink: the carve-out
        # sweeps the link and removes the directory.
        loose = os.path.join(self.cull, "loose")
        os.makedirs(loose)
        link = os.path.join(loose, "link")
        os.symlink("../run.sh", link)
        _stamp(self.db, self.actual, self.cull)
        code, out, _ = self._dedupe("--delete")
        self.assertEqual(code, 1)
        self.assertIn("run.sh*", out)          # executable duplicate
        self.assertIn(link + "@", out)         # swept symlink
        self.assertIn(loose + "/", out)        # rmdir'd directory
        self.assertIn(plain + "\n", out)       # plain file: no indicator

    def test_directory_vanishing_mid_walk_is_survived(self):
        # A cull subdir removed *during* the walk (as when files are deleted
        # from the cull tree concurrently) must not crash the run: it is
        # skipped with a warning and the rest of the cull is still processed.
        _write(self.actual, "keep/a.txt", b"same")
        _write(self.cull, "keep/b.txt", b"same")
        _write(self.actual, "gone/x.txt", b"dup")
        _write(self.cull, "gone/y.txt", b"dup")
        _stamp(self.db, self.actual, self.cull)

        real_scan = dedupe._scan
        victim = os.path.join(self.cull, "gone")

        def flaky_scan(path):
            # Delete the cull 'gone' dir the instant dedupe scans it, exactly
            # as a concurrent deletion would, so the scan raises mid-walk.
            if os.path.abspath(path) == victim:
                shutil.rmtree(path)
            return real_scan(path)

        with mock.patch.object(dedupe, "_scan", flaky_scan):
            code, out, err = self._dedupe("--delete")

        self.assertNotIn("Traceback", err)
        self.assertIn("vanished", err)
        # the other directory was still processed to completion
        self.assertFalse(os.path.exists(os.path.join(self.cull, "keep", "b.txt")))

    def test_cull_root_survives_even_when_emptied(self):
        _write(self.actual, "a.txt", b"same")
        _write(self.cull, "b.txt", b"same")
        _stamp(self.db, self.actual, self.cull)
        code, _, _ = self._dedupe("--delete")
        self.assertEqual(code, 1)
        self.assertTrue(os.path.isdir(self.cull), "placeholder rule")
        self.assertEqual(os.listdir(self.cull), [])

    def test_matching_is_per_synced_directory_not_whole_tree(self):
        # Same digest exists in actual, but in a *different* directory:
        # directory matters, so the cull file survives.
        _write(self.actual, "sub/a.txt", b"same")
        _write(self.cull, "b.txt", b"same")
        _stamp(self.db, self.actual, self.cull)
        code, _, _ = self._dedupe("--delete")
        self.assertEqual(code, 0)
        self.assertTrue(os.path.exists(os.path.join(self.cull, "b.txt")))

    def test_synced_subdirs_recurse_and_empty_ones_vanish(self):
        _write(self.actual, "sub/deep/a.txt", b"same")
        _write(self.cull, "sub/deep/b.txt", b"same")
        _stamp(self.db, self.actual, self.cull)
        code, _, _ = self._dedupe("--delete")
        self.assertEqual(code, 1)
        self.assertFalse(os.path.exists(os.path.join(self.cull, "sub")),
                         "emptied directories cascade up")
        self.assertTrue(os.path.isdir(self.cull))

    def test_cull_only_subdir_with_content_is_untouched(self):
        _write(self.actual, "a.txt", b"same")
        _write(self.cull, "b.txt", b"same")
        _write(self.cull, "lonely/keep.txt", b"same")  # no actual/lonely
        _stamp(self.db, self.actual, self.cull)
        code, _, _ = self._dedupe("--delete")
        self.assertEqual(code, 1)
        self.assertTrue(os.path.exists(
            os.path.join(self.cull, "lonely", "keep.txt")),
            "sync rule: never descended, so never matched")

    def test_carveout_sweeps_empty_cull_only_dirs(self):
        _write(self.actual, "a.txt", b"same")
        _write(self.cull, "b.txt", b"same")
        os.makedirs(os.path.join(self.cull, "empty", "nested"))
        _write(self.cull, "empty/.DS_Store", b"finder junk")
        _stamp(self.db, self.actual, self.cull)
        code, _, _ = self._dedupe("--delete")
        self.assertEqual(code, 1)
        self.assertFalse(os.path.exists(os.path.join(self.cull, "empty")))
        self.assertEqual(os.listdir(self.cull), [])

    def test_stale_mtime_vetoes_deletion(self):
        _write(self.actual, "a.txt", b"same")
        cull_file = _write(self.cull, "b.txt", b"same")
        _stamp(self.db, self.actual, self.cull)
        with open(cull_file, "ab") as f:  # modified after stamping
            f.write(b" drifted")
        code, _, err = self._dedupe("--delete")
        self.assertEqual(code, 0)
        self.assertTrue(os.path.exists(cull_file))
        self.assertIn("modified since stamped", err)

    def test_unknown_files_are_invisible(self):
        _write(self.actual, "a.txt", b"same")
        _write(self.cull, "known-unique.txt", b"only here")
        _stamp(self.db, self.actual, self.cull)
        unknown = _write(self.cull, "b.txt", b"same")  # never stamped
        code, _, _ = self._dedupe("--delete")
        self.assertEqual(code, 0)
        self.assertTrue(os.path.exists(unknown))

    def test_overlap_and_identity_refusals(self):
        nested = os.path.join(self.actual, "inner")
        os.makedirs(nested)
        code, _, err = _run(["--database", self.db, self.actual, nested])
        self.assertEqual(code, 2)
        self.assertIn("overlap", err)
        code, _, err = _run(["--database", self.db, self.actual, self.actual])
        self.assertEqual(code, 2)
        self.assertIn("same directory", err)

    def test_missing_db_and_unknown_root_are_exit_2(self):
        _write(self.actual, "a.txt", b"x")
        _write(self.cull, "b.txt", b"x")
        code, _, err = self._dedupe()
        self.assertEqual(code, 2)
        self.assertIn("no such database", err)
        _stamp(self.db, self.actual)  # cull never scanned
        code, _, err = self._dedupe()
        self.assertEqual(code, 2)
        self.assertIn("no database rows", err)

    def test_symlink_rules(self):
        _write(self.actual, "sub/a.txt", b"same")
        _write(self.cull, "sub/b.txt", b"same")
        _stamp(self.db, self.actual, self.cull)
        # Relative link inside the cull tree: exit junk, swept.
        os.symlink("b.txt", os.path.join(self.cull, "sub", "inside-link"))
        # Absolute link: real content, blocks its directory forever.
        os.symlink(self.actual, os.path.join(self.cull, "outside-link"))
        code, _, _ = self._dedupe("--delete")
        self.assertEqual(code, 1)
        self.assertFalse(os.path.exists(os.path.join(self.cull, "sub")),
                         "duplicate deleted, inside-link swept, dir removed")
        self.assertTrue(os.path.islink(os.path.join(self.cull, "outside-link")),
                        "absolute symlink is never deleted")

    def test_ds_store_swept_only_at_exit(self):
        _write(self.actual, "a.txt", b"same")
        _write(self.cull, "b.txt", b"same")
        _write(self.cull, "busy/unique.txt", b"only here")
        os.makedirs(os.path.join(self.actual, "busy"))
        _write(self.actual, "busy/x.txt", b"other")
        _write(self.cull, "busy/.DS_Store", b"junk")
        _stamp(self.db, self.actual, self.cull)
        code, _, _ = self._dedupe("--delete")
        self.assertEqual(code, 1)
        self.assertTrue(os.path.exists(
            os.path.join(self.cull, "busy", ".DS_Store")),
            "a blocked directory keeps its ignorables")

    # --- post-crash reconciliation coverage (2026-07-16) -------------------

    def test_absolute_symlink_is_never_deletable_even_into_cull(self):
        # Confirmed rule: an absolute symlink is content wherever it points,
        # even into the cull tree; only relative inside-pointing links sweep.
        _write(self.actual, "sub/a.txt", b"same")
        _write(self.cull, "sub/b.txt", b"same")
        _stamp(self.db, self.actual, self.cull)
        abs_link = os.path.join(self.cull, "sub", "abs-inside")
        os.symlink(os.path.join(self.cull, "sub", "b.txt"), abs_link)
        code, _, _ = self._dedupe("--delete")
        self.assertEqual(code, 1)
        self.assertFalse(os.path.exists(os.path.join(self.cull, "sub", "b.txt")),
                         "the duplicate itself is still culled")
        self.assertTrue(os.path.islink(abs_link),
                        "the absolute link survives (now dangling) and "
                        "blocks its directory")
        self.assertTrue(os.path.isdir(os.path.join(self.cull, "sub")))

    def test_dangling_relative_link_into_cull_is_still_swept(self):
        # Where a relative link points is what matters, not whether its
        # target still exists -- the deletion phase just emptied it.
        _write(self.actual, "sub/a.txt", b"same")
        _write(self.cull, "sub/b.txt", b"same")
        _stamp(self.db, self.actual, self.cull)
        os.symlink("b.txt", os.path.join(self.cull, "sub", "rel-link"))
        code, _, _ = self._dedupe("--delete")
        self.assertEqual(code, 1)
        self.assertFalse(os.path.exists(os.path.join(self.cull, "sub")),
                         "duplicate deleted, dangling relative link swept, "
                         "directory removed")

    def test_stale_witness_vouches_for_nothing(self):
        wit = _write(self.actual, "a.txt", b"same")
        cull_file = _write(self.cull, "b.txt", b"same")
        _stamp(self.db, self.actual, self.cull)
        with open(wit, "ab") as f:  # actual side modified after stamping
            f.write(b" drifted")
        code, _, err = self._dedupe("--delete")
        self.assertEqual(code, 0)
        self.assertTrue(os.path.exists(cull_file))
        self.assertIn("not used as a witness", err)

    def test_size_mismatch_is_refused_loudly(self):
        wit = _write(self.actual, "a.txt", b"0123456789")
        dup = _write(self.cull, "b.txt", b"01234567890123456789")
        _stamp(self.db, self.actual, self.cull)
        # Forge a digest collision: point the cull row AND its xattr at the
        # witness's digest while the live sizes differ. (Setting an xattr
        # changes ctime, not mtime, so the file still reads as fresh.)
        conn = sqlite3.connect(self.db)
        wd = conn.execute("SELECT digest FROM files WHERE rel_path "
                          "LIKE '%a.txt'").fetchone()[0]
        conn.execute("UPDATE files SET digest = ? WHERE rel_path "
                     "LIKE '%b.txt'", (wd,))
        conn.commit(); conn.close()
        meta = schema.loads(xattr.get(dup, schema.XATTR_NAME))
        meta["digests"] = {next(iter(meta["digests"])): wd}
        xattr.set(dup, schema.XATTR_NAME, schema.dumps(meta))
        code, _, err = self._dedupe("--delete")
        self.assertEqual(code, 2)
        self.assertTrue(os.path.exists(dup))
        self.assertIn("MISMATCH", err)

    def test_mixed_algorithms_refused_without_allow_mixed(self):
        _write(self.actual, "a.txt", b"apples")
        _write(self.cull, "b.txt", b"oranges")
        _stamp(self.db, self.actual, self.cull)
        conn = sqlite3.connect(self.db)
        conn.execute("UPDATE files SET algo = 'md5' WHERE rel_path "
                     "LIKE '%b.txt'")
        conn.commit(); conn.close()
        code, _, err = self._dedupe("--delete")
        self.assertEqual(code, 2)
        self.assertIn("mixed digest algorithms", err)
        code, _, _ = self._dedupe("--delete", "--allow-mixed")
        self.assertEqual(code, 0)  # nothing matches across algos

    def test_fence_blocks_deletion_and_removal(self):
        _write(self.actual, "sub/a.txt", b"same")
        fenced_dup = _write(self.cull, "sub/b.txt", b"same")
        _write(self.actual, "c.txt", b"other")
        top_dup = _write(self.cull, "c.txt", b"other")
        _stamp(self.db, self.actual, self.cull)
        _write(self.cull, f"sub/{dedupe.MARKER}", b"")  # fence after stamping
        code, out, _ = self._dedupe("--delete")
        self.assertEqual(code, 1)                       # c.txt still culled
        self.assertFalse(os.path.exists(top_dup))
        self.assertTrue(os.path.exists(fenced_dup), "the fence held")
        self.assertTrue(os.path.isdir(os.path.join(self.cull, "sub")))
        self.assertIn("fenced", out)

    def test_fence_in_cull_only_subtree_blocks_the_carveout(self):
        _write(self.actual, "a.txt", b"same")
        _write(self.cull, "b.txt", b"same")
        os.makedirs(os.path.join(self.cull, "lonely"))
        _write(self.cull, "lonely/.DS_Store", b"junk")
        _stamp(self.db, self.actual, self.cull)
        _write(self.cull, f"lonely/{dedupe.MARKER}", b"")
        code, _, _ = self._dedupe("--delete")
        self.assertEqual(code, 1)
        self.assertTrue(os.path.exists(
            os.path.join(self.cull, "lonely", ".DS_Store")),
            "a fenced junk-only directory is not swept")

    def test_fence_on_cull_root_does_nothing(self):
        _write(self.actual, "a.txt", b"same")
        dup = _write(self.cull, "b.txt", b"same")
        _stamp(self.db, self.actual, self.cull)
        _write(self.cull, dedupe.MARKER, b"")
        code, _, err = self._dedupe("--delete")
        self.assertEqual(code, 0)
        self.assertTrue(os.path.exists(dup))
        self.assertIn("nothing to do", err)

    def test_hard_link_is_deleted_with_a_note(self):
        wit = _write(self.actual, "a.txt", b"same")
        dup = os.path.join(self.cull, "b.txt")
        os.link(wit, dup)
        _stamp(self.db, self.actual, self.cull)
        code, _, err = self._dedupe("--delete")
        self.assertEqual(code, 1)
        self.assertFalse(os.path.lexists(dup))
        self.assertTrue(os.path.exists(wit), "the data keeps its actual name")
        self.assertIn("hard link", err)


class OfflineTests(unittest.TestCase):
    """The -n/--offline prediction: database-only, no filesystem access."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.actual = os.path.join(self._tmp.name, "actual")
        self.cull = os.path.join(self._tmp.name, "cull")
        self.db = os.path.join(self._tmp.name, "db.sqlite")
        os.makedirs(self.actual)
        os.makedirs(self.cull)

    def _offline(self, *extra: str) -> tuple[int, str, str]:
        return _run(["--database", self.db, self.actual, self.cull,
                     "-n", *extra])

    def test_predicts_with_both_trees_gone(self):
        # The whole point: after the "drive" disappears, the prediction
        # still runs from rows alone.
        _write(self.actual, "a.txt", b"same")
        _write(self.cull, "sub/b.txt", b"same")
        _write(self.actual, "sub/w.txt", b"same")
        _stamp(self.db, self.actual, self.cull)
        shutil.rmtree(self.actual)
        shutil.rmtree(self.cull)
        code, out, _ = self._offline()
        self.assertEqual(code, 1)
        self.assertIn("might delete", out)
        self.assertIn(os.path.join(self.cull, "sub", "b.txt"), out)
        self.assertIn("offline: predicting from database contents alone",
                      out)
        self.assertNotIn("would delete", out)
        self.assertNotIn("rmdir", out)

    def test_touches_nothing_not_even_the_database(self):
        _write(self.actual, "a.txt", b"same")
        _write(self.cull, "b.txt", b"same")
        _stamp(self.db, self.actual, self.cull)
        conn = sqlite3.connect(self.db)
        before = conn.execute("SELECT COUNT(*) FROM files").fetchone()[0]
        conn.close()
        code, _, _ = self._offline()
        self.assertEqual(code, 1)
        self.assertTrue(os.path.exists(os.path.join(self.cull, "b.txt")))
        conn = sqlite3.connect(self.db)
        after = conn.execute("SELECT COUNT(*) FROM files").fetchone()[0]
        conn.close()
        self.assertEqual(before, after)

    def test_same_relative_directory_rule_holds_offline(self):
        # Same digest in a *different* relative directory witnesses nothing,
        # exactly like the live walk.
        _write(self.actual, "sub/a.txt", b"same")
        _write(self.cull, "b.txt", b"same")
        _stamp(self.db, self.actual, self.cull)
        code, out, _ = self._offline()
        self.assertEqual(code, 0)
        self.assertNotIn("might delete " + os.path.join(self.cull, "b.txt"),
                         out)

    def test_conflicts_with_delete(self):
        with self.assertRaises(SystemExit) as ctx:
            _run(["--database", self.db, self.actual, self.cull,
                  "-n", "--delete"])
        self.assertEqual(ctx.exception.code, 2)

    def test_unknown_root_is_exit_2(self):
        # A root with no rows (a wrong spelling, say) is a refusal, not a
        # silent zero-match run.
        _write(self.actual, "a.txt", b"same")
        _write(self.cull, "b.txt", b"same")
        _stamp(self.db, self.actual, self.cull)
        nowhere = os.path.join(self._tmp.name, "nowhere")
        code, _, err = _run(["--database", self.db, self.actual, nowhere,
                             "-n"])
        self.assertEqual(code, 2)
        self.assertIn("no database rows", err)

    def test_overlapping_roots_refused_offline(self):
        _write(self.actual, "sub/a.txt", b"same")
        _stamp(self.db, self.actual)
        code, _, err = _run(["--database", self.db, self.actual,
                             os.path.join(self.actual, "sub"), "-n"])
        self.assertEqual(code, 2)
        self.assertIn("overlap", err)

    def test_recorded_size_mismatch_is_refused_loudly(self):
        # --locate populates sizes; a doctored row simulates the collision
        # case (same digest, different recorded size).
        _write(self.actual, "a.txt", b"same")
        _write(self.cull, "b.txt", b"same")
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = cli.main(["--sum", "--locate", "-q",
                             "--database", self.db, self.actual, self.cull])
        self.assertEqual(code, 0, err.getvalue())
        conn = sqlite3.connect(self.db)
        conn.execute("UPDATE files SET size = 999 "
                     "WHERE rel_path LIKE '%b.txt'")
        conn.commit()
        conn.close()
        code, out, err = self._offline()
        self.assertEqual(code, 2)
        self.assertIn("MISMATCH", err)
        self.assertIn("might delete: 0 files", out)
        self.assertNotIn("might delete /", out)

    def test_unknown_sizes_are_flagged_in_the_summary(self):
        # Stamped without --locate, sizes are NULL: the byte total can't
        # be known and the summary says so.
        _write(self.actual, "a.txt", b"same")
        _write(self.cull, "b.txt", b"same")
        _stamp(self.db, self.actual, self.cull)
        code, out, _ = self._offline()
        self.assertEqual(code, 1)
        self.assertIn("of unknown size", out)


if __name__ == "__main__":
    unittest.main()
