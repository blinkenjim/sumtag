"""Unit tests for the re-hash decision (sumtag.decide.should_rehash).

Independent-oracle tests: every expected verdict below is hand-derived from
CLAUDE.md "Re-hashing logic" (the prescriptive design intent) and first
principles -- never transcribed from the function body -- so these tests can
catch a bug in the code rather than enshrine one.

The authority, quoted from CLAUDE.md:

    A file is (re-)hashed when any of the following are true:
      - The --force flag was given.
      - The user.sumtag xattr is absent or unreadable.
      - The file_mtime in the xattr is older than the file's current mtime.
      - The version in the xattr has a lower major version number than the
        current software.

    Freshness is algorithm-agnostic: a file with *any* current digest --
    regardless of which algorithm produced it -- counts as up to date.

Contract notes: ``meta`` is either None (absent/unreadable/malformed xattr --
the caller collapses all three to None) or a schema-valid document.  The
return is ``(bool, reason)``; the booleans are the contract.  Exact reason
strings are cosmetic -v output and are pinned separately as characterization
(see ReasonStringCharacterization), except "file modified since last hash",
which CLAUDE.md itself documents in the --prescan example line.
"""

from __future__ import annotations

import unittest

from sumtag import decide
from sumtag.decide import classify_verify, should_rehash

# Hand-written ISO 8601 UTC microsecond stamps (the xattr timestamp format).
T0 = "2026-07-01T12:00:00.000000Z"           # a reference instant
T0_PLUS_1US = "2026-07-01T12:00:00.000001Z"  # one microsecond later
T_LATER_DAY = "2026-07-02T00:00:00.000000Z"
T_LATER_YEAR = "2027-01-01T00:00:00.000000Z"

CURRENT_MAJOR = 0  # sumtag's own major today (version 0.1.0)


def make_meta(version: str = "0.1.0",
              digests: dict | None = None,
              file_mtime: str = T0) -> dict:
    """A schema-shaped metadata document with hand-chosen fields."""
    return {
        "version": version,
        "digests": {"xxh3": "0123456789abcdef"} if digests is None else digests,
        "file_mtime": file_mtime,
        "hashed_at": T0,
        "run_started_at": T0,
    }


class ShouldRehashTruthTable(unittest.TestCase):
    """One test per row of the CLAUDE.md re-hash conditions (booleans only)."""

    def test_force_rehashes_even_an_uptodate_file(self):
        # "--force: Re-hash every file unconditionally, ignoring any existing
        # xattr metadata."  A perfectly fresh file still re-hashes under -f.
        rehash, _ = should_rehash(make_meta(), T0, force=True,
                                  current_major=CURRENT_MAJOR)
        self.assertTrue(rehash)

    def test_force_rehashes_when_metadata_is_absent_too(self):
        # OR-semantics: force and absent metadata both independently say
        # re-hash; together the verdict is still simply re-hash.
        rehash, _ = should_rehash(None, T0, force=True,
                                  current_major=CURRENT_MAJOR)
        self.assertTrue(rehash)

    def test_absent_metadata_rehashes(self):
        # "The user.sumtag xattr is absent or unreadable."  The caller
        # collapses absent/unreadable/malformed to meta=None.
        rehash, _ = should_rehash(None, T0, force=False,
                                  current_major=CURRENT_MAJOR)
        self.assertTrue(rehash)

    def test_older_major_version_rehashes(self):
        # "The version in the xattr has a lower major version number than the
        # current software (semver major bump = re-hash by default)."
        meta = make_meta(version="0.9.9")
        rehash, _ = should_rehash(meta, T0, force=False, current_major=1)
        self.assertTrue(rehash)

    def test_equal_major_does_not_rehash(self):
        # Same major, fresh mtime, digest present: up to date.  A minor/patch
        # difference within one major must NOT trigger (the rule is about the
        # major component only).
        meta = make_meta(version="1.0.0")
        rehash, _ = should_rehash(meta, T0, force=False, current_major=1)
        self.assertFalse(rehash)

    def test_newer_major_in_file_does_not_rehash(self):
        # Only a LOWER stored major triggers.  A file stamped by a newer
        # sumtag than the one now running is not "older metadata".
        meta = make_meta(version="2.0.0")
        rehash, _ = should_rehash(meta, T0, force=False, current_major=1)
        self.assertFalse(rehash)

    def test_stale_mtime_rehashes(self):
        # "The file_mtime in the xattr is older than the file's current
        # mtime."  Stored T0, file now at a later day.
        meta = make_meta(file_mtime=T0)
        rehash, _ = should_rehash(meta, T_LATER_DAY, force=False,
                                  current_major=CURRENT_MAJOR)
        self.assertTrue(rehash)

    def test_uptodate_file_skips(self):
        # Nothing triggers: no force, metadata present, current major,
        # digest present, mtimes equal.  The skip row.
        rehash, _ = should_rehash(make_meta(), T0, force=False,
                                  current_major=CURRENT_MAJOR)
        self.assertFalse(rehash)


class ShouldRehashProperties(unittest.TestCase):
    """Edge cases and invariants, each hand-derived from the design intent."""

    def test_freshness_is_algorithm_agnostic(self):
        # "a file with *any* current digest -- regardless of which algorithm
        # produced it -- counts as up to date.  Switching which algorithm is
        # currently active never by itself triggers a re-hash."
        meta = make_meta(digests={"md5": "d41d8cd98f00b204e9800998ecf8427e"})
        rehash, _ = should_rehash(meta, T0, force=False,
                                  current_major=CURRENT_MAJOR)
        self.assertFalse(rehash)

    def test_empty_digest_map_rehashes(self):
        # The freshness rule's contrapositive: with NO digest at all there is
        # nothing current under any algorithm, so the file cannot count as up
        # to date -- it must be hashed.
        meta = make_meta(digests={})
        rehash, _ = should_rehash(meta, T0, force=False,
                                  current_major=CURRENT_MAJOR)
        self.assertTrue(rehash)

    def test_mtime_equal_is_uptodate(self):
        # The rule says "older than", strictly: equal mtimes mean the stamp
        # still describes this file.  (CLAUDE.md "What it does": "Skips the
        # file if the recorded mtime matches (already up-to-date).")
        meta = make_meta(file_mtime=T0)
        rehash, _ = should_rehash(meta, T0, force=False,
                                  current_major=CURRENT_MAJOR)
        self.assertFalse(rehash)

    def test_mtime_one_microsecond_older_rehashes(self):
        # Timestamps carry microsecond precision; the smallest representable
        # staleness must trigger.
        meta = make_meta(file_mtime=T0)
        rehash, _ = should_rehash(meta, T0_PLUS_1US, force=False,
                                  current_major=CURRENT_MAJOR)
        self.assertTrue(rehash)

    def test_mtime_newer_than_live_is_not_stale(self):
        # The rule is strictly "older than the file's current mtime".  A
        # stored mtime NEWER than the live one (file restored from backup,
        # clock stepped back) is not that case, so it does not re-hash.
        meta = make_meta(file_mtime=T_LATER_DAY)
        rehash, _ = should_rehash(meta, T0, force=False,
                                  current_major=CURRENT_MAJOR)
        self.assertFalse(rehash)

    def test_mtime_comparison_spans_date_boundaries(self):
        # ISO 8601 UTC fixed-width strings order lexicographically as time
        # does; the decision must respect that across day and year rollovers.
        meta = make_meta(file_mtime=T0)
        for later in (T0_PLUS_1US, T_LATER_DAY, T_LATER_YEAR):
            with self.subTest(live=later):
                rehash, _ = should_rehash(meta, later, force=False,
                                          current_major=CURRENT_MAJOR)
                self.assertTrue(rehash)

    def test_reason_is_always_a_nonempty_string(self):
        # Interface property: every verdict carries a human reason (they feed
        # the -v announcement "hash <path> (<reason>)" / "skip <path> (<reason>)").
        cases = [
            (make_meta(), T0, True),
            (None, T0, False),
            (make_meta(), T0, False),
            (make_meta(file_mtime=T0), T_LATER_DAY, False),
        ]
        for meta, live, force in cases:
            with self.subTest(meta=bool(meta), force=force):
                _, reason = should_rehash(meta, live, force=force,
                                          current_major=CURRENT_MAJOR)
                self.assertIsInstance(reason, str)
                self.assertTrue(reason)

    def test_stale_reason_is_the_documented_wording(self):
        # PRESCRIPTIVE reason: CLAUDE.md's --prescan example shows the -v
        # line "hash /backup/vault/photo0042.dng (file modified since last
        # hash)", documenting this reason's exact wording.
        _, reason = should_rehash(make_meta(file_mtime=T0), T_LATER_DAY,
                                  force=False, current_major=CURRENT_MAJOR)
        self.assertEqual(reason, "file modified since last hash")


class ReasonStringCharacterization(unittest.TestCase):
    """CHARACTERIZATION ONLY: pins the current reason wordings for -v output
    stability.  These strings are not independently documented (unlike the
    stale-mtime reason above), so agreement here proves consistency, not
    correctness -- a deliberate rewording is fine if -v consumers agree.
    """

    def test_current_reason_wordings(self):
        cases = [
            ((make_meta(), T0, True), "forced"),
            ((None, T0, False), "no usable metadata"),
            ((make_meta(version="0.0.1"), T0, False), "older major version"),
            ((make_meta(digests={}), T0, False), "no digest present"),
            ((make_meta(), T0, False), "up-to-date"),
        ]
        for (meta, live, force), expected in cases:
            with self.subTest(expected=expected):
                _, reason = should_rehash(meta, live, force=force,
                                          current_major=1 if expected == "older major version" else CURRENT_MAJOR)
                self.assertEqual(reason, expected)


class ClassifyVerifyTruthTable(unittest.TestCase):
    """The CLAUDE.md "Verification" truth table, one test per row.

    Independent authority, quoted:

        | stored mtime vs live mtime | digest   | meaning                    |
        | same                       | match    | verified intact            |
        | same                       | mismatch | SILENT CORRUPTION (alarm)  |
        | changed                    | mismatch | legitimately modified      |
        | changed                    | match    | touched but identical -- fine |

        A file with no usable xattr is reported as unverifiable (distinct
        from a mismatch -- there is nothing to verify against), never as
        corruption.

    ``computed`` maps each algorithm present in the xattr to the freshly
    recomputed digest of the live bytes.
    """

    STORED = "0123456789abcdef"
    OTHER = "fedcba9876543210"   # a differing digest: the bytes changed

    def test_same_mtime_matching_digest_is_intact(self):
        meta = make_meta(digests={"xxh3": self.STORED}, file_mtime=T0)
        outcome = classify_verify(meta, T0, {"xxh3": self.STORED})
        self.assertEqual(outcome, decide.INTACT)

    def test_same_mtime_mismatch_is_corruption(self):
        # THE alarm case (intent #1): contents changed while mtime did not.
        meta = make_meta(digests={"xxh3": self.STORED}, file_mtime=T0)
        outcome = classify_verify(meta, T0, {"xxh3": self.OTHER})
        self.assertEqual(outcome, decide.CORRUPTION)

    def test_changed_mtime_mismatch_is_stale_not_corruption(self):
        # "file was legitimately modified; the stamp is merely stale
        # (restamp needed) -- not corruption"
        meta = make_meta(digests={"xxh3": self.STORED}, file_mtime=T0)
        outcome = classify_verify(meta, T_LATER_DAY, {"xxh3": self.OTHER})
        self.assertEqual(outcome, decide.STALE)

    def test_changed_mtime_matching_digest_is_intact(self):
        # "touched but content identical -- fine"
        meta = make_meta(digests={"xxh3": self.STORED}, file_mtime=T0)
        outcome = classify_verify(meta, T_LATER_DAY, {"xxh3": self.STORED})
        self.assertEqual(outcome, decide.INTACT)

    def test_no_metadata_is_unverifiable(self):
        self.assertEqual(classify_verify(None, T0, {}), decide.UNVERIFIABLE)

    def test_empty_digest_map_is_unverifiable(self):
        # A document with no digest has nothing to verify against -- same
        # bucket as no document at all, never corruption.
        meta = make_meta(digests={}, file_mtime=T0)
        self.assertEqual(classify_verify(meta, T0, {}), decide.UNVERIFIABLE)


class ClassifyVerifyProperties(unittest.TestCase):
    """Invariants beyond the four table rows, hand-derived."""

    STORED = ClassifyVerifyTruthTable.STORED
    OTHER = ClassifyVerifyTruthTable.OTHER

    def test_mtime_gate_is_equality_not_ordering(self):
        # The table's rows are "same" vs "changed": ANY difference is
        # "changed", including a live mtime EARLIER than the stored one
        # (clock stepped back, file restored).  A mismatch there is still
        # stale, not corruption -- the mtime did leave a trace.
        meta = make_meta(digests={"xxh3": self.STORED}, file_mtime=T_LATER_DAY)
        outcome = classify_verify(meta, T0, {"xxh3": self.OTHER})
        self.assertEqual(outcome, decide.STALE)

    def test_multiple_digests_all_matching_is_intact(self):
        # "every algorithm present in the digests map is recomputed and
        # compared; generic iteration, no special-casing."
        meta = make_meta(digests={"xxh3": self.STORED,
                                  "md5": "d41d8cd98f00b204e9800998ecf8427e"},
                         file_mtime=T0)
        computed = {"xxh3": self.STORED,
                    "md5": "d41d8cd98f00b204e9800998ecf8427e"}
        self.assertEqual(classify_verify(meta, T0, computed), decide.INTACT)

    def test_any_single_mismatch_defeats_intact(self):
        # Intact requires EVERY stored digest to match: one disagreeing
        # algorithm means the bytes cannot equal what was stamped.  With an
        # unchanged mtime that is the corruption row.
        meta = make_meta(digests={"xxh3": self.STORED,
                                  "md5": "d41d8cd98f00b204e9800998ecf8427e"},
                         file_mtime=T0)
        computed = {"xxh3": self.OTHER,
                    "md5": "d41d8cd98f00b204e9800998ecf8427e"}
        self.assertEqual(classify_verify(meta, T0, computed), decide.CORRUPTION)

    def test_any_single_mismatch_with_changed_mtime_is_stale(self):
        meta = make_meta(digests={"xxh3": self.STORED,
                                  "md5": "d41d8cd98f00b204e9800998ecf8427e"},
                         file_mtime=T0)
        computed = {"xxh3": self.OTHER,
                    "md5": "d41d8cd98f00b204e9800998ecf8427e"}
        self.assertEqual(classify_verify(meta, T_LATER_DAY, computed),
                         decide.STALE)

    def test_outcomes_are_four_distinct_values(self):
        # The engine dispatches on these; they must be pairwise distinct.
        outcomes = {decide.INTACT, decide.CORRUPTION, decide.STALE,
                    decide.UNVERIFIABLE}
        self.assertEqual(len(outcomes), 4)


class MatchMovedDirsTests(unittest.TestCase):
    """The moved-directory matcher (TODO.md design, decided 2026-08-05).

    Pure function over plain data: ``lost`` maps a lost directory's key to
    its recorded residents {basename: (inode, file_mtime)}; ``found`` maps a
    candidate directory's key to its live children in the same shape.
    Returns (matches, ambiguous): L matches F iff EVERY recorded resident of
    L has a child of F agreeing on all three of (basename via the dict key,
    inode, mtime) -- the subset direction tolerates files ADDED after the
    move, and anything less than full agreement falls back to
    prune-plus-rescan, the safe direction.  A lost dir matching several
    candidates, or several lost dirs claiming one candidate, is ambiguous:
    matched nowhere, reported for exit 2.
    """

    T0 = "2026-08-01T12:00:00.000000Z"
    T1 = "2026-08-01T12:00:01.000000Z"

    def rows(self):
        return {"f1": (100, self.T0), "f2": (101, self.T1)}

    def test_exact_match(self):
        matches, ambiguous = decide.match_moved_dirs(
            {"old": self.rows()}, {"new": self.rows()})
        self.assertEqual(matches, {"old": "new"})
        self.assertEqual(ambiguous, set())

    def test_subset_tolerates_files_added_after_the_move(self):
        found = dict(self.rows(), extra=(200, self.T0))
        matches, _ = decide.match_moved_dirs({"old": self.rows()},
                                             {"new": found})
        self.assertEqual(matches, {"old": "new"})

    def test_missing_child_breaks_the_match(self):
        # A file deleted after the move: not the same directory anymore --
        # falls back to prune + rescan.
        found = {"f1": (100, self.T0)}   # f2 gone
        matches, ambiguous = decide.match_moved_dirs({"old": self.rows()},
                                                     {"new": found})
        self.assertEqual(matches, {})
        self.assertEqual(ambiguous, set())

    def test_inode_disagreement_breaks_the_match(self):
        found = {"f1": (100, self.T0), "f2": (999, self.T1)}
        matches, _ = decide.match_moved_dirs({"old": self.rows()},
                                             {"new": found})
        self.assertEqual(matches, {})

    def test_mtime_disagreement_breaks_the_match(self):
        # Inode reuse insurance: the recycled inode won't carry the exact
        # recorded microsecond mtime.
        found = {"f1": (100, self.T0), "f2": (101, self.T0)}  # f2 mtime off
        matches, _ = decide.match_moved_dirs({"old": self.rows()},
                                             {"new": found})
        self.assertEqual(matches, {})

    def test_renamed_child_breaks_the_match(self):
        found = {"f1": (100, self.T0), "renamed": (101, self.T1)}
        matches, _ = decide.match_moved_dirs({"old": self.rows()},
                                             {"new": found})
        self.assertEqual(matches, {})

    def test_lost_dir_matching_two_candidates_is_ambiguous(self):
        # Hard-link farm: two candidate dirs carry identical (inode, name,
        # mtime) children. No guess -- ambiguous, matched nowhere.
        matches, ambiguous = decide.match_moved_dirs(
            {"old": self.rows()},
            {"c1": self.rows(), "c2": self.rows()})
        self.assertEqual(matches, {})
        self.assertEqual(ambiguous, {"old"})

    def test_two_lost_dirs_claiming_one_candidate_are_ambiguous(self):
        matches, ambiguous = decide.match_moved_dirs(
            {"oldA": self.rows(), "oldB": self.rows()},
            {"new": self.rows()})
        self.assertEqual(matches, {})
        self.assertEqual(ambiguous, {"oldA", "oldB"})

    def test_independent_moves_all_resolve(self):
        lost = {"a": {"f": (1, self.T0)}, "b": {"g": (2, self.T0)}}
        found = {"x": {"f": (1, self.T0)}, "y": {"g": (2, self.T0)},
                 "unrelated": {"z": (9, self.T1)}}
        matches, ambiguous = decide.match_moved_dirs(lost, found)
        self.assertEqual(matches, {"a": "x", "b": "y"})
        self.assertEqual(ambiguous, set())

    def test_empty_residents_never_match(self):
        # A rowless lost dir would match everything vacuously; refuse.
        matches, ambiguous = decide.match_moved_dirs(
            {"old": {}}, {"new": self.rows()})
        self.assertEqual(matches, {})
        self.assertEqual(ambiguous, set())

    def test_no_candidates_no_matches(self):
        matches, ambiguous = decide.match_moved_dirs({"old": self.rows()}, {})
        self.assertEqual(matches, {})
        self.assertEqual(ambiguous, set())


if __name__ == "__main__":
    unittest.main()
