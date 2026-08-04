"""Unit tests for grouper's pure comparison layer (sumtag.grouper).

Independent-oracle tests: signatures, scorers, and the candidate-nomination
index, with every expected value computed by hand from first principles and
the documented design (CLAUDE.md "Experimental companion programs" /
grouper's own contract statements).  Anchors used:

- "digest -- content only, renames free; name-digest -- all-or-nothing per
  (name, digest); name-score -- name-anchored partial credit: 1 point for a
  shared basename, +2 if the digests also agree."
- 1.0 means identical contents (which fixes the default denominator: two
  identical directories of n files score 3n points, so the divisor must be
  3*max(|A|,|B|)).
- matched = "the files in common under that function's own notion of
  matching (shared basenames for name-score, the multiset intersection for
  the Jaccard functions)".
- The nomination invariant: score > 0 REQUIRES a shared signature key --
  what makes --max-df candidate nomination lossless.

Rows are plain dicts standing in for sqlite3.Row (both support [] access);
the file-list order never matters (a signature describes a set of files).
"""

from __future__ import annotations

import pickle
import unittest
from collections import Counter

from sumtag.grouper import (
    COMPARISONS,
    _candidate_partners,
    _kept_index,
    _multiset_jaccard,
    make_name_score,
    max_identical,
    max_matched,
    max_smaller,
    sig_digest,
    sig_name_digest,
    sig_names,
)


def row(rel_path: str, digest: str, algo: str = "xxh3") -> dict:
    return {"rel_path": rel_path, "algo": algo, "digest": digest}


class SignatureTests(unittest.TestCase):
    FILES = [row("proj/a.txt", "d1"), row("proj/b.txt", "d2"),
             row("proj/copy.txt", "d1")]

    def test_sig_digest_is_a_content_multiset(self):
        # Content only: names never appear; repetition counts (two files
        # with digest d1 are two entries, not one).
        self.assertEqual(sig_digest(self.FILES),
                         Counter({("xxh3", "d1"): 2, ("xxh3", "d2"): 1}))

    def test_sig_name_digest_pairs_name_with_content(self):
        self.assertEqual(
            sig_name_digest(self.FILES),
            Counter({("a.txt", "xxh3", "d1"): 1,
                     ("b.txt", "xxh3", "d2"): 1,
                     ("copy.txt", "xxh3", "d1"): 1}))

    def test_sig_names_maps_basename_to_content(self):
        # Basenames are unique within one directory, so a plain dict holds.
        self.assertEqual(
            sig_names(self.FILES),
            {"a.txt": ("xxh3", "d1"), "b.txt": ("xxh3", "d2"),
             "copy.txt": ("xxh3", "d1")})

    def test_signatures_use_basenames_not_paths(self):
        # The directory prefix is the dir's identity, not the file's: the
        # same listing under two different parents signs identically.
        a = [row("x/f.txt", "d1")]
        b = [row("deeply/nested/y/f.txt", "d1")]
        self.assertEqual(sig_name_digest(a), sig_name_digest(b))
        self.assertEqual(sig_names(a), sig_names(b))

    def test_signatures_are_plain_picklable_values(self):
        # The documented worker contract: signatures must be plain picklable
        # values (dicts/Counters of str tuples), since --jobs rebuilds them
        # in spawned processes.
        for name, (make_sig, _) in COMPARISONS.items():
            with self.subTest(fn=name):
                sig = make_sig(self.FILES)
                self.assertEqual(pickle.loads(pickle.dumps(sig)), sig)

    def test_empty_file_list_signs_empty(self):
        self.assertEqual(sig_digest([]), Counter())
        self.assertEqual(sig_names([]), {})


class MultisetJaccardTests(unittest.TestCase):
    """|A & B| / |A | B| on multisets; matched = the intersection size."""

    def test_hand_computed_overlap(self):
        # A = {x, y}, B = {x, z}: intersection {x} (1), union {x,y,z} (3).
        a, b = Counter(["x", "y"]), Counter(["x", "z"])
        self.assertEqual(_multiset_jaccard(a, b), (1 / 3, 1))

    def test_multiset_counting(self):
        # Two copies vs one: intersection min(2,1)=1, union max(2,1)=2.
        self.assertEqual(_multiset_jaccard(Counter({"x": 2}),
                                           Counter({"x": 1})), (0.5, 1))

    def test_identical_scores_one(self):
        a = Counter({"x": 2, "y": 1})
        self.assertEqual(_multiset_jaccard(a, Counter(a)), (1.0, 3))

    def test_disjoint_scores_zero(self):
        self.assertEqual(_multiset_jaccard(Counter(["x"]), Counter(["y"])),
                         (0.0, 0))

    def test_empty_vs_empty_is_identical(self):
        # "two empty directories have identical (empty) contents"
        self.assertEqual(_multiset_jaccard(Counter(), Counter()), (1.0, 0))

    def test_empty_vs_nonempty_is_zero(self):
        self.assertEqual(_multiset_jaccard(Counter(), Counter(["x"])),
                         (0.0, 0))

    def test_symmetry(self):
        a, b = Counter({"x": 2, "y": 1}), Counter({"x": 1, "z": 3})
        self.assertEqual(_multiset_jaccard(a, b), _multiset_jaccard(b, a))


class MaxScoreDenominatorTests(unittest.TestCase):
    """The three pluggable "max possible" ingredients, from their anchors:
    identical keeps 1.0 == identical (3*max), smaller lets a subset reach
    1.0 (3*min), matched only counts names that matched (3*n_matched).
    """

    def test_hand_values(self):
        self.assertEqual(max_identical(2, 5, 2), 15)   # 3 * max
        self.assertEqual(max_smaller(2, 5, 2), 6)      # 3 * min
        self.assertEqual(max_matched(2, 5, 2), 6)      # 3 * matched
        self.assertEqual(max_identical(0, 0, 0), 0)


class NameScoreTests(unittest.TestCase):
    """The default scorer: 1 point per shared basename, +2 when the
    (algo, digest) also agrees; similarity = points / max_fn(...);
    matched = shared-basename count.
    """

    def setUp(self):
        self.score = make_name_score(max_identical)

    def test_hand_computed_partial_credit(self):
        # A: a.txt=d1, b.txt=d2.  B: a.txt=d1, b.txt=d3, c.txt=d4.
        # Shared names {a,b}: 2 points; a's digests agree: +2 -> 4 points.
        # Denominator: 3 * max(2, 3) = 9.
        a = {"a.txt": ("xxh3", "d1"), "b.txt": ("xxh3", "d2")}
        b = {"a.txt": ("xxh3", "d1"), "b.txt": ("xxh3", "d3"),
             "c.txt": ("xxh3", "d4")}
        self.assertEqual(self.score(a, b), (4 / 9, 2))

    def test_identical_directories_score_one(self):
        a = {"a.txt": ("xxh3", "d1"), "b.txt": ("xxh3", "d2")}
        self.assertEqual(self.score(a, dict(a)), (1.0, 2))

    def test_same_names_all_content_drifted(self):
        # Names all shared, no digest agrees: n points / 3n = 1/3 exactly.
        a = {"a.txt": ("xxh3", "d1"), "b.txt": ("xxh3", "d2")}
        b = {"a.txt": ("xxh3", "dX"), "b.txt": ("xxh3", "dY")}
        self.assertEqual(self.score(a, b), (1 / 3, 2))

    def test_renamed_identical_file_scores_zero(self):
        # "A renamed byte-identical file scores 0 here (cmp_digest is the
        # function that still sees it)."
        a = {"old.txt": ("xxh3", "d1")}
        b = {"new.txt": ("xxh3", "d1")}
        self.assertEqual(self.score(a, b), (0.0, 0))

    def test_different_algorithms_earn_the_name_point_only(self):
        # Digests only compare within one algorithm (the mixed-algorithm
        # hazard): same hex under different algos is incomparable, so the
        # pair gets 1 name point, never the +2 -- we never guess about
        # content.  1 point / 3*max(1,1) = 1/3.
        a = {"f.txt": ("xxh3", "d1")}
        b = {"f.txt": ("md5", "d1")}
        self.assertEqual(self.score(a, b), (1 / 3, 1))

    def test_empty_vs_empty_is_identical(self):
        self.assertEqual(self.score({}, {}), (1.0, 0))

    def test_empty_vs_nonempty_is_zero(self):
        self.assertEqual(self.score({}, {"f": ("xxh3", "d1")}), (0.0, 0))

    def test_subset_under_the_three_denominators(self):
        # One perfect pair, dir A wholly inside 100-file dir B: 3 points.
        a = {"f0.txt": ("xxh3", "d0")}
        b = {f"f{i}.txt": ("xxh3", f"d{i}") for i in range(100)}
        # identical: 3/300 -- the documented "~0.01, not 1.0".
        self.assertEqual(make_name_score(max_identical)(a, b), (0.01, 1))
        # smaller: 3/3 -- "Subset scores 1.0".
        self.assertEqual(make_name_score(max_smaller)(a, b), (1.0, 1))
        # matched: 3/3 -- max given the names that matched.
        self.assertEqual(make_name_score(max_matched)(a, b), (1.0, 1))


class NominationInvariantTests(unittest.TestCase):
    """score(a, b) > 0 REQUIRES a shared signature key -- every registered
    comparison function must satisfy this, or --max-df candidate nomination
    (whose inverted index is built over signature keys) silently loses
    pairs.  Verified for the whole registry over key-disjoint inputs.
    """

    def test_key_disjoint_signatures_score_zero(self):
        files_a = [row("d/x.txt", "d1"), row("d/y.txt", "d2")]
        files_b = [row("e/p.txt", "d3"), row("e/q.txt", "d4")]
        for name, (make_sig, score) in COMPARISONS.items():
            with self.subTest(fn=name):
                sig_a, sig_b = make_sig(files_a), make_sig(files_b)
                self.assertFalse(set(sig_a) & set(sig_b),
                                 "test premise: keys must be disjoint")
                similarity, matched = score(sig_a, sig_b)
                self.assertEqual(similarity, 0.0)
                self.assertEqual(matched, 0)


class KeptIndexTests(unittest.TestCase):
    """The nomination index: signature key -> sorted [dir_id...], keeping
    only keys present in 2..max_df directories (singletons nominate
    nothing; past the cap is the ubiquitous-noise case the cap exists to
    exclude).
    """

    SIGS = {
        1: Counter({"a": 1}),
        2: Counter({"a": 1, "b": 1}),
        3: Counter({"b": 2, "c": 1}),   # multiplicity is irrelevant to keys
    }

    def test_hand_built_index(self):
        # 'a' in dirs {1,2}, 'b' in {2,3}, 'c' only in {3} (singleton).
        self.assertEqual(_kept_index(self.SIGS, max_df=2),
                         {"a": [1, 2], "b": [2, 3]})

    def test_cap_drops_ubiquitous_keys(self):
        sigs = dict(self.SIGS)
        sigs[4] = Counter({"a": 1})     # 'a' now in 3 dirs
        self.assertEqual(_kept_index(sigs, max_df=2), {"b": [2, 3]})

    def test_cap_of_one_keeps_nothing(self):
        # Every key useful for pairing is by definition in >= 2 dirs.
        self.assertEqual(_kept_index(self.SIGS, max_df=1), {})

    def test_ids_come_back_sorted(self):
        sigs = {9: Counter({"t": 1}), 3: Counter({"t": 1}),
                7: Counter({"t": 1})}
        self.assertEqual(_kept_index(sigs, max_df=10), {"t": [3, 7, 9]})


class CandidatePartnersTests(unittest.TestCase):
    """Later dirs sharing at least one kept key with d -- deduplicated,
    sorted, strictly d < partner so each unordered pair is nominated by
    exactly one outer directory (mirroring the triangular loop).
    """

    SIGS = KeptIndexTests.SIGS
    INDEX = {"a": [1, 2], "b": [2, 3]}

    def test_only_later_dirs_nominate(self):
        self.assertEqual(_candidate_partners(self.INDEX, self.SIGS[1], 1),
                         [2])
        self.assertEqual(_candidate_partners(self.INDEX, self.SIGS[2], 2),
                         [3])
        self.assertEqual(_candidate_partners(self.INDEX, self.SIGS[3], 3),
                         [])

    def test_multiple_shared_keys_nominate_once(self):
        # A pair sharing several rare keys is still one candidate.
        index = {"a": [1, 2], "b": [1, 2]}
        sigs = {1: Counter({"a": 1, "b": 1}), 2: Counter({"a": 1, "b": 1})}
        self.assertEqual(_candidate_partners(index, sigs[1], 1), [2])

    def test_dropped_keys_nominate_nothing(self):
        # 'c' was not kept (singleton), so it can produce no partners.
        self.assertEqual(_candidate_partners(self.INDEX,
                                             Counter({"c": 1}), 1), [])


# --- Pipeline flows (test-only phase; no body rewrites) ---------------------

import contextlib  # noqa: E402
import io          # noqa: E402
import os as _os   # noqa: E402
import sqlite3     # noqa: E402
import tempfile    # noqa: E402

from sumtag import grouper  # noqa: E402
from sumtag.store import _SCHEMA_SQL  # noqa: E402

ROW_DEFAULTS = ("2026-08-01T00:00:00.000000Z", "2026-08-01T00:00:01.000000Z",
                "2026-08-01T00:00:00.000000Z", "0.1.0")


def run_grouper(argv: list[str]) -> tuple[int, str, str]:
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        try:
            code = grouper.main(argv)
        except SystemExit as e:  # connect() sys.exits on a missing database
            code = e.code
    return code, out.getvalue(), err.getvalue()


class PipelineFixture(unittest.TestCase):
    """A seeded sumtag database (written directly with sqlite3 -- grouper
    only ever READS sumtag's tables, so independent seeding is the honest
    fixture).  The corpus, one mountpoint /mnt/t:

      projA/          f1=d1 f2=d2 f3=d3     } same basename, 2 of 3 digests
      backup/projA/   f1=d1 f2=d2 f3=dX     }  agree: name-score 7/9 = 0.778
      big/x/ big/y/   g1..g3 identical      } sim 1.0, matched 3
      pair2a/ pair2b/ n=dS  identical       } sim 1.0, matched 1
      lone/           two unique files        pairs with nothing
      .git/hooks/     junk: hidden path component
      dots/           junk: all files hidden
      keep-mixed/     one visible + one hidden file (kept whole)
    """

    FILES = [
        ("projA/f1", "d1"), ("projA/f2", "d2"), ("projA/f3", "d3"),
        ("backup/projA/f1", "d1"), ("backup/projA/f2", "d2"),
        ("backup/projA/f3", "dX"),
        ("big/x/g1", "e1"), ("big/x/g2", "e2"), ("big/x/g3", "e3"),
        ("big/y/g1", "e1"), ("big/y/g2", "e2"), ("big/y/g3", "e3"),
        ("pair2a/n", "dS"), ("pair2b/n", "dS"),
        ("lone/u1", "u1"), ("lone/u2", "u2"),
        (".git/hooks/pre-commit.sample", "h1"),
        ("dots/.hidden1", "h2"), ("dots/.hidden2", "h3"),
        ("keep-mixed/visible.txt", "k1"), ("keep-mixed/.DS_Store", "k2"),
    ]

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.db = _os.path.join(self._tmp.name, "g.sqlite")
        conn = sqlite3.connect(self.db)
        conn.executescript(_SCHEMA_SQL)
        conn.execute("INSERT INTO mountpoints(path) VALUES ('/mnt/t')")
        for i, (rel, digest) in enumerate(self.FILES):
            conn.execute(
                "INSERT INTO files (mountpoint_id, rel_path, inode, algo,"
                " digest, file_mtime, hashed_at, run_started_at, version)"
                " VALUES (1, ?, ?, 'xxh3', ?, ?, ?, ?, ?)",
                (rel, 1000 + i, digest, *ROW_DEFAULTS))
        conn.commit()
        conn.close()

    def _table(self, sql: str) -> list[tuple]:
        conn = sqlite3.connect(self.db)
        try:
            return conn.execute(sql).fetchall()
        finally:
            conn.close()


class IndexStageTests(PipelineFixture):
    def test_index_drops_junk_with_stderr_counts(self):
        code, _, err = run_grouper(["--database", self.db, "--index"])
        self.assertEqual(code, 0)
        self.assertIn("junk directorie", err)          # .git/hooks
        self.assertIn("no visible files", err)         # dots/
        dirs = {r[0] for r in self._table("SELECT rel_path FROM dirs")}
        self.assertNotIn(".git/hooks", dirs)
        self.assertNotIn("dots", dirs)
        self.assertIn("keep-mixed", dirs)              # kept: has a visible file
        # A kept directory keeps ALL its files, hidden included.
        n = self._table(
            "SELECT COUNT(*) FROM dir_files df JOIN dirs d ON d.id=df.dir_id"
            " WHERE d.rel_path='keep-mixed'")[0][0]
        self.assertEqual(n, 2)

    def test_no_junk_filter_keeps_everything(self):
        code, _, err = run_grouper(["--database", self.db, "--index",
                                    "--no-junk-filter"])
        self.assertEqual(code, 0)
        self.assertNotIn("dropped", err)
        dirs = {r[0] for r in self._table("SELECT rel_path FROM dirs")}
        self.assertIn(".git/hooks", dirs)
        self.assertIn("dots", dirs)

    def test_missing_database_is_a_hard_error(self):
        code, _, err = run_grouper(["--database", "/no/such.sqlite",
                                    "--index"])
        self.assertNotEqual(code, 0)

    def test_pairs_without_index_names_the_missing_stage(self):
        code, _, err = run_grouper(["--database", self.db, "--pairs"])
        self.assertEqual(code, 1)
        self.assertIn("run --index first", err)


class GroupingFlowTests(PipelineFixture):
    def _prep(self, *extra: str) -> None:
        code, _, _ = run_grouper(["--database", self.db, "--prep", *extra])
        assert code == 0, "prep failed"

    def test_threshold_groups_and_reports(self):
        self._prep()
        code, out, _ = run_grouper(["--database", self.db,
                                    "--threshold", "0.7"])
        self.assertEqual(code, 0)
        self.assertIn("grouping: fn=name-score  threshold=0.7", out)
        # Three groups: the two 1.0 pairs and the 0.778 projA pair; lone/,
        # keep-mixed/ pair with nothing and appear in no group.
        self.assertIn("3 group(s), 6 directorie(s)", out)
        self.assertNotIn("lone", out)

    def test_matched_tiebreak_seeds_big_overlaps_first(self):
        # big/x-big/y and pair2a-pair2b both score 1.0; the 3-file overlap
        # (matched 3) must walk first and take group id 1.
        self._prep()
        run_grouper(["--database", self.db, "--threshold", "0.9"])
        rows = self._table(
            "SELECT gd.group_id, d.rel_path FROM group_dirs gd"
            " JOIN dirs d ON d.id=gd.dir_id ORDER BY gd.group_id, d.rel_path")
        by_group: dict[int, list[str]] = {}
        for gid, rel in rows:
            by_group.setdefault(gid, []).append(rel)
        self.assertEqual(by_group[min(by_group)], ["big/x", "big/y"])

    def test_report_shows_best_pair_strength(self):
        self._prep()
        _, out, _ = run_grouper(["--database", self.db, "--threshold", "0.7"])
        self.assertIn("best pair 1.000", out)
        self.assertIn("best pair 0.778", out)

    def test_current_grouping_is_not_rebuilt(self):
        self._prep()
        run_grouper(["--database", self.db, "--threshold", "0.7"])
        before = self._table(
            "SELECT built_at FROM grouper_meta WHERE artifact='groups'")
        run_grouper(["--database", self.db, "--threshold", "0.7"])
        after = self._table(
            "SELECT built_at FROM grouper_meta WHERE artifact='groups'")
        self.assertEqual(before, after)    # skipped: provenance untouched

    def test_bare_report_without_grouping_hints_the_pipeline(self):
        self._prep()
        code, _, err = run_grouper(["--database", self.db])
        self.assertEqual(code, 1)
        self.assertIn("no stored grouping", err)

    def test_threshold_without_pairs_is_refused(self):
        run_grouper(["--database", self.db, "--index"])
        code, _, err = run_grouper(["--database", self.db,
                                    "--threshold", "0.5"])
        self.assertEqual(code, 1)
        self.assertIn("no stored pairs", err)

    def test_fn_mismatch_is_refused_with_a_rerun_hint(self):
        self._prep()                        # default fn: name-score
        code, _, err = run_grouper(["--database", self.db,
                                    "--threshold", "0.7", "--fn", "digest"])
        self.assertEqual(code, 1)
        self.assertIn("built with --fn name-score", err)
        self.assertIn("rerun --pairs", err)

    def test_min_sim_floor_refuses_a_lower_threshold(self):
        self._prep("--min-sim", "0.9")
        code, _, err = run_grouper(["--database", self.db,
                                    "--threshold", "0.7"])
        self.assertEqual(code, 1)
        self.assertIn("--min-sim 0.9", err)
        # At or above the floor the knob still works.
        code, out, _ = run_grouper(["--database", self.db,
                                    "--threshold", "0.95"])
        self.assertEqual(code, 0)
        self.assertIn("2 group(s)", out)   # only the two 1.0 pairs survive

    def test_name_gate_restricts_pairs_to_same_basenames(self):
        # With --name, projA vs backup/projA (both basename projA) still
        # pair; big/x vs big/y and pair2a vs pair2b (different basenames)
        # score 0 by definition and never group.
        self._prep("--name")
        code, out, _ = run_grouper(["--database", self.db,
                                    "--threshold", "0.7"])
        self.assertEqual(code, 0)
        self.assertIn("1 group(s), 2 directorie(s)", out)
        self.assertIn("projA", out)

    def test_nomination_stores_the_same_pairs_as_exhaustive(self):
        # The lossless-nomination invariant, end to end: a generous cap
        # nominates every pair the exhaustive loop scores.
        self._prep("--max-df", "0")        # forced exhaustive
        exhaustive = set(self._table(
            "SELECT dir_a, dir_b, similarity, matched FROM dir_pairs"))
        code, _, err = run_grouper(["--database", self.db, "--pairs",
                                    "--max-df", "1000"])
        self.assertEqual(code, 0)
        self.assertIn("candidate nomination", err)   # announced, by consent
        nominated = set(self._table(
            "SELECT dir_a, dir_b, similarity, matched FROM dir_pairs"))
        self.assertEqual(nominated, exhaustive)


class ReportSortTests(PipelineFixture):
    def _sized_prep(self) -> None:
        conn = sqlite3.connect(self.db)
        conn.execute("UPDATE files SET size = 10")
        conn.execute("UPDATE files SET size = 1000"
                     " WHERE rel_path LIKE 'pair2%'")
        conn.commit()
        conn.close()
        run_grouper(["--database", self.db, "--prep"])
        run_grouper(["--database", self.db, "--threshold", "0.7"])

    def test_size_sort_refuses_when_no_sizes_stored(self):
        run_grouper(["--database", self.db, "--prep"])
        run_grouper(["--database", self.db, "--threshold", "0.7"])
        code, _, err = run_grouper(["--database", self.db,
                                    "--sort", "size"])
        self.assertEqual(code, 1)
        self.assertIn("sumtag --locate", err)

    def test_size_sort_orders_largest_first_with_weight_headers(self):
        self._sized_prep()
        code, out, _ = run_grouper(["--database", self.db, "--sort", "size"])
        self.assertEqual(code, 0)
        # pair2a+pair2b carry 2000 bytes vs big's 60 and projA's 60: the
        # 2-file group leads despite its low bond-order id.
        first_group = out.split("group ")[1]
        self.assertIn("pair2a", first_group)
        self.assertIn("avg ", out)          # the per-directory average
        self.assertIn("2 files", first_group)

    def test_files_sort_orders_by_stamped_file_count(self):
        # big and projA both total 6 files -- a tie, broken by ascending
        # group id (the documented tiebreak), and big holds the lower id
        # from seeding first at similarity 1.0. The 2-file pair2 group
        # trails both.
        run_grouper(["--database", self.db, "--prep"])
        run_grouper(["--database", self.db, "--threshold", "0.7"])
        code, out, _ = run_grouper(["--database", self.db,
                                    "--sort", "files"])
        self.assertEqual(code, 0)
        groups = out.split("group ")[1:]
        self.assertIn("big/x", groups[0])
        self.assertIn("6 files", groups[0])
        self.assertIn("projA", groups[1])
        self.assertIn("pair2a", groups[2])   # 2 files: last


class InspectionHelperTests(PipelineFixture):
    def setUp(self) -> None:
        super().setUp()
        run_grouper(["--database", self.db, "--index"])

    def test_ls_lists_a_directory_via_the_index(self):
        code, out, _ = run_grouper(["--database", self.db, "--ls", "projA"])
        self.assertEqual(code, 0)
        self.assertIn("(3 file(s))", out)
        self.assertIn("xxh3:d1", out)

    def test_ls_unknown_directory_hints_the_index(self):
        code, _, err = run_grouper(["--database", self.db, "--ls", "ghost"])
        self.assertEqual(code, 1)
        self.assertIn("not in index", err)

    def test_compare_prints_four_decimals(self):
        code, out, _ = run_grouper(["--database", self.db, "--compare",
                                    "projA", "backup/projA"])
        self.assertEqual(code, 0)
        self.assertEqual(out.strip(), "0.7778")

    def test_compare_with_name_gate_zeroes_different_basenames(self):
        code, out, _ = run_grouper(["--database", self.db, "--compare",
                                    "pair2a", "pair2b", "--name"])
        self.assertEqual(code, 0)
        self.assertEqual(out.strip(), "0.0000")

    def test_dupes_groups_by_digest_with_inodes(self):
        code, out, _ = run_grouper(["--database", self.db, "--dupes"])
        self.assertEqual(code, 0)
        self.assertIn("xxh3:d1  x2", out)
        self.assertIn("group(s)", out)


class CleanDbTests(PipelineFixture):
    def test_clean_drops_everything_and_vacuum_reclaims(self):
        run_grouper(["--database", self.db, "--prep"])
        run_grouper(["--database", self.db, "--threshold", "0.7"])
        code, out, _ = run_grouper(["--database", self.db, "--clean-db"])
        self.assertEqual(code, 0)
        self.assertIn("dropped:", out)
        self.assertIn("reclaimed:", out)
        names = {r[0] for r in self._table(
            "SELECT name FROM sqlite_master WHERE type='table'")}
        self.assertFalse(names & {"dirs", "dir_files", "dir_pairs",
                                  "groups", "group_dirs", "grouper_meta"})
        # Sumtag's own tables are never touched.
        self.assertIn("files", names)
        self.assertIn("mountpoints", names)

    def test_second_clean_reports_nothing_to_do(self):
        run_grouper(["--database", self.db, "--prep"])
        run_grouper(["--database", self.db, "--clean-db"])
        code, out, _ = run_grouper(["--database", self.db, "--clean-db"])
        self.assertEqual(code, 0)
        self.assertIn("nothing to clean", out)


if __name__ == "__main__":
    unittest.main()
