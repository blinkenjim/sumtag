"""Unit tests for --exclude (walk-level glob exclusion; CLAUDE.md "--exclude").

Covers the traversal behavior in sumtag.walk.iter_files directly: basename
glob matching, directory pruning, repeatability, independence from
@sumtag-ignore/--no-ignore, and the excluded-scan-root warning.
"""

from __future__ import annotations

import os
import tempfile
import unittest

from sumtag import walk


def _touch(path: str) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        f.write("x")


class ExcludeWalkTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = self._tmp.name

    def _walk(self, **kwargs) -> list[str]:
        rels = [os.path.relpath(p, self.root)
                for p in walk.iter_files([self.root], **kwargs)]
        return rels

    def test_no_exclude_yields_everything(self):
        _touch(f"{self.root}/a.txt")
        _touch(f"{self.root}/sub/b.vob")
        self.assertEqual(self._walk(), ["a.txt", os.path.join("sub", "b.vob")])
        # exclude=None and exclude=[] behave identically
        self.assertEqual(self._walk(exclude=[]), self._walk(exclude=None))

    def test_file_glob_matches_basename_anywhere(self):
        _touch(f"{self.root}/a.txt")
        _touch(f"{self.root}/b.vob")
        _touch(f"{self.root}/sub/deep/c.vob")
        _touch(f"{self.root}/sub/d.txt")
        self.assertEqual(self._walk(exclude=["*.vob"]),
                         ["a.txt", os.path.join("sub", "d.txt")])

    def test_matching_directory_prunes_subtree(self):
        _touch(f"{self.root}/VIDEO_TS/a.txt")
        _touch(f"{self.root}/sub/VIDEO_TS/b.txt")
        _touch(f"{self.root}/sub/keep.txt")
        self.assertEqual(self._walk(exclude=["VIDEO_TS"]),
                         [os.path.join("sub", "keep.txt")])

    def test_pruned_directory_not_announced(self):
        _touch(f"{self.root}/VIDEO_TS/a.txt")
        _touch(f"{self.root}/keep/b.txt")
        seen: list[str] = []
        list(walk.iter_files([self.root], exclude=["VIDEO_TS"],
                             on_dir=seen.append))
        self.assertEqual(seen, [self.root, os.path.join(self.root, "keep")])

    def test_patterns_are_repeatable(self):
        _touch(f"{self.root}/a.txt")
        _touch(f"{self.root}/b.vob")
        _touch(f"{self.root}/c.iso")
        self.assertEqual(self._walk(exclude=["*.vob", "*.iso"]), ["a.txt"])

    def test_matching_is_case_sensitive(self):
        _touch(f"{self.root}/a.VOB")
        _touch(f"{self.root}/b.vob")
        self.assertEqual(self._walk(exclude=["*.vob"]), ["a.VOB"])

    def test_basename_only_slash_pattern_matches_nothing(self):
        # Patterns match basenames only (decided 2026-07-13); a pattern with
        # a path separator can never match a basename, so it excludes nothing.
        _touch(f"{self.root}/sub/a.txt")
        self.assertEqual(self._walk(exclude=["sub/a.txt"]),
                         [os.path.join("sub", "a.txt")])

    def test_independent_of_no_ignore(self):
        # --no-ignore (respect_ignore=False) disregards markers only;
        # --exclude still applies.
        _touch(f"{self.root}/marked/{walk.IGNORE_MARKER}")
        _touch(f"{self.root}/marked/a.txt")
        _touch(f"{self.root}/b.vob")
        _touch(f"{self.root}/c.txt")
        self.assertEqual(self._walk(respect_ignore=False, exclude=["*.vob"]),
                         ["c.txt", os.path.join("marked", "a.txt")])

    def test_exclude_composes_with_marker(self):
        _touch(f"{self.root}/marked/{walk.IGNORE_MARKER}")
        _touch(f"{self.root}/marked/a.txt")
        _touch(f"{self.root}/b.vob")
        _touch(f"{self.root}/c.txt")
        self.assertEqual(self._walk(exclude=["*.vob"]), ["c.txt"])

    def test_excluded_scan_root_warns_and_skips(self):
        _touch(f"{self.root}/tree/a.txt")
        start = os.path.join(self.root, "tree")
        warnings: list[str] = []
        got = list(walk.iter_files([start], exclude=["tree"],
                                   on_warn=warnings.append))
        self.assertEqual(got, [])
        self.assertEqual(len(warnings), 1)
        self.assertIn("--exclude", warnings[0])
        self.assertIn(start, warnings[0])

    def test_excluded_file_root_warns_and_skips(self):
        _touch(f"{self.root}/a.vob")
        start = os.path.join(self.root, "a.vob")
        warnings: list[str] = []
        got = list(walk.iter_files([start], exclude=["*.vob"],
                                   on_warn=warnings.append))
        self.assertEqual(got, [])
        self.assertEqual(len(warnings), 1)


class ExcludeCliTests(unittest.TestCase):
    def test_flag_is_repeatable_and_defaults_empty(self):
        from sumtag.cli import build_parser
        parser = build_parser()
        args = parser.parse_args(["."])
        self.assertEqual(args.exclude, [])
        args = parser.parse_args(["--exclude", "*.vob", "--exclude=VIDEO_TS", "."])
        self.assertEqual(args.exclude, ["*.vob", "VIDEO_TS"])


if __name__ == "__main__":
    unittest.main()
