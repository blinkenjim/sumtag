"""Unit tests for the CLI grammar and conflict matrix (sumtag.cli).

Test-only coverage (no body rewrites -- the orchestration tier of the
re-code plan): every documented conflict and requires-relationship from
CLAUDE.md "Flags" / "CLI usage", table-driven.  Error assertions check
that the message names the flags involved -- pinning WHICH rule fired --
without freezing cosmetic wording.  The --progress/-q order resolution is
tested directly against _resolve_progress_quiet.
"""

from __future__ import annotations

import contextlib
import io
import unittest

from sumtag import cli

#: Argv fragments that must be REJECTED at parse/validate time, with the
#: substrings their error must contain (all of them).  Every row includes a
#: directory argument so only the rule under test can fire.
REJECTED: list[tuple[list[str], list[str]]] = [
    # The mandatory-action rule: a bare run errors naming the choices.
    (["/data"], ["--sum", "--verify", "--remove", "--import", "--locate",
                 "--prune-dirs", "--prune-all"]),
    # Global pairings.
    (["--sum", "-q", "-v", "/d"], ["-q", "-v"]),
    (["--sum", "-f", "-n", "/d"], ["--force", "--dry-run"]),
    # Database requirements.
    (["--import", "/d"], ["--import", "--database"]),
    (["--locate", "/d"], ["--locate", "--database"]),
    (["--sum", "--verify", "/d"], ["--verify", "--sum"]),
    # --database with no action on it.
    (["--database", "x.sqlite", "--verify", "/d"], ["--verify", "--database"]),
    # --verify's conflicts.
    (["--verify", "-f", "/d"], ["--verify", "--force"]),
    (["--verify", "--import", "--database", "x", "/d"],
     ["--verify", "--import"]),
    (["--verify", "--locate", "--database", "x", "/d"],
     ["--verify", "--locate"]),
    # --remove's conflicts.
    (["--remove", "--database", "x", "--sum", "/d"], ["--remove"]),
    (["--remove", "--sum", "/d"], ["--remove", "--sum"]),
    (["--remove", "--verify", "/d"], ["--remove", "--verify"]),
    (["--remove", "-f", "/d"], ["--remove", "--force"]),
    (["--remove", "--import", "--database", "x", "/d"],
     ["--remove", "--import"]),
    (["--remove", "--locate", "--database", "x", "/d"],
     ["--remove", "--locate"]),
    # The prune flags.
    (["--prune-dirs", "/d"], ["--prune-dirs", "--database"]),
    (["--prune-all", "/d"], ["--prune-all", "--database"]),
    (["--prune-dirs", "--database", "x", "--sum", "/d"],
     ["--prune-dirs", "--sum"]),
    (["--prune-dirs", "--database", "x", "--verify", "/d"],
     ["--verify"]),
    (["--prune-all", "--database", "x", "--remove", "/d"],
     ["--prune-all", "--remove"]),
    (["--prune-dirs", "--database", "x", "-f", "/d"],
     ["--prune-dirs", "--force"]),
    (["--prune-dirs", "--database", "x", "--prescan", "/d"],
     ["--prune-dirs", "--prescan"]),
    (["--prune-all", "--database", "x", "--db-prescan", "/d"],
     ["--prune-all"]),
    (["--prune-dirs", "--database", "x", "--exclude", "*.vob", "/d"],
     ["--prune-dirs", "--exclude"]),
    (["--prune-all", "--database", "x", "--no-ignore", "/d"],
     ["--prune-all", "--no-ignore"]),
    # Prescan pairings.
    (["--remove", "--prescan", "/d"], ["--prescan", "--remove"]),
    (["--sum", "--db-prescan", "/d"], ["--db-prescan", "--database"]),
    (["--sum", "--database", "x", "--db-prescan", "--prescan", "/d"],
     ["--db-prescan", "--prescan"]),
    (["--remove", "--db-prescan", "--database", "x", "/d"],
     ["--db-prescan"]),
]

#: Argv fragments that must be ACCEPTED (parse + validate cleanly).
ACCEPTED: list[list[str]] = [
    ["--sum", "/d"],                                   # sum needs no database
    ["--sum", "-n", "/d"],
    ["--sum", "-f", "/d"],
    ["--sum", "-vv", "/d"],
    ["--sum", "-qq", "/d"],
    ["--verify", "/d"],
    ["--verify", "-n", "/d"],                          # redundant no-op, allowed
    ["--remove", "/d"],
    ["--remove", "-n", "/d"],                          # the preview
    ["--database", "x.sqlite", "--sum", "/d"],
    ["--database", "x.sqlite", "--import", "/d"],
    ["--database", "x.sqlite", "--locate", "/d"],      # implies --import
    ["--database", "x.sqlite", "--import", "--locate", "/d"],   # redundant, ok
    ["--database", "x.sqlite", "--sum", "--import", "/d"],      # redundant, ok
    ["--database", "x.sqlite", "--sum", "--locate", "/d"],
    ["--database", "x.sqlite", "--import", "-f", "/d"],  # force overrides
    ["--database", "x.sqlite", "--locate", "-f", "/d"],
    ["--database", "x.sqlite", "--import", "-n", "/d"],
    ["--prune-dirs", "--database", "x", "/d"],
    ["--prune-all", "--database", "x", "/d"],
    ["--prune-dirs", "--prune-all", "--database", "x", "/d"],   # redundant, ok
    ["--prune-dirs", "--database", "x", "-n", "/d"],
    ["--sum", "--prescan", "/d"],
    ["--sum", "--database", "x", "--prescan", "/d"],
    ["--sum", "--database", "x", "--db-prescan", "/d"],
    ["--sum", "--database", "x", "--db-prescan", "-n", "/d"],
    ["--sum", "--exclude", "*.vob", "--exclude", "VIDEO_TS", "/d"],
    ["--sum", "--no-ignore", "/d"],
    ["--verify", "--prescan", "/d"],
    ["--sum", "--progress", "--si", "/d"],
    ["--sum", ".", "/data", "/backup"],                # multiple roots
]


def _validate(argv: list[str]) -> tuple[int | None, str]:
    """Parse+validate argv; return (SystemExit code or None, stderr text)."""
    parser = cli.build_parser()
    err = io.StringIO()
    with contextlib.redirect_stderr(err):
        try:
            args = parser.parse_args(argv)
            cli.validate(args, parser)
        except SystemExit as e:
            return e.code, err.getvalue()
    return None, err.getvalue()


class ConflictMatrixTests(unittest.TestCase):
    def test_rejected_combinations(self):
        for argv, needles in REJECTED:
            with self.subTest(argv=" ".join(argv)):
                code, stderr = _validate(argv)
                self.assertEqual(code, 2,
                                 f"expected a usage error for {argv!r}")
                for needle in needles:
                    self.assertIn(needle, stderr)

    def test_accepted_combinations(self):
        for argv in ACCEPTED:
            with self.subTest(argv=" ".join(argv)):
                code, stderr = _validate(argv)
                self.assertIsNone(
                    code, f"unexpected rejection of {argv!r}: {stderr}")

    def test_no_directory_is_a_usage_error(self):
        # At least one directory is required; there is no cwd default.
        code, _ = _validate(["--sum"])
        self.assertEqual(code, 2)

    def test_version_prints_and_exits_zero(self):
        parser = cli.build_parser()
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            with self.assertRaises(SystemExit) as ctx:
                parser.parse_args(["--version"])
        self.assertEqual(ctx.exception.code, 0)
        from sumtag import __version__
        self.assertEqual(out.getvalue().strip(), f"sumtag {__version__}")


class ProgressQuietOrderTests(unittest.TestCase):
    """--progress vs -q is resolved by command-line order, later wins
    outright, with a warning to stderr (CLAUDE.md "Flags"); a bundled short
    cluster containing q counts as a -q occurrence at its position.
    """

    def _resolve(self, raw: list[str]):
        parser = cli.build_parser()
        args = parser.parse_args(raw)
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            cli._resolve_progress_quiet(raw, args)
        return args, err.getvalue()

    def test_quiet_later_wins(self):
        args, warning = self._resolve(["--sum", "--progress", "-q", "/d"])
        self.assertFalse(args.progress)
        self.assertEqual(args.quiet, 1)
        self.assertIn("-q overrides --progress", warning)

    def test_progress_later_wins(self):
        args, warning = self._resolve(["--sum", "-q", "--progress", "/d"])
        self.assertTrue(args.progress)
        self.assertEqual(args.quiet, 0)     # the loser is discarded outright
        self.assertIn("--progress overrides -q", warning)

    def test_bundled_cluster_counts_as_quiet(self):
        # -fq is a -q occurrence at its position.
        args, warning = self._resolve(["--sum", "--progress", "-fq", "/d"])
        self.assertFalse(args.progress)
        self.assertIn("-q overrides --progress", warning)

    def test_qq_still_loses_to_a_later_progress(self):
        args, _ = self._resolve(["--sum", "-qq", "--progress", "/d"])
        self.assertTrue(args.progress)
        self.assertEqual(args.quiet, 0)

    def test_no_conflict_no_warning(self):
        for raw in (["--sum", "--progress", "/d"], ["--sum", "-q", "/d"],
                    ["--sum", "/d"]):
            with self.subTest(raw=raw):
                args, warning = self._resolve(raw)
                self.assertEqual(warning, "")


if __name__ == "__main__":
    unittest.main()
