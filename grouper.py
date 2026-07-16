#!/usr/bin/env python3
"""grouper.py -- find groups of similar directories in a sumtag database.

Sumtag stamps files with a checksum and mirrors that metadata into an optional
SQLite database (see CLAUDE.md "Database storage"). Grouper is the "intent #2"
playground built on top of it: its purpose is to find groups of directories
whose contents are identical or nearly so -- e.g. two slightly different
versions of the same project from slightly different times. Grouping is the
default act; everything else is a preparation stage or an inspection helper.

Usage (pipeline order):
    grouper.py --database DB --prep [--fn N] [--jobs N] [--progress]
                                              # stages 1+2: build the directory
                                              #   index, then compare every
                                              #   directory to every other and
                                              #   store the pairs
    grouper.py --database DB --index [--progress]          # stage 1 alone
    grouper.py --database DB --pairs [--fn N] [--jobs N] [--progress]
                                     [--max-df N] [--min-sim X]  # stage 2 alone
    grouper.py --database DB --threshold 0.7  # stage 3: build + persist groups
                                              #   (skipped if the stored grouping
                                              #   is already current), then
                                              #   report them
    grouper.py --database DB                  # THE report: show stored grouping

    grouper.py --database DB --ls DIR             # inspect one directory
    grouper.py --database DB --compare A B [--fn N]  # similarity of two dirs
    grouper.py --database DB --dups [--min N]     # duplicate *files* report
    grouper.py --database DB --top [N]            # the N (default 1) most
                                                  #   frequent checksums,
                                                  #   excluding empty files

Stages may be combined in one invocation (--prep --threshold 0.7). --index and
--pairs are silent on success; --threshold ends with the stored-grouping
report, same as the bare invocation. Errors go to stderr with exit 1.

Data model (grouper-owned derived tables inside the sumtag database):

    dirs / dir_files -- the directory index: every files.rel_path distilled to
        the distinct directories that directly contain at least one *visible*
        stamped file (basename not starting with '.'); all-hidden directories
        are junk, dropped at --index so nothing downstream ever sees them.
        dir_files maps dir_id -> files.rowid (indexed) so a directory's
        contents are one keyed lookup. Comparisons are direct-children only,
        deliberately -- a directory's own file listing is its signature.
    dir_pairs -- similarity for every pair of directories (dir_a < dir_b),
        computed by one named comparison function. ALL nonzero pairs are
        stored, not just those above any threshold: the N^2 comparison is the
        expensive part, so the threshold stays a cheap, exploratory knob --
        regrouping at a different threshold recomputes nothing.
    groups / group_dirs -- the persisted grouping. group_dirs.dir_id is the
        PRIMARY KEY, so "a directory belongs to at most one group" (the
        partition rule) is enforced by the schema, not by code discipline.
    grouper_meta -- provenance: which comparison function built the pairs,
        which fn/threshold built the grouping, and when. A stored artifact
        never masquerades as something it isn't.

Grouping algorithm (settled by discussion, 2026-07-02/03):

    walk dir_pairs in (similarity DESC, dir_a, dir_b) order:
        stop at the first pair below the threshold   # the "interestingness"
                                                     # shortcut -- lossless,
                                                     # nothing below it could
                                                     # have grouped anyway
        if both dirs already placed:  skip (no merging, ever)
        if neither placed:            the two found a new group
        if exactly one placed:        the other joins its group

    Each directory gets exactly one placement decision -- the first pair it
    appears in, walking best-first -- and is never reconsidered, which is what
    makes the partition structurally unbreakable. "Similar to a group" means
    similar to any one member (single-linkage; may be refined later). No group
    can ever have a single member. Deterministic for a given database: the
    walk order is total, so identical inputs give identical groups.

Comparison functions:

    A comparison function scores two directories' contents from 0.0 (nothing
    in common) to 1.0 (identical), consistently for the same directories in
    the same database. Each is registered by name in COMPARISONS as a
    (signature, score) pair, swapped with --fn to subtly change how grouping
    behaves. signature(files) distills one directory's file-list into
    whatever the scorer consumes -- built once per directory, because the
    pair loop scores N^2/2 times and rebuilding it per pair dominated the
    whole phase (measured 2026-07-15: ~16x). score(sig_a, sig_b) is the
    pairwise math. Both are pure functions, so determinism comes free.

    --pairs distributes the scoring across --jobs worker processes (default:
    all CPUs). Each worker loads its own signature table straight from the
    database over a read-only connection -- nothing large is ever pickled
    across the spawn pipe -- so every worker holds a full copy in RAM:
    --jobs is a memory knob as much as a speed knob on a big corpus. All
    database writes stay in the parent (SQLite has one writer), committed
    incrementally; an interrupted --pairs keeps its completed inserts but
    deletes the pairs provenance first, so --threshold refuses a partial
    table ("no stored pairs") until a --pairs run completes. The pair set
    produced is identical at any --jobs value.

Candidate nomination (--max-df):

    Exhaustive --pairs scores all N*(N-1)/2 pairs -- infeasible at millions
    of directories (a 4.7M-dir corpus is ~11 trillion comparisons). Above
    ~50M comparisons (or with an explicit --max-df N), --pairs instead
    nominates candidates from an inverted index of signature keys: only
    pairs sharing at least one key present in no more than N directories
    (default 1000) are scored. Nominated pairs get EXACT scores, computed
    from full signatures -- the cap governs who gets looked at, never what
    a look sees. What is given up: pairs whose only overlap is ubiquitous
    keys (.DS_Store and friends), whose similarity is necessarily tiny --
    except for degenerate boilerplate-only directories (two dirs holding
    nothing but identical .DS_Store files score 1.0 exhaustively and are
    not nominated; treating those as discoveries is noise anyway). Every
    registered comparison function scores 0 for key-disjoint signatures, so
    with a large enough N nomination is provably lossless. --max-df 0
    forces exhaustive at any size. Candidate mode is announced on stderr
    and the cap is recorded in grouper_meta -- approximation by consent,
    never silently.

Storage floor (--min-sim):

    --pairs stores every nonzero similarity so --threshold stays a free,
    exploratory knob -- affordable at thousands of directories, ruinous at
    millions, where the weak tail (pairs scoring 0.01 off a few shared
    names) dwarfs everything interesting. --min-sim X keeps only pairs
    scoring >= X: the knob stays free at and above the floor and is sold
    below it. The floor is recorded in grouper_meta, and a --threshold
    below the stored floor is refused outright (rerun --pairs with a lower
    floor) -- never answered silently from an incomplete table. Scores are
    still computed for every nominated pair; the floor governs what is
    KEPT, not what is looked at (--max-df governs that). A size-ratio
    prefilter (skip scoring when the smaller signature couldn't possibly
    reach the floor against the larger) was considered and declined: the
    bound holds for the three current comparison functions but not for the
    commented MAX_SCORES denominator variants, so it would plant a trap in
    the registry; revisit per-function if scoring cost ever matters again
    post-nomination.

Progress (--progress):

    A live bar (stderr) on the build stages, following sumtag's
    conventions: opt-in, appears once a stage has run 2 seconds, redrawn in
    place, cleared on completion, suppressed when stderr is not a terminal.
    Redraws are time-based (a few per second), not event-based: the bar
    appears and keeps ticking even while every worker is mid-stripe and no
    result has landed yet. --pairs shows its phases in turn -- file rows
    loaded, directories signed, then pairs scored -- so a large corpus is
    never silently "warming up". The denominator is known a priori -- N
    files for --index, N*(N-1)/2 comparisons for exhaustive --pairs; under
    candidate nomination the pair count isn't knowable up front, so the bar
    counts directories completed out of N instead.

Caveats: derived tables go stale when sumtag rescans (rerun the pipeline);
dir_files references files.rowid, which VACUUM can renumber -- both fine for
artifacts rebuilt in one command each.
"""

from __future__ import annotations

import argparse
import os
import shutil
import sqlite3
import sys
import time
from collections import Counter
from concurrent.futures import FIRST_COMPLETED, ProcessPoolExecutor, wait
from datetime import datetime, timezone

DEFAULT_FN = "name-score"


# --- Plumbing ----------------------------------------------------------------

def connect(path: str, writable: bool = False) -> sqlite3.Connection:
    """Open the sumtag SQLite database, read-only unless a build stage runs.

    A writable connection gets a generous busy timeout: during --pairs the
    parent commits while late-starting workers may still hold read locks for
    their signature load, and the default 5s would surface as spurious
    "database is locked" errors instead of a short wait.
    """
    if not os.path.exists(path):
        sys.exit(f"grouper: no such database: {path}")
    mode = "rw" if writable else "ro"
    timeout = 600.0 if writable else 5.0
    conn = sqlite3.connect(f"file:{path}?mode={mode}", uri=True,
                           timeout=timeout)
    conn.row_factory = sqlite3.Row
    return conn


def _table_exists(conn: sqlite3.Connection, name: str) -> bool:
    return conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
        (name,)).fetchone() is not None


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def _set_meta(conn: sqlite3.Connection, artifact: str, fn: str,
              threshold: float | None, max_df: int | None = None,
              min_sim: float | None = None) -> None:
    """Record provenance for a built artifact ('pairs' or 'groups').

    max_df is the candidate-nomination cap the pair table was built with
    (NULL = exhaustive all-pairs); min_sim is its storage floor (NULL = all
    nonzero pairs kept). Older databases predate these columns, so they are
    added in place when missing.
    """
    conn.execute("""
        CREATE TABLE IF NOT EXISTS grouper_meta (
          artifact  TEXT PRIMARY KEY,
          fn        TEXT NOT NULL,
          threshold REAL,              -- NULL for the pair table
          built_at  TEXT NOT NULL,
          max_df    INTEGER,           -- nomination cap; NULL = exhaustive
          min_sim   REAL               -- storage floor; NULL = keep nonzero
        )""")
    cols = {r[1] for r in conn.execute("PRAGMA table_info(grouper_meta)")}
    for col, typ in (("max_df", "INTEGER"), ("min_sim", "REAL")):
        if col not in cols:
            conn.execute(f"ALTER TABLE grouper_meta ADD COLUMN {col} {typ}")
    conn.execute("""
        INSERT INTO grouper_meta(artifact, fn, threshold, built_at,
                                 max_df, min_sim)
        VALUES (?, ?, ?, ?, ?, ?)
        ON CONFLICT(artifact) DO UPDATE SET
            fn=excluded.fn, threshold=excluded.threshold,
            built_at=excluded.built_at, max_df=excluded.max_df,
            min_sim=excluded.min_sim
        """, (artifact, fn, threshold, _now_iso(), max_df, min_sim))


def _get_meta(conn: sqlite3.Connection, artifact: str):
    if not _table_exists(conn, "grouper_meta"):
        return None
    return conn.execute(
        "SELECT * FROM grouper_meta WHERE artifact=?", (artifact,)).fetchone()


# --- Progress bar --------------------------------------------------------------
#
# Follows sumtag's --progress conventions (CLAUDE.md): explicit opt-in flag,
# stderr only, appears once the stage has been running 2 seconds, redrawn in
# place a few times a second, cleared the moment the stage completes, and
# suppressed outright when stderr is not a terminal. Unlike sumtag's
# within-file bar the unit is work items, not bytes -- and the total is known
# a priori: N files for --index, N*(N-1)/2 comparisons for --pairs.

_TRIGGER_S = 2.0    # bar appears once a stage has run this long
_REDRAW_S = 0.25    # ...and then redraws at most this often


def _human_count(n: float) -> str:
    if n >= 1e9:
        return f"{n / 1e9:.2f}G"
    if n >= 1e6:
        return f"{n / 1e6:.2f}M"
    if n >= 1e3:
        return f"{n / 1e3:.1f}k"
    return f"{n:.0f}"


def _human_eta(secs: float) -> str:
    """Compact duration (45s, 5m12s, 1h05m) -- an estimate, so deliberately
    styled unlike elapsed's clock format, same as sumtag's ETA field."""
    secs = int(secs)
    if secs < 60:
        return f"{secs}s"
    if secs < 3600:
        return f"{secs // 60}m{secs % 60:02d}s"
    return f"{secs // 3600}h{(secs % 3600) // 60:02d}m"


class Progress:
    """A single build stage's bar: construct with the known total, add() as
    work completes, finish() to clear. Inert unless enabled AND stderr is a
    terminal, so callers never need to guard their add() calls."""

    def __init__(self, total: int, unit: str, enabled: bool):
        self.total = total
        self.unit = unit
        self.enabled = enabled and sys.stderr.isatty() and total > 0
        self.done = 0
        self._t0 = time.monotonic()
        self._next_draw = self._t0 + _TRIGGER_S
        self._drawn = 0             # width of whatever is on screen now

    def add(self, n: int) -> None:
        self.done += n
        if not self.enabled:
            return
        now = time.monotonic()
        if now < self._next_draw:
            return
        self._next_draw = now + _REDRAW_S
        self._draw(now)

    def poke(self) -> None:
        """Redraw (if due) without recording progress. Callers waiting on
        workers call this on a short timeout so the bar appears at the
        2-second mark and elapsed/ETA keep ticking even before the first
        result lands -- otherwise a long-running stripe would leave the
        terminal silent for minutes."""
        self.add(0)

    def _draw(self, now: float) -> None:
        elapsed = now - self._t0
        frac = min(self.done / self.total, 1.0)
        rate = self.done / elapsed if elapsed > 0 else 0.0
        if 0 < self.done < self.total and rate > 0:
            eta = _human_eta((self.total - self.done) / rate)
        elif self.done >= self.total:
            eta = "0s"
        else:
            eta = "?"       # no work finished yet; nothing to extrapolate
        left = (f"{_human_count(self.done):>7}/{_human_count(self.total)} "
                f"{self.unit}  {_human_count(rate):>7}/s  [")
        h, rem = divmod(int(elapsed), 3600)
        m, s = divmod(rem, 60)
        right = f"] {int(frac * 100):>3}%  {h}:{m:02d}:{s:02d}  ETA {eta:<7}"
        # Re-measured each redraw, so the bar tracks a resized terminal
        # without any SIGWINCH machinery (cheap at a few redraws per second).
        cols = shutil.get_terminal_size((80, 24)).columns
        bar_w = max(cols - len(left) - len(right) - 1, 5)
        filled = int(bar_w * frac)
        bar = "=" * filled + (">" if filled < bar_w else "") \
            + " " * (bar_w - filled - 1)
        line = left + bar + right
        pad = " " * max(self._drawn - len(line), 0)
        sys.stderr.write("\r" + line + pad)
        sys.stderr.flush()
        self._drawn = len(line)

    def finish(self) -> None:
        """Clear the bar (if one was ever drawn) so normal output isn't
        garbled; also called on error paths, hence the callers' try/finally."""
        if self._drawn:
            sys.stderr.write("\r" + " " * self._drawn + "\r")
            sys.stderr.flush()
            self._drawn = 0


# --- Directory index (stage 1) -----------------------------------------------

DIR_INDEX_SQL = """
DROP TABLE IF EXISTS dir_files;
DROP TABLE IF EXISTS dirs;

CREATE TABLE dirs (
  id            INTEGER PRIMARY KEY,
  mountpoint_id INTEGER NOT NULL REFERENCES mountpoints(id),
  rel_path      TEXT NOT NULL,          -- '' is the mount point itself
  UNIQUE (mountpoint_id, rel_path)
);

CREATE TABLE dir_files (
  file_id INTEGER PRIMARY KEY,          -- files.rowid; a file has one directory
  dir_id  INTEGER NOT NULL REFERENCES dirs(id)
);

-- The key that makes "all files in this directory" fast: without it the
-- dir_id -> files mapping would be a full scan of dir_files.
CREATE INDEX idx_dir_files_dir ON dir_files(dir_id);
"""


def build_dir_index(conn: sqlite3.Connection, progress: bool = False) -> None:
    """(Re)build dirs/dir_files from the files table (a full drop-and-rebuild).

    Junk filter: a directory whose direct stamped files are all hidden
    (every basename starts with '.') is dropped from the index entirely --
    it never reaches --pairs or the grouping. Directories with at least one
    visible file are kept whole, hidden files included: the filter decides
    which directories exist, not which files count in their signatures.
    """
    if not _table_exists(conn, "files"):
        raise LookupError("not a sumtag database (no files table)")
    conn.executescript(DIR_INDEX_SQL)
    dir_ids: dict[tuple[int, str], int] = {}
    visible: set[int] = set()            # dir ids with >= 1 non-dot file
    files = conn.execute(
        "SELECT rowid, mountpoint_id, rel_path FROM files").fetchall()
    bar = Progress(len(files), "files", progress)
    try:
        for f in files:
            key = (f["mountpoint_id"], os.path.dirname(f["rel_path"]))
            dir_id = dir_ids.get(key)
            if dir_id is None:
                cur = conn.execute(
                    "INSERT INTO dirs(mountpoint_id, rel_path) VALUES (?, ?)",
                    key)
                dir_id = cur.lastrowid
                dir_ids[key] = dir_id
            if not os.path.basename(f["rel_path"]).startswith("."):
                visible.add(dir_id)
            conn.execute(
                "INSERT INTO dir_files(file_id, dir_id) VALUES (?, ?)",
                (f["rowid"], dir_id))
            bar.add(1)
    finally:
        bar.finish()
    hidden = [(d,) for d in dir_ids.values() if d not in visible]
    if hidden:
        conn.executemany("DELETE FROM dir_files WHERE dir_id = ?", hidden)
        conn.executemany("DELETE FROM dirs WHERE id = ?", hidden)
        print(f"grouper: dropped {len(hidden)} directorie(s) with no "
              "visible files", file=sys.stderr)
    conn.commit()


def resolve_dir(conn: sqlite3.Connection, path: str):
    """Map a directory path (absolute or mount-relative) to its dirs row.

    An absolute path that exists locally is split with sumtag's own
    firmlink-aware mount_relative(), so lookups agree exactly with how the
    rows were stored. Otherwise the value is tried as a mount-relative
    rel_path directly -- the detached-database case, where the drive the
    rows describe isn't currently mounted here.
    """
    candidates: list[tuple[str | None, str]] = []
    if os.path.isdir(path):
        from sumtag.store import mount_relative
        mount, rel = mount_relative(path)
        candidates.append((mount, "" if rel == "." else rel))
    candidates.append((None, path.rstrip("/")))

    for mount, rel in candidates:
        if mount is not None:
            row = conn.execute(
                """
                SELECT d.id, d.rel_path, m.path AS mount
                  FROM dirs d JOIN mountpoints m ON m.id = d.mountpoint_id
                 WHERE m.path = ? AND d.rel_path = ?
                """, (mount, rel)).fetchone()
        else:
            row = conn.execute(
                """
                SELECT d.id, d.rel_path, m.path AS mount
                  FROM dirs d JOIN mountpoints m ON m.id = d.mountpoint_id
                 WHERE d.rel_path = ?
                """, (rel,)).fetchone()
        if row is not None:
            return row
    return None


def _dir_files(conn: sqlite3.Connection, dir_id: int) -> list[sqlite3.Row]:
    """One directory's indexed files -- the keyed lookup the index exists for."""
    return conn.execute(
        """
        SELECT f.rel_path, f.algo, f.digest, f.size
          FROM files f JOIN dir_files df ON df.file_id = f.rowid
         WHERE df.dir_id = ?
        """, (dir_id,)).fetchall()


def ls(conn: sqlite3.Connection, path: str) -> int:
    """List the files in one directory via the index."""
    d = resolve_dir(conn, path)
    if d is None:
        print(f"grouper: directory not in index: {path} (run --index?)",
              file=sys.stderr)
        return 1
    rows = _dir_files(conn, d["id"])
    print(f"{os.path.join(d['mount'], d['rel_path'])}  ({len(rows)} file(s))")
    for r in sorted(rows, key=lambda r: r["rel_path"]):
        size = "?" if r["size"] is None else str(r["size"])
        print(f"    {r['algo']}:{r['digest']}  {size:>10}  "
              f"{os.path.basename(r['rel_path'])}")
    return 0


# --- Comparison functions ----------------------------------------------------
#
# A *comparison function* scores how similar two directories' contents are:
# 0.0 = nothing in common, 1.0 = identical. Each is a (signature, score)
# pair: signature(files) distills one directory's file-list into the value
# the scorer consumes, built once per directory; score(sig_a, sig_b) does the
# pairwise math, called N^2/2 times by --pairs. Each function may weigh
# whatever it likes, but must be consistent for the same directories in the
# same database -- both halves are pure, so determinism comes free.
# Signatures must be plain picklable values (dicts/Counters of str tuples,
# never sqlite3.Row), since --jobs ships them to worker processes.
# Registered by name in COMPARISONS; swapped with --fn.
#
# Nomination invariant: score(a, b) > 0 REQUIRES that a and b share at
# least one signature key (dict/Counter key). All registered functions
# satisfy it -- multiset Jaccard needs a common element, name-score needs a
# matched basename -- and --max-df candidate nomination depends on it: the
# inverted index is built over signature keys, so a pair sharing no key is
# safe to never score. A future function that can score key-disjoint
# signatures above zero would silently break nomination; don't write one
# without revisiting _kept_index.

def sig_digest(files) -> Counter:
    """Signature: multiset of (algo, digest) -- content only, names ignored.

    Digests only compare within one algorithm (the mixed-algorithm hazard,
    CLAUDE.md Database storage), so the tuples carry algo alongside digest.
    """
    return Counter((r["algo"], r["digest"]) for r in files)


def sig_name_digest(files) -> Counter:
    """Signature: multiset of (basename, algo, digest) -- renames count against.

    All-or-nothing per file: a byte-identical file under a different name
    matches nothing here.
    """
    return Counter((os.path.basename(r["rel_path"]), r["algo"], r["digest"])
                   for r in files)


def _multiset_jaccard(a: Counter, b: Counter) -> float:
    """Jaccard similarity on multisets: |A & B| / |A | B|, empty-vs-empty = 1.0.

    Multisets rather than sets so repetition counts: a directory holding two
    copies of the same content is not identical to one holding a single copy.
    A fully renamed copy still scores 1.0 under sig_digest -- filenames were
    never in its signature.
    """
    if not a and not b:
        return 1.0  # two empty directories have identical (empty) contents
    inter = sum((a & b).values())
    union = sum((a | b).values())
    return inter / union


# --- Name-anchored scoring ---------------------------------------------------
#
# Pairs are anchored on filename (unique within a directory, so the
# "effectively N-to-N" comparison collapses to a 1:1 match on basename):
# each name present in both directories scores 1 point, and 2 more if the
# pair's checksums also agree -- 3 for a perfect pair. Partial credit, in
# other words: sharing a name means *something*, sharing name and content
# means everything. A renamed byte-identical file scores 0 here (cmp_digest
# is the function that still sees it). Similarity = score / max possible.
# This gradient suits grouper's purpose: version N and N+1 of a project
# share names exactly where content drifted.
#
# "Max possible" is itself a pluggable ingredient (MAX_SCORES), so denominator
# experiments are one-line registry additions rather than new scoring code.

def max_identical(n_a: int, n_b: int, n_matched: int) -> int:
    """Max score if the directories were identical: 3 * max(|A|, |B|).

    This preserves "1.0 means identical contents": a 1-file directory wholly
    contained in a 100-file directory scores ~0.01, not 1.0. Any denominator
    is imperfect when |A| != |B| -- but the more the counts differ, the more
    likely the directories are dissimilar anyway, so the imperfection is
    self-limiting.
    """
    return 3 * max(n_a, n_b)


def max_smaller(n_a: int, n_b: int, n_matched: int) -> int:
    """Max achievable given the sizes: 3 * min(|A|, |B|). Subset scores 1.0."""
    return 3 * min(n_a, n_b)


def max_matched(n_a: int, n_b: int, n_matched: int) -> int:
    """Max given the names that matched: 3 * n_matched. Loosest of the three."""
    return 3 * n_matched


MAX_SCORES = {
    "identical": max_identical,  # the default; keeps 1.0 == identical
    "smaller": max_smaller,
    "matched": max_matched,
}


def sig_names(files) -> dict:
    """Signature: basename -> (algo, digest); basenames are unique in a dir."""
    return {os.path.basename(r["rel_path"]): (r["algo"], r["digest"])
            for r in files}


def make_name_score(max_fn):
    """Build a name-anchored scorer (over sig_names) around one max-score fn."""
    def score(a: dict, b: dict) -> float:
        matched = a.keys() & b.keys()
        points = len(matched)          # 1 point per shared basename
        for name in matched:
            # Tuple equality requires same algo AND digest: a pair stamped
            # under different algorithms is incomparable, so it earns the
            # name point only -- we never guess about content.
            if a[name] == b[name]:
                points += 2
        denom = max_fn(len(a), len(b), len(matched))
        if denom == 0:
            return 1.0  # two empty directories have identical (empty) contents
        return points / denom
    return score


COMPARISONS = {
    # content only; renames don't matter
    "digest": (sig_digest, _multiset_jaccard),
    # content + filename; renames penalized
    "name-digest": (sig_name_digest, _multiset_jaccard),
    # Name-anchored partial-credit scoring; swap the MAX_SCORES ingredient to
    # register a denominator variant, e.g.:
    #   "name-score-min": (sig_names, make_name_score(max_smaller)),
    "name-score": (sig_names, make_name_score(max_identical)),
}


def compare(conn: sqlite3.Connection, path_a: str, path_b: str,
            fn_name: str) -> int:
    """CLI wrapper: resolve two directory paths, score them, print similarity."""
    make_sig, score = COMPARISONS[fn_name]
    sides = []
    for path in (path_a, path_b):
        d = resolve_dir(conn, path)
        if d is None:
            print(f"grouper: directory not in index: {path} (run --index?)",
                  file=sys.stderr)
            return 1
        sides.append(make_sig(_dir_files(conn, d["id"])))
    print(f"{score(sides[0], sides[1]):.4f}")
    return 0


# --- Pair table (stage 2) ----------------------------------------------------

PAIRS_SQL = """
DROP TABLE IF EXISTS dir_pairs;

CREATE TABLE dir_pairs (
  dir_a      INTEGER NOT NULL REFERENCES dirs(id),   -- dir_a < dir_b: each
  dir_b      INTEGER NOT NULL REFERENCES dirs(id),   -- unordered pair once
  similarity REAL NOT NULL,
  PRIMARY KEY (dir_a, dir_b)
);

-- Exactly the grouping walk's order, so the walk is one index scan.
CREATE INDEX idx_dir_pairs_walk ON dir_pairs(similarity DESC, dir_a, dir_b);
"""


INSERT_PAIR_SQL = \
    "INSERT INTO dir_pairs(dir_a, dir_b, similarity) VALUES (?, ?, ?)"

_INSERT_CHUNK = 100_000     # rows per executemany between progress ticks

# Exhaustive scoring is exact and cheap enough up to roughly this many
# comparisons (~10k dirs, well under a minute); past it, an unflagged run
# switches to candidate nomination with DEFAULT_MAX_DF.
_EXHAUSTIVE_MAX = 50_000_000
DEFAULT_MAX_DF = 1000


def _kept_index(sigs: dict, max_df: int) -> dict:
    """Invert signatures into token -> sorted [dir_id, ...] for nomination.

    Tokens are signature keys (see the nomination invariant above). Tokens
    in a single directory nominate nothing; tokens in more than max_df
    directories are the ubiquitous-noise case the cap exists to exclude --
    a token in k dirs witnesses k*(k-1)/2 candidate pairs, so one
    .DS_Store-grade token would reinstate the all-pairs blowup by itself.
    """
    index: dict = {}
    for d, sig in sigs.items():
        for tok in sig.keys():
            index.setdefault(tok, []).append(d)
    return {t: sorted(ds) for t, ds in index.items()
            if 2 <= len(ds) <= max_df}


def _candidate_partners(index: dict, sig, d: int) -> list:
    """Directories later than d sharing at least one kept token with it.

    Deduplicated here (a pair sharing five rare names is nominated once)
    and sorted so the scoring order is deterministic. The d < partner
    ordering makes each unordered pair the responsibility of exactly one
    outer directory, mirroring the exhaustive triangular loop.
    """
    partners: set = set()
    for tok in sig.keys():
        ds = index.get(tok)
        if ds:
            partners.update(e for e in ds if e > d)
    return sorted(partners)

# Worker-process state, installed by _pairs_init. Each worker loads the
# file rows and builds the signature table ITSELF, from its own read-only
# connection -- nothing large ever crosses the spawn pipe. The first cut
# shipped the parent's finished signature table via initargs, which pickles
# it through a pipe once per worker: at real-archive scale (a 24M-file
# corpus measured ~7-8GB of signatures) that wedged the whole run before
# the first task was even dispatched. Only the database path and function
# name are sent now. The cost is each worker redoing the load (concurrent
# read-only scans, amortized by the OS page cache); the constraint that
# remains is RAM -- every worker holds a full signature table, so --jobs
# is a memory knob as much as a speed knob. The scorer is re-resolved from
# COMPARISONS by name because name-score is a closure, and closures don't
# pickle.
_SIGS: dict | None = None
_IDS: list | None = None
_SCORE = None
_INDEX: dict | None = None      # nomination index; None = exhaustive mode
_FLOOR = 0.0                    # --min-sim storage floor; 0.0 = keep nonzero


def _load_signatures(conn: sqlite3.Connection, fn_name: str,
                     progress: bool = False) -> dict:
    """Stream every indexed file row and distill per-directory signatures.

    Used identically by the serial path (in-process, with progress bars)
    and by each worker's initializer (silently -- stderr belongs to the
    parent's bar).
    """
    make_sig = COMPARISONS[fn_name][0]
    n_files = conn.execute("SELECT COUNT(*) FROM dir_files").fetchone()[0]
    contents: dict[int, list[sqlite3.Row]] = {}
    load_bar = Progress(n_files, "files", progress)
    try:
        for r in conn.execute(
                """
                SELECT df.dir_id AS dir_id, f.rel_path, f.algo, f.digest
                  FROM files f JOIN dir_files df ON df.file_id = f.rowid
                """):
            contents.setdefault(r["dir_id"], []).append(r)
            load_bar.add(1)
    finally:
        load_bar.finish()
    sigs: dict[int, object] = {}
    sig_bar = Progress(len(contents), "dirs", progress)
    try:
        for d, files in contents.items():
            sigs[d] = make_sig(files)
            sig_bar.add(1)
    finally:
        sig_bar.finish()
    return sigs


def _pairs_init(db_path: str, fn_name: str, max_df: int,
                min_sim: float) -> None:
    """max_df of 0 means exhaustive mode (no nomination index)."""
    global _SIGS, _IDS, _SCORE, _INDEX, _FLOOR
    conn = connect(db_path)                # read-only; closed after the load
    try:
        _SIGS = _load_signatures(conn, fn_name)
    finally:
        conn.close()
    _IDS = sorted(_SIGS)
    _SCORE = COMPARISONS[fn_name][1]
    _INDEX = _kept_index(_SIGS, max_df) if max_df else None
    _FLOOR = min_sim


def _score_one(sigs: dict, score, a: int, partners,
               floor: float = 0.0) -> list:
    """Pairs of dir a against the given later dirs scoring >= the floor
    (and above zero -- the default floor of 0.0 keeps all nonzero)."""
    sig_a = sigs[a]
    out = []
    for b in partners:
        s = score(sig_a, sigs[b])
        if s > 0.0 and s >= floor:
            out.append((a, b, s))
    return out


def _score_stripe(outer: range) -> list:
    out = []
    for i in outer:
        a = _IDS[i]
        partners = _IDS[i + 1:] if _INDEX is None \
            else _candidate_partners(_INDEX, _SIGS[a], a)
        out.extend(_score_one(_SIGS, _SCORE, a, partners, _FLOOR))
    return out


def build_pairs(conn: sqlite3.Connection, db_path: str, fn_name: str,
                jobs: int = 1, progress: bool = False,
                max_df: int | None = None, min_sim: float = 0.0) -> None:
    """Score directory pairs and store the similarities worth keeping.

    Exhaustively (every pair, the N^2/2 loop) on smaller corpora, or via
    candidate nomination above _EXHAUSTIVE_MAX comparisons -- see the module
    docstring's "Candidate nomination" section. max_df: None = auto, 0 =
    force exhaustive, N = force nomination at cap N. The comparisons are
    the expensive part, so by default every similarity > 0 is kept
    regardless of any threshold -- the threshold stays a cheap knob at
    grouping time. Zero-similarity pairs are omitted: they can never form
    or join a group, so their rows would buy nothing (and on real corpora
    they are the overwhelming majority). min_sim > 0 additionally drops
    pairs scoring below it at storage time (the module docstring's
    "Storage floor" section); build_groups refuses a --threshold below the
    stored floor.

    The scoring loop fans out across `jobs` worker processes, each of which
    loads its own signature table from the database (see _pairs_init for why
    nothing large is shipped). Workers only score; every database write
    happens here in the parent (SQLite has one writer). The outer loop is
    triangular (row 0 scores N-1 pairs, the last row none), so workers take
    interleaved stripes of it -- rows k, k+S, k+2S... -- and each gets an
    even mix of long and short rows. Stripes are inserted and committed as
    they complete, so insertion order varies run to run, but the pair set is
    identical, and the grouping walk orders by its own index, so groups are
    unaffected. Each stripe's comparison count is known up front, which is
    what drives the `progress` bar's a-priori total of N*(N-1)/2.

    Commits are incremental (per completed stripe / every ~1M rows), so the
    run never owes one giant endgame commit and an interrupted run keeps its
    completed inserts. The 'pairs' provenance row is deleted before the
    first insert and rewritten only on success, so a partial table can never
    masquerade as a finished one: build_groups refuses it with "no stored
    pairs" until --pairs completes.
    """
    if not _table_exists(conn, "dirs"):
        raise LookupError("directory index missing; run --index first")
    score = COMPARISONS[fn_name][1]
    ids = [r[0] for r in conn.execute("SELECT id FROM dirs ORDER BY id")]
    n = len(ids)

    # Tombstone the provenance BEFORE touching the table. Dropping a large
    # dir_pairs is one long uninterruptible C call, and a Ctrl-C during it is
    # delivered at the next bytecode boundary -- if the drop came first, that
    # interrupt would land between "table emptied" and "meta deleted",
    # leaving an empty table wearing valid metadata.
    if _table_exists(conn, "grouper_meta"):
        conn.execute("DELETE FROM grouper_meta WHERE artifact='pairs'")
        conn.commit()

    total = n * (n - 1) // 2
    if max_df is None:
        cap = DEFAULT_MAX_DF if total > _EXHAUSTIVE_MAX else 0
    else:
        cap = max_df                    # 0 = exhaustive forced
    if cap:
        print(f"grouper: candidate nomination, max-df {cap} "
              f"(pairs sharing only commoner tokens are not scored)",
              file=sys.stderr)

    parallel = jobs > 1 and total >= 500_000
    if parallel:
        # WAL lets the workers' long signature reads coexist with the
        # parent's incremental commits; under the default rollback journal
        # each commit needs exclusivity and serializes against the readers
        # (measured 6x slowdown from lock thrash). Restored on success; an
        # interrupted run leaves the database in WAL, which every SQLite
        # reader handles and the next run resets.
        conn.execute("PRAGMA journal_mode=WAL")
    conn.executescript(PAIRS_SQL)

    # Exhaustively the work is exactly N*(N-1)/2 comparisons; under
    # nomination the comparison count isn't knowable up front, so the bar
    # counts outer directories completed instead.
    bar = Progress(n if cap else total, "dirs" if cap else "pairs", progress)
    try:
        if parallel:
            # Below ~half a million pairs the serial loop beats process
            # startup. 8 stripes per worker: interleaving balances better and
            # the bar advances more smoothly, at no measurable stripe cost.
            n_stripes = jobs * 8
            stripes = [range(k, n, n_stripes) for k in range(n_stripes)]
            with ProcessPoolExecutor(max_workers=jobs,
                                     initializer=_pairs_init,
                                     initargs=(db_path, fn_name, cap,
                                               min_sim)) as ex:
                futs = {ex.submit(_score_stripe, st):
                        (len(st) if cap else sum(n - 1 - i for i in st))
                        for st in stripes}
                # Short-timeout wait rather than as_completed: a stripe is
                # 1/(jobs*8) of the whole run, which can be minutes -- the
                # bar must appear and keep ticking before the first one
                # lands, not only when results arrive.
                pending = set(futs)
                while pending:
                    done, pending = wait(pending, timeout=_REDRAW_S,
                                         return_when=FIRST_COMPLETED)
                    for fut in done:
                        # Insert in slices, ticking between them: one giant
                        # stripe's executemany could otherwise freeze the
                        # bar for seconds on a huge corpus.
                        rows = fut.result()
                        for k in range(0, len(rows), _INSERT_CHUNK):
                            conn.executemany(INSERT_PAIR_SQL,
                                             rows[k:k + _INSERT_CHUNK])
                            bar.poke()
                        conn.commit()
                        bar.add(futs[fut])
                    bar.poke()
        else:
            # The serial path builds signatures in-process, with the load
            # phases showing their own bars.
            sigs = _load_signatures(conn, fn_name, progress)
            index = _kept_index(sigs, cap) if cap else None
            since_commit = 0
            for i in range(n):
                a = ids[i]
                partners = ids[i + 1:] if index is None \
                    else _candidate_partners(index, sigs[a], a)
                rows = _score_one(sigs, score, a, partners, min_sim)
                conn.executemany(INSERT_PAIR_SQL, rows)
                since_commit += len(rows)
                if since_commit >= 10 * _INSERT_CHUNK:
                    conn.commit()
                    since_commit = 0
                bar.add(1 if cap else n - 1 - i)
    finally:
        bar.finish()
    _set_meta(conn, "pairs", fn_name, None, cap or None, min_sim or None)
    conn.commit()
    if parallel:
        conn.execute("PRAGMA journal_mode=DELETE")


# --- Grouping (stage 3, the point of the program) ------------------------------

GROUPS_SQL = """
DROP TABLE IF EXISTS group_dirs;
DROP TABLE IF EXISTS groups;

CREATE TABLE groups (
  id INTEGER PRIMARY KEY               -- creation order = best-first discovery
);

CREATE TABLE group_dirs (
  dir_id   INTEGER PRIMARY KEY REFERENCES dirs(id),  -- PK *is* the partition:
  group_id INTEGER NOT NULL REFERENCES groups(id)    -- one group per dir, ever
);
CREATE INDEX idx_group_dirs_group ON group_dirs(group_id);
"""


def build_groups(conn: sqlite3.Connection, threshold: float,
                 fn_arg: str | None) -> None:
    """Walk the stored pairs best-first and persist the resulting partition.

    Each directory is placed exactly once, at the first (highest-similarity)
    pair it appears in, and never reconsidered -- so it cannot end up in two
    groups, and groups never merge. The walk stops at the first pair below
    the threshold: everything past it could not have grouped anyway (the
    lossless "interestingness" shortcut).
    """
    pmeta = _get_meta(conn, "pairs")
    if pmeta is None or not _table_exists(conn, "dir_pairs"):
        raise LookupError("no stored pairs; run --pairs first")
    if fn_arg is not None and fn_arg != pmeta["fn"]:
        raise LookupError(
            f"stored pairs were built with --fn {pmeta['fn']}; "
            f"rerun --pairs --fn {fn_arg} to switch functions")
    # Pairs below a storage floor were computed but never kept, so a
    # threshold under the floor cannot be answered from this table --
    # refuse rather than silently group from incomplete data. Databases
    # written before the column exist have no floor (full nonzero set).
    floor = pmeta["min_sim"] if "min_sim" in pmeta.keys() else None
    if floor is not None and threshold < floor:
        raise LookupError(
            f"stored pairs were built with --min-sim {floor:g}; pairs "
            f"below it were not kept -- rerun --pairs with --min-sim "
            f"{threshold:g} or lower to group at this threshold")

    pairs = conn.execute(
        """
        SELECT dir_a, dir_b FROM dir_pairs
         WHERE similarity >= ?
      ORDER BY similarity DESC, dir_a, dir_b
        """, (threshold,)).fetchall()

    conn.executescript(GROUPS_SQL)
    placed: dict[int, int] = {}  # dir_id -> group_id
    for p in pairs:
        a, b = p["dir_a"], p["dir_b"]
        a_in, b_in = a in placed, b in placed
        if a_in and b_in:
            continue                       # both settled; no merging, ever
        if not a_in and not b_in:          # the two found a new group
            gid = conn.execute("INSERT INTO groups DEFAULT VALUES").lastrowid
            placed[a] = placed[b] = gid
        elif a_in:                         # the fresh one joins the group
            placed[b] = placed[a]
        else:
            placed[a] = placed[b]
    conn.executemany(
        "INSERT INTO group_dirs(dir_id, group_id) VALUES (?, ?)",
        list(placed.items()))
    _set_meta(conn, "groups", pmeta["fn"], threshold)
    conn.commit()


def grouping_current(conn: sqlite3.Connection, threshold: float,
                     fn_arg: str | None) -> bool:
    """True if the stored grouping already reflects this threshold (and fn),
    so --threshold can skip the rebuild and go straight to the report.

    Stale groupings don't count: pairs rebuilt after the grouping was
    persisted mean the grouping no longer describes the stored pairs, even at
    the same threshold.
    """
    gmeta = _get_meta(conn, "groups")
    if gmeta is None or not _table_exists(conn, "group_dirs"):
        return False
    if gmeta["threshold"] != threshold:
        return False
    if fn_arg is not None and fn_arg != gmeta["fn"]:
        return False
    pmeta = _get_meta(conn, "pairs")
    if pmeta is not None and pmeta["built_at"] > gmeta["built_at"]:
        return False
    return True


def report_groups(conn: sqlite3.Connection) -> int:
    """The one reporter: print the stored grouping with its provenance."""
    gmeta = _get_meta(conn, "groups")
    if gmeta is None or not _table_exists(conn, "group_dirs"):
        print("grouper: no stored grouping; run --pairs, then --threshold X",
              file=sys.stderr)
        return 1
    print(f"grouping: fn={gmeta['fn']}  threshold={gmeta['threshold']:g}  "
          f"built {gmeta['built_at']}")
    pmeta = _get_meta(conn, "pairs")
    if pmeta is not None and pmeta["built_at"] > gmeta["built_at"]:
        print("note: pair table is newer than this grouping; "
              "rerun --threshold to refresh")

    # Highest stored similarity between any two members of each group -- the
    # bond that says how alike the group is at its closest point.
    best: dict[int, float] = {}
    if _table_exists(conn, "dir_pairs"):
        best = {r["group_id"]: r["best"] for r in conn.execute(
            """
            SELECT ga.group_id AS group_id, MAX(p.similarity) AS best
              FROM dir_pairs p
              JOIN group_dirs ga ON ga.dir_id = p.dir_a
              JOIN group_dirs gb ON gb.dir_id = p.dir_b
                               AND gb.group_id = ga.group_id
          GROUP BY ga.group_id
            """)}

    rows = conn.execute(
        """
        SELECT gd.group_id, m.path AS mount, d.rel_path
          FROM group_dirs gd
          JOIN dirs d ON d.id = gd.dir_id
          JOIN mountpoints m ON m.id = d.mountpoint_id
      ORDER BY gd.group_id, m.path, d.rel_path
        """).fetchall()
    by_gid: dict[int, list[str]] = {}
    for r in rows:
        path = os.path.join(r["mount"], r["rel_path"]) if r["rel_path"] \
            else r["mount"]
        by_gid.setdefault(r["group_id"], []).append(path)

    for gid in sorted(by_gid):           # creation order: strongest bonds first
        members = by_gid[gid]
        top = f", best pair {best[gid]:.3f}" if gid in best else ""
        print(f"\ngroup {gid}  ({len(members)} directories{top})")
        for path in members:
            print(f"    {path}")
    print(f"\n{len(by_gid)} group(s), {len(rows)} directorie(s)")
    return 0


# --- Duplicate-file report (inspection helper) --------------------------------

# Digest of zero-length input, per algorithm. Every empty file shares its
# algorithm's constant, so --top excludes empties by digest value -- reliable
# even when the nullable, --locate-populated size column was never filled in.
EMPTY_DIGESTS = {
    "xxh3": "2d06800538d394c2",
    "md5": "d41d8cd98f00b204e9800998ecf8427e",
}


def find_duplicate_groups(conn: sqlite3.Connection, min_count: int,
                          top_n: int | None = None,
                          exclude_empty: bool = False):
    """Yield (algo, digest, [rows]) for every digest shared by >= min_count files.

    Grouping is by (algo, digest): a digest only means "same bytes" within one
    algorithm, so two files stamped under different algos won't group together
    even if identical -- the documented mixed-algorithm hazard (CLAUDE.md
    Database storage). We surface the algo in the output so that's visible.

    top_n keeps only the N most frequent digests; exclude_empty drops empty
    files (matched by EMPTY_DIGESTS, plus size <> 0 where size is known).
    """
    params: list = []
    where = ""
    if exclude_empty:
        clauses = []
        for algo, digest in sorted(EMPTY_DIGESTS.items()):
            clauses.append("NOT (algo = ? AND digest = ?)")
            params += [algo, digest]
        clauses.append("(size IS NULL OR size <> 0)")
        where = "WHERE " + " AND ".join(clauses)
    params.append(min_count)
    limit = ""
    if top_n is not None:
        limit = "LIMIT ?"
        params.append(top_n)
    dup_keys = conn.execute(
        f"""
        SELECT algo, digest, COUNT(*) AS n
          FROM files
          {where}
      GROUP BY algo, digest
        HAVING n >= ?
      ORDER BY n DESC, digest
        {limit}
        """,
        params,
    ).fetchall()

    for key in dup_keys:
        rows = conn.execute(
            """
            SELECT f.rel_path, f.inode, f.size, m.path AS mount
              FROM files f
              JOIN mountpoints m ON m.id = f.mountpoint_id
             WHERE f.algo = ? AND f.digest = ?
          ORDER BY m.path, f.rel_path
            """,
            (key["algo"], key["digest"]),
        ).fetchall()
        yield key["algo"], key["digest"], rows


def report_dups(conn: sqlite3.Connection, min_count: int,
                top_n: int | None = None,
                exclude_empty: bool = False) -> None:
    groups = list(find_duplicate_groups(conn, min_count, top_n, exclude_empty))
    if not groups:
        print("no duplicate groups found")
        return

    total_files = 0
    for algo, digest, rows in groups:
        total_files += len(rows)
        # Distinct inodes reveal real copies vs hard links to the same data:
        # same inode = same bytes on disk, deleting one frees nothing.
        inodes = {r["inode"] for r in rows}
        hardlink_note = "" if len(inodes) == len(rows) else \
            f"  ({len(inodes)} distinct inode(s) -- some are hard links)"
        print(f"\n{algo}:{digest}  x{len(rows)}{hardlink_note}")
        for r in rows:
            print(f"    [ino {r['inode']}]  {os.path.join(r['mount'], r['rel_path'])}")

    print(f"\n{len(groups)} group(s), {total_files} file(s) total")


# --- CLI ----------------------------------------------------------------------

def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="grouper.py",
        description="Find groups of directories with identical or similar "
                    "contents in a sumtag database. With no action flags, "
                    "reports the stored grouping.",
    )
    parser.add_argument("--database", required=True, metavar="DB",
                        help="path to the sumtag SQLite database")
    parser.add_argument("--prep", action="store_true",
                        help="stages 1+2: --index then --pairs in one shot")
    parser.add_argument("--index", action="store_true",
                        help="stage 1: (re)build the dirs/dir_files directory "
                             "index (silent)")
    parser.add_argument("--pairs", action="store_true",
                        help="stage 2: compare every directory to every other "
                             "and store all nonzero similarities (silent)")
    parser.add_argument("--threshold", type=float, metavar="X",
                        help="stage 3: build + persist the grouping from "
                             "stored pairs, using X (0.0-1.0) as the "
                             "similar-enough bar (skipped if the stored "
                             "grouping is already current), then report it")
    parser.add_argument("--fn", choices=sorted(COMPARISONS),
                        help=f"comparison function for --pairs/--compare "
                             f"(default: {DEFAULT_FN})")
    parser.add_argument("--jobs", type=int, metavar="N",
                        default=os.cpu_count() or 1,
                        help="worker processes for --pairs scoring "
                             "(default: all CPUs); 1 disables "
                             "multiprocessing")
    parser.add_argument("--progress", action="store_true",
                        help="live progress bar on stderr for the build "
                             "stages (--index/--pairs): appears once a stage "
                             "has run 2s, cleared when done; suppressed when "
                             "stderr is not a terminal")
    parser.add_argument("--max-df", type=int, metavar="N", default=None,
                        help="--pairs: score only candidate pairs sharing "
                             "at least one signature token present in at "
                             "most N directories, instead of exhaustive "
                             "all-pairs; 0 forces exhaustive. Default: "
                             "exhaustive up to ~10k dirs, then candidates "
                             f"with N={DEFAULT_MAX_DF}")
    parser.add_argument("--min-sim", type=float, metavar="X", default=0.0,
                        help="--pairs: keep only pairs scoring at least X "
                             "(0.0-1.0); a later --threshold below X is "
                             "refused. Default 0.0: keep every nonzero "
                             "pair")
    parser.add_argument("--ls", metavar="DIR",
                        help="list the files in DIR (absolute or "
                             "mount-relative) using the directory index")
    parser.add_argument("--compare", nargs=2, metavar=("DIR_A", "DIR_B"),
                        help="print the similarity (0.0-1.0) of two "
                             "directories' contents")
    parser.add_argument("--dups", action="store_true",
                        help="report duplicate files (same digest)")
    parser.add_argument("--top", type=int, nargs="?", const=1, metavar="N",
                        help="report the N (default 1) most frequently "
                             "occurring checksums and their files, excluding "
                             "empty files")
    parser.add_argument("--min", type=int, default=2, metavar="N",
                        help="--dups/--top: only groups of at least N files "
                             "(default: 2)")
    args = parser.parse_args(argv)

    if args.threshold is not None and not 0.0 <= args.threshold <= 1.0:
        parser.error("--threshold must be between 0.0 and 1.0")
    if args.jobs < 1:
        parser.error("--jobs must be at least 1")
    if args.max_df is not None and args.max_df < 0:
        parser.error("--max-df must be 0 (exhaustive) or a positive cap")
    if not 0.0 <= args.min_sim <= 1.0:
        parser.error("--min-sim must be between 0.0 and 1.0")
    if args.dups and args.top is not None:
        parser.error("--dups and --top are mutually exclusive")
    if args.top is not None and args.top < 1:
        parser.error("--top must be at least 1")

    if args.prep:
        args.index = args.pairs = True

    building = args.index or args.pairs or args.threshold is not None
    conn = connect(args.database, writable=building)
    try:
        try:
            if args.index:
                build_dir_index(conn, progress=args.progress)
            if args.pairs:
                build_pairs(conn, args.database, args.fn or DEFAULT_FN,
                            jobs=args.jobs, progress=args.progress,
                            max_df=args.max_df, min_sim=args.min_sim)
            if args.threshold is not None \
                    and not grouping_current(conn, args.threshold, args.fn):
                build_groups(conn, args.threshold, args.fn)
        except LookupError as e:
            print(f"grouper: {e}", file=sys.stderr)
            return 1

        if args.ls is not None:
            return ls(conn, args.ls)
        if args.compare is not None:
            return compare(conn, args.compare[0], args.compare[1],
                           args.fn or DEFAULT_FN)
        if args.dups:
            report_dups(conn, args.min)
            return 0
        if args.top is not None:
            report_dups(conn, args.min, top_n=args.top, exclude_empty=True)
            return 0
        if args.threshold is not None or not building:
            return report_groups(conn)
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
