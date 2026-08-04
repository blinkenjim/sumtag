"""Unit tests for the traversal's pure helpers (sumtag.walk).

Independent-oracle tests for the sort key behind the deterministic
traversal order and the --exclude basename matcher.  The walker itself
(iter_files) is covered end-to-end by tests/test_exclude.py,
tests/test_symlinks.py, and the conformance harness; these pin the two
I/O-free pieces directly.  Authorities: CLAUDE.md "What it does" (traversal
order) and "Command-line exclusion (--exclude)".
"""

from __future__ import annotations

import os
import tempfile
import unittest

from sumtag.walk import IGNORE_MARKER, _excluded_by, _name_key, iter_files


class NameKeyTests(unittest.TestCase):
    """The traversal sort key: "ascending case-insensitive alphabetical
    order (casefolded, with the raw name as a tie-break for case-only twins
    like README/readme)" -- CLAUDE.md, made case-insensitive 2026-07-21 to
    match Finder's ordering.
    """

    def test_case_insensitive_ordering(self):
        # Finder-style: 'apple' sorts before 'Banana' despite ASCII putting
        # every uppercase letter first.
        names = ["Banana", "apple", "Cherry", "date"]
        self.assertEqual(sorted(names, key=_name_key),
                         ["apple", "Banana", "Cherry", "date"])

    def test_case_only_twins_are_deterministic(self):
        # Casefold ties break on the raw name, ascending -- so the ordering
        # of README vs readme is fixed (uppercase first, ASCII order), never
        # dependent on input order.
        self.assertEqual(sorted(["readme", "README"], key=_name_key),
                         ["README", "readme"])
        self.assertEqual(sorted(["README", "readme"], key=_name_key),
                         ["README", "readme"])

    def test_key_is_total_and_stable(self):
        # Distinct names never compare equal under the key (the raw name is
        # part of it), so the order is total: any input permutation sorts to
        # the same sequence.
        names = ["b", "B", "a", "A", "ab", "aB"]
        expected = sorted(names, key=_name_key)
        self.assertEqual(sorted(reversed(names), key=_name_key), expected)
        self.assertEqual(len({_name_key(n) for n in names}), len(names))


class ExcludedByTests(unittest.TestCase):
    """The --exclude matcher: fnmatch-style glob against the basename only,
    case-sensitively on every platform (CLAUDE.md "--exclude").
    """

    def test_glob_matches(self):
        self.assertIsNotNone(_excluded_by("movie.vob", ["*.vob"]))
        self.assertIsNotNone(_excluded_by("VIDEO_TS", ["VIDEO_TS"]))
        self.assertIsNotNone(_excluded_by("cache-2026", ["cache-*"]))
        self.assertIsNotNone(_excluded_by("a.pyc", ["*.py?"]))

    def test_no_match(self):
        self.assertIsNone(_excluded_by("movie.txt", ["*.vob"]))
        self.assertIsNone(_excluded_by("anything", []))

    def test_case_sensitive_on_every_platform(self):
        # fnmatchcase semantics: "the same name matches the same pattern on
        # macOS and Linux" -- so no normcase folding, ever.
        self.assertIsNone(_excluded_by("MOVIE.VOB", ["*.vob"]))
        self.assertIsNone(_excluded_by("video_ts", ["VIDEO_TS"]))

    def test_any_pattern_suffices(self):
        # Repeatable flag: a name matching ANY pattern is excluded.
        self.assertIsNotNone(_excluded_by("b.iso", ["*.vob", "*.iso"]))

    def test_returns_the_matching_pattern(self):
        # The return value is the pattern that matched -- it is quoted in
        # the scan-root warning ("matches --exclude '<pat>'").  Which of
        # several matching patterns is reported is cosmetic; that the
        # returned value IS a matching pattern is the contract.
        self.assertEqual(_excluded_by("movie.vob", ["*.vob"]), "*.vob")
        got = _excluded_by("movie.vob", ["*.v??", "movie.*"])
        self.assertIn(got, ("*.v??", "movie.*"))

    def test_slash_pattern_never_matches_a_basename(self):
        # "a pattern containing / can never match a basename and therefore
        # excludes nothing" -- basenames contain no slash.
        self.assertIsNone(_excluded_by("b", ["a/b"]))
        self.assertIsNone(_excluded_by("sub", ["sub/*"]))


class IterFilesTests(unittest.TestCase):
    """The walker itself, against real temp trees.  Authorities: CLAUDE.md
    "What it does" (deterministic order), "Ignore markers", and the on_dir
    contract (each visited directory announced after the prune check,
    before any of its files).  --exclude and symlink behavior are covered
    by tests/test_exclude.py and tests/test_symlinks.py.
    """

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = self._tmp.name

    def _touch(self, rel: str) -> None:
        path = os.path.join(self.root, rel)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w") as f:
            f.write("x")

    def _rels(self, **kwargs) -> list[str]:
        return [os.path.relpath(p, self.root)
                for p in iter_files([self.root], **kwargs)]

    def test_order_is_files_first_then_subdirs_case_insensitive(self):
        # Within each directory: files in case-insensitive alphabetical
        # order, THEN recursion into subdirectories in the same order --
        # so the stream tracks a Finder window top to bottom.
        for rel in ("Beta.txt", "alpha.txt", "Sub/inner.txt", "ant/z.txt"):
            self._touch(rel)
        self.assertEqual(self._rels(), [
            "alpha.txt", "Beta.txt",                  # root files, casefolded
            os.path.join("ant", "z.txt"),             # then subdirs, in order:
            os.path.join("Sub", "inner.txt"),         # 'ant' < 'Sub' casefolded
        ])

    def test_case_only_twin_files_keep_the_tiebreak_order(self):
        # README before readme: casefold tie broken by the raw name.  On a
        # case-insensitive filesystem (macOS's default APFS) the two names
        # are one file and the twin case cannot exist on disk -- skip; the
        # ordering logic itself is pinned by NameKeyTests against the pure
        # sort key.
        self._touch("README")
        self._touch("readme")
        if len(os.listdir(self.root)) < 2:
            self.skipTest("filesystem is case-insensitive; twins collapse")
        self.assertEqual(self._rels(), ["README", "readme"])

    def test_marker_prunes_the_whole_subtree(self):
        self._touch("keep.txt")
        self._touch("vendor/blob.bin")
        self._touch("vendor/deep/nested.bin")
        self._touch(f"vendor/{IGNORE_MARKER}")
        self.assertEqual(self._rels(), ["keep.txt"])

    def test_no_ignore_overrides_markers_but_never_yields_them(self):
        # respect_ignore=False processes the fenced tree; the marker FILE
        # itself is still never yielded ("never hashed or stamped").
        self._touch("vendor/blob.bin")
        self._touch(f"vendor/{IGNORE_MARKER}")
        self.assertEqual(self._rels(respect_ignore=False),
                         [os.path.join("vendor", "blob.bin")])

    def test_marker_on_scan_root_warns_and_skips(self):
        self._touch("a.txt")
        self._touch(IGNORE_MARKER)
        warnings: list[str] = []
        self.assertEqual(self._rels(on_warn=warnings.append), [])
        self.assertEqual(len(warnings), 1)
        self.assertIn("@sumtag-ignore on scan root", warnings[0])

    def test_marker_below_root_is_silent(self):
        # Only a marker on the EXPLICIT root earns a warning; interior
        # markers prune silently.
        self._touch("keep.txt")
        self._touch(f"sub/{IGNORE_MARKER}")
        warnings: list[str] = []
        self.assertEqual(self._rels(on_warn=warnings.append), ["keep.txt"])
        self.assertEqual(warnings, [])

    def test_on_dir_announces_visited_dirs_only_before_their_files(self):
        self._touch("a/f1.txt")
        self._touch("b/f2.txt")
        self._touch(f"b/{IGNORE_MARKER}")
        events: list[tuple[str, str]] = []
        for path in iter_files([self.root],
                               on_dir=lambda d: events.append(("dir", d))):
            events.append(("file", path))
        # Pruned 'b' draws no on_dir call; every yielded file follows its
        # directory's announcement.
        self.assertEqual(events, [
            ("dir", self.root),
            ("dir", os.path.join(self.root, "a")),
            ("file", os.path.join(self.root, "a", "f1.txt")),
        ])

    def test_file_root_is_yielded_directly(self):
        # An explicit file argument is the user's explicit claim.
        self._touch("plain.txt")
        target = os.path.join(self.root, "plain.txt")
        self.assertEqual(list(iter_files([target])), [target])

    def test_roots_processed_in_the_order_given(self):
        self._touch("r1/z.txt")
        self._touch("r2/a.txt")
        r1, r2 = (os.path.join(self.root, r) for r in ("r1", "r2"))
        self.assertEqual(list(iter_files([r2, r1])), [
            os.path.join(r2, "a.txt"), os.path.join(r1, "z.txt")])


if __name__ == "__main__":
    unittest.main()
