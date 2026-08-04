"""Unit tests for the traversal's pure helpers (sumtag.walk).

Independent-oracle tests for the sort key behind the deterministic
traversal order and the --exclude basename matcher.  The walker itself
(iter_files) is covered end-to-end by tests/test_exclude.py,
tests/test_symlinks.py, and the conformance harness; these pin the two
I/O-free pieces directly.  Authorities: CLAUDE.md "What it does" (traversal
order) and "Command-line exclusion (--exclude)".
"""

from __future__ import annotations

import unittest

from sumtag.walk import _excluded_by, _name_key


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


if __name__ == "__main__":
    unittest.main()
