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


if __name__ == "__main__":
    unittest.main()
