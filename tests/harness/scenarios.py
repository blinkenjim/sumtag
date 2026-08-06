"""The scenario catalog — CLAUDE.md's behavior tables made runnable.

Each :class:`Scenario` declares a starting tree (:class:`~tests.harness.corpus.Corpus`),
an optional ``mutate`` step that disturbs the tree after it is stamped but before
sumtag runs, the ``argv`` to pass to sumtag, and a ``check`` that inspects the
real on-disk result and records any failures.

Written test-first: run against today's stub, the scenarios that require sumtag
to *act* (stamp, re-hash, detect corruption) go red, while the ones satisfied by
*inaction* (skip, prune, dry-run) pass trivially — which is exactly correct.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from .corpus import Corpus, FileSpec, PreStamp
from . import oracle


@dataclass
class RunResult:
    """What the sumtag subprocess returned."""

    exit_code: int
    stdout: str
    stderr: str


class Checker:
    """Accumulates assertion failures so one run reports every problem at once."""

    def __init__(self) -> None:
        self.failures: list[str] = []

    def expect(self, condition: bool, message: str) -> None:
        if not condition:
            self.failures.append(message)


@dataclass
class Scenario:
    name: str
    description: str
    corpus: Corpus
    argv: list[str]
    check: Callable[[Path, RunResult, Checker], None]
    mutate: Callable[[Path], None] | None = None
    # Extra sumtag invocations run before the checked run (e.g. to seed a prior
    # database state). The token "{db}" is substituted in argv and extra_runs.
    extra_runs: list[list[str]] = field(default_factory=list)


def _db_for(root: Path) -> str:
    """The database path the runner created alongside this scenario's tree."""
    return str(root) + ".sqlite"


# --- mutate helpers -------------------------------------------------------


def _corrupt_silently(relpath: str) -> Callable[[Path], None]:
    """Overwrite a file's bytes but restore its exact mtime — silent corruption."""

    def mutate(root: Path) -> None:
        p = root / relpath
        ns = p.stat().st_mtime_ns
        p.write_bytes(b"CORRUPTED-CONTENT" * 16)
        os.utime(p, ns=(ns, ns))  # reset mtime: the corruption leaves no mtime trace

    return mutate


def _edit_legitimately(relpath: str) -> Callable[[Path], None]:
    """Overwrite a file's bytes and let its mtime advance naturally — a real edit."""

    def mutate(root: Path) -> None:
        (root / relpath).write_bytes(b"legitimately-edited-content" * 8)

    return mutate


# --- the catalog ----------------------------------------------------------


def catalog() -> list[Scenario]:
    scenarios: list[Scenario] = []

    # 1. A file with no xattr should get stamped. (Requires action -> red vs stub.)
    def check_fresh(root, res, k):
        s = oracle.inspect(root / "data.bin")
        k.expect(s.present, "expected a user.sumtag xattr to be written")
        k.expect(s.digest_matches_content, "stored digest should match file contents")
        k.expect(s.mtime_matches, "file_mtime should equal the file's mtime")
        k.expect(res.exit_code == 0, f"expected exit 0, got {res.exit_code}")

    scenarios.append(Scenario(
        name="fresh_file_gets_stamped",
        description="A file with no xattr is hashed and stamped.",
        corpus=Corpus(files=[FileSpec("data.bin", size=256)]),
        argv=["--sum"],
        check=check_fresh,
    ))

    # 2. An up-to-date file should be skipped. Prestamp a *wrong* digest with a
    #    matching mtime: if it survives, the file was skipped. (Inaction -> green.)
    def check_skip(root, res, k):
        s = oracle.inspect(root / "data.bin")
        k.expect(s.present, "xattr should still be present")
        k.expect(not s.digest_matches_content,
                 "up-to-date file should be skipped; the wrong digest should persist")
        k.expect(res.exit_code == 0, f"expected exit 0, got {res.exit_code}")

    scenarios.append(Scenario(
        name="uptodate_file_skipped",
        description="A file whose recorded mtime matches is not re-hashed.",
        corpus=Corpus(files=[FileSpec("data.bin", size=256,
                                      prestamp=PreStamp("wrong-digest"))]),
        argv=["--sum"],
        check=check_skip,
    ))

    # 3. A stale file (recorded mtime older than the file) should be re-hashed,
    #    so its file_mtime catches up. (Requires action -> red vs stub.)
    def check_stale(root, res, k):
        s = oracle.inspect(root / "data.bin")
        k.expect(s.mtime_matches,
                 "stale file should be restamped so file_mtime catches up")
        k.expect(s.digest_matches_content, "digest should match content after re-hash")
        k.expect(res.exit_code == 0, f"expected exit 0, got {res.exit_code}")

    scenarios.append(Scenario(
        name="stale_file_rehashed",
        description="A file with an outdated recorded mtime is re-hashed.",
        corpus=Corpus(files=[FileSpec("data.bin", size=256,
                                      prestamp=PreStamp("stale"))]),
        argv=["--sum"],
        check=check_stale,
    ))

    # 4. --force re-hashes even an up-to-date file, correcting a wrong digest.
    #    (Requires action -> red vs stub.)
    def check_force(root, res, k):
        s = oracle.inspect(root / "data.bin")
        k.expect(s.digest_matches_content,
                 "--force should re-hash and correct the wrong digest")
        k.expect(res.exit_code == 0, f"expected exit 0, got {res.exit_code}")

    scenarios.append(Scenario(
        name="force_rehashes_uptodate",
        description="--force re-hashes a file that would otherwise be skipped.",
        corpus=Corpus(files=[FileSpec("data.bin", size=256,
                                      prestamp=PreStamp("wrong-digest"))]),
        argv=["--sum", "--force"],
        check=check_force,
    ))

    # 5. THE marquee: silent corruption. Stamp a valid file, corrupt its bytes
    #    without disturbing mtime, then --verify must flag it. (Action -> red.)
    def check_corruption(root, res, k):
        k.expect(res.exit_code == 1,
                 f"--verify should report corruption with exit 1, got {res.exit_code}")

    scenarios.append(Scenario(
        name="silent_corruption_detected",
        description="--verify flags content that changed while mtime did not.",
        corpus=Corpus(files=[FileSpec("data.bin", size=256,
                                      prestamp=PreStamp("valid"))]),
        mutate=_corrupt_silently("data.bin"),
        argv=["--verify"],
        check=check_corruption,
    ))

    # 6. A legitimate edit (content + mtime both change) is NOT corruption.
    #    (Inaction on the exit code -> green vs stub.)
    def check_legit_edit(root, res, k):
        k.expect(res.exit_code == 0,
                 f"a legitimate edit is not corruption; expected exit 0, got {res.exit_code}")

    scenarios.append(Scenario(
        name="legit_edit_not_corruption",
        description="--verify does not flag a normally-edited file as corruption.",
        corpus=Corpus(files=[FileSpec("data.bin", size=256,
                                      prestamp=PreStamp("valid"))]),
        mutate=_edit_legitimately("data.bin"),
        argv=["--verify"],
        check=check_legit_edit,
    ))

    # 7. An @sumtag-ignore marker prunes the subtree in every mode. Wrong digest
    #    under the marker must survive untouched. (Inaction -> green.)
    def check_prune(root, res, k):
        s = oracle.inspect(root / "vendor" / "blob.bin")
        k.expect(not s.digest_matches_content,
                 "files under an @sumtag-ignore marker must not be touched")

    scenarios.append(Scenario(
        name="ignore_marker_prunes_subtree",
        description="A directory with @sumtag-ignore is not descended into.",
        corpus=Corpus(
            files=[FileSpec("vendor/blob.bin", size=256,
                            prestamp=PreStamp("wrong-digest"))],
            ignore_dirs=["vendor"],
        ),
        argv=["--sum"],
        check=check_prune,
    ))

    # 8. --dry-run writes nothing. A fresh file must remain unstamped. (Green.)
    def check_dryrun(root, res, k):
        s = oracle.inspect(root / "data.bin")
        k.expect(not s.present, "--dry-run must not write any xattr")
        k.expect(res.exit_code == 0, f"expected exit 0, got {res.exit_code}")

    scenarios.append(Scenario(
        name="dry_run_writes_nothing",
        description="-n reports but writes no xattr.",
        corpus=Corpus(files=[FileSpec("data.bin", size=256)]),
        argv=["--sum", "-n"],
        check=check_dryrun,
    ))

    # 9. --database mirrors metadata in addition to the xattr.
    def check_db_mirror(root, res, k):
        s = oracle.inspect(root / "data.bin")
        rows = oracle.read_db(_db_for(root))
        k.expect(s.present, "xattr should still be written (mirror is in addition)")
        k.expect(len(rows) == 1, f"expected exactly 1 db row, got {len(rows)}")
        if rows:
            r = rows[0]
            k.expect(r.algo == "xxh3", f"expected algo xxh3, got {r.algo!r}")
            k.expect(r.digest == s.actual_digest, "db digest should match content")
            k.expect(r.rel_path.endswith("data.bin"),
                     f"rel_path should end with data.bin, got {r.rel_path!r}")
            # mountpoint + rel_path must recompose to the actual file -- guards
            # against a rel_path that escapes its mount (the macOS firmlink bug,
            # where relpath produced ../../.. against /System/Volumes/Data).
            recon = os.path.join(r.mountpoint, r.rel_path)
            k.expect(os.path.exists(recon) and os.path.samefile(recon, str(root / "data.bin")),
                     f"mountpoint+rel_path should recompose to the file, got {recon!r}")
        k.expect(res.exit_code == 0, f"expected exit 0, got {res.exit_code}")

    scenarios.append(Scenario(
        name="database_mirrors_metadata",
        description="--database writes a row mirroring the xattr.",
        corpus=Corpus(files=[FileSpec("data.bin", size=256)]),
        argv=["--database", "{db}", "--sum"],
        check=check_db_mirror,
    ))

    # 10. Re-scanning a file UPSERTs its row in place — no duplicate rows.
    def check_db_upsert(root, res, k):
        rows = oracle.read_db(_db_for(root))
        k.expect(len(rows) == 1,
                 f"re-scan should update one row, not duplicate; got {len(rows)}")
        if rows:
            k.expect(rows[0].digest == oracle.inspect(root / "data.bin").actual_digest,
                     "db digest should match content after re-scan")

    scenarios.append(Scenario(
        name="database_upsert_on_rescan",
        description="A second --database run updates the existing row in place.",
        corpus=Corpus(files=[FileSpec("data.bin", size=256)]),
        extra_runs=[["--database", "{db}", "--sum"]],            # first stamp + mirror
        argv=["--force", "--database", "{db}", "--sum"],          # re-hash + re-mirror
        check=check_db_upsert,
    ))

    # 11. --import copies existing xattr metadata WITHOUT computing. Prove it by
    #     prestamping a deliberately wrong digest: it must land in the db verbatim.
    def check_import(root, res, k):
        s = oracle.inspect(root / "data.bin")
        rows = oracle.read_db(_db_for(root))
        k.expect(len(rows) == 1, f"expected 1 imported row, got {len(rows)}")
        if rows:
            k.expect(rows[0].digest != s.actual_digest,
                     "import must copy the stored digest, not recompute it")
            k.expect(rows[0].digest == s.stored_digest,
                     "db digest should equal the xattr digest verbatim")
        k.expect(not s.digest_matches_content, "import must not rewrite the xattr")
        k.expect(res.exit_code == 0, f"expected exit 0, got {res.exit_code}")

    scenarios.append(Scenario(
        name="import_copies_without_computing",
        description="--import feeds the db from xattrs without hashing.",
        corpus=Corpus(files=[FileSpec("data.bin", size=256,
                                      prestamp=PreStamp("wrong-digest"))]),
        argv=["--database", "{db}", "--import"],
        check=check_import,
    ))

    # 12. --import skips files that have no usable xattr metadata.
    def check_import_skips(root, res, k):
        rows = oracle.read_db(_db_for(root))
        k.expect(len(rows) == 0,
                 f"files without metadata should not be imported; got {len(rows)} rows")

    scenarios.append(Scenario(
        name="import_skips_unstamped",
        description="--import does not import a file that lacks an xattr.",
        corpus=Corpus(files=[FileSpec("data.bin", size=256)]),  # no prestamp
        argv=["--database", "{db}", "--import"],
        check=check_import_skips,
    ))

    # 13. -n composes with --import: previews, writes nothing — not even the db file.
    def check_import_dryrun(root, res, k):
        k.expect(not os.path.exists(_db_for(root)),
                 "--import -n must not create the database")
        k.expect(res.exit_code == 0, f"expected exit 0, got {res.exit_code}")

    scenarios.append(Scenario(
        name="import_dry_run_no_writes",
        description="--database --import -n previews without touching the db.",
        corpus=Corpus(files=[FileSpec("data.bin", size=256,
                                      prestamp=PreStamp("valid"))]),
        argv=["--database", "{db}", "--import", "-n"],
        check=check_import_dryrun,
    ))

    # 14. --sum without --locate leaves the locate/stat columns NULL.
    def check_sum_no_stat(root, res, k):
        rows = oracle.read_db(_db_for(root))
        k.expect(len(rows) == 1, f"expected 1 row, got {len(rows)}")
        if rows:
            k.expect(rows[0].size is None,
                     "stat columns must stay NULL without --locate")

    scenarios.append(Scenario(
        name="sum_without_locate_leaves_stat_null",
        description="--sum alone does not populate the locate/stat columns.",
        corpus=Corpus(files=[FileSpec("data.bin", size=256)]),
        argv=["--database", "{db}", "--sum"],
        check=check_sum_no_stat,
    ))

    # 15. --locate captures stat columns (size here, as the easy-to-verify one).
    def check_locate_stat(root, res, k):
        s = oracle.inspect(root / "data.bin")
        rows = oracle.read_db(_db_for(root))
        k.expect(len(rows) == 1, f"expected 1 row, got {len(rows)}")
        if rows:
            actual_size = (root / "data.bin").stat().st_size
            k.expect(rows[0].size == actual_size,
                     f"expected size {actual_size}, got {rows[0].size!r}")
            k.expect(rows[0].digest == s.actual_digest,
                     "--sum --locate together should still compute correctly")

    scenarios.append(Scenario(
        name="locate_captures_stat_columns",
        description="--locate stats every file and writes size/mode/etc. to the db.",
        corpus=Corpus(files=[FileSpec("data.bin", size=256)]),
        argv=["--database", "{db}", "--sum", "--locate"],
        check=check_locate_stat,
    ))

    # 16. --locate implies --import: without --sum or --force, it must not
    #     compute -- prove it the same way as import_copies_without_computing,
    #     with a deliberately wrong prestamped digest that must survive verbatim
    #     -- while still capturing stat columns.
    def check_locate_implies_import(root, res, k):
        s = oracle.inspect(root / "data.bin")
        rows = oracle.read_db(_db_for(root))
        k.expect(len(rows) == 1, f"expected 1 row, got {len(rows)}")
        if rows:
            k.expect(rows[0].digest != s.actual_digest,
                     "--locate must not compute; the wrong stored digest should survive")
            k.expect(rows[0].digest == s.stored_digest,
                     "db digest should equal the xattr digest verbatim")
            actual_size = (root / "data.bin").stat().st_size
            k.expect(rows[0].size == actual_size,
                     f"expected size {actual_size}, got {rows[0].size!r}")
        k.expect(not s.digest_matches_content, "--locate must not rewrite the xattr")

    scenarios.append(Scenario(
        name="locate_without_sum_imports_and_stats",
        description="--locate alone propagates existing metadata and stats, without computing.",
        corpus=Corpus(files=[FileSpec("data.bin", size=256,
                                      prestamp=PreStamp("wrong-digest"))]),
        argv=["--database", "{db}", "--locate"],
        check=check_locate_implies_import,
    ))

    # 17. A row written by a plain --sum run (stat columns NULL) gets its stat
    #     columns filled by a later --locate-only run, without disturbing the
    #     digest already mirrored -- the COALESCE-on-stat-less-update contract.
    def check_locate_fills_existing_row(root, res, k):
        s = oracle.inspect(root / "data.bin")
        rows = oracle.read_db(_db_for(root))
        k.expect(len(rows) == 1,
                 f"--locate re-scan should update the row in place, got {len(rows)}")
        if rows:
            k.expect(rows[0].digest == s.actual_digest,
                     "digest from the earlier --sum run must survive untouched")
            actual_size = (root / "data.bin").stat().st_size
            k.expect(rows[0].size == actual_size,
                     f"expected size {actual_size}, got {rows[0].size!r}")

    scenarios.append(Scenario(
        name="locate_fills_stat_on_existing_row",
        description="--locate backfills stat columns on a row an earlier --sum run created.",
        corpus=Corpus(files=[FileSpec("data.bin", size=256)]),
        extra_runs=[["--database", "{db}", "--sum"]],   # row exists, stat columns NULL
        argv=["--database", "{db}", "--locate"],         # backfill stat, keep the digest
        check=check_locate_fills_existing_row,
    ))

    # --- 2026-08-05 extension: the features added after the original catalog
    # (TODO.md "Extend the conformance harness") -------------------------------

    # 18. --remove strips the stamp from a stamped file.
    def check_remove(root, res, k):
        s = oracle.inspect(root / "data.bin")
        k.expect(not s.present, "--remove should strip the user.sumtag xattr")
        k.expect(res.exit_code == 0, f"expected exit 0, got {res.exit_code}")

    scenarios.append(Scenario(
        name="remove_strips_stamps",
        description="--remove deletes the xattr from a stamped file.",
        corpus=Corpus(files=[FileSpec("data.bin", size=256,
                                      prestamp=PreStamp("valid"))]),
        argv=["--remove"],
        check=check_remove,
    ))

    # 19. --remove -n previews without touching anything. (Inaction -> green.)
    def check_remove_dryrun(root, res, k):
        s = oracle.inspect(root / "data.bin")
        k.expect(s.present, "--remove -n must leave the xattr in place")
        k.expect(res.exit_code == 0, f"expected exit 0, got {res.exit_code}")

    scenarios.append(Scenario(
        name="remove_dry_run_preserves",
        description="--remove -n previews; the stamp survives.",
        corpus=Corpus(files=[FileSpec("data.bin", size=256,
                                      prestamp=PreStamp("valid"))]),
        argv=["--remove", "-n"],
        check=check_remove_dryrun,
    ))

    # 20. A --remove conflict is a CLI error before anything is touched
    #     (representative of the conflict family; the unit suite covers the
    #     whole matrix).
    def check_remove_conflict(root, res, k):
        s = oracle.inspect(root / "data.bin")
        k.expect(res.exit_code == 2,
                 f"conflicting flags should exit 2, got {res.exit_code}")
        k.expect(s.present, "a rejected run must not touch the xattr")

    scenarios.append(Scenario(
        name="remove_verify_conflict_is_cli_error",
        description="--remove --verify is rejected at the CLI, exit 2.",
        corpus=Corpus(files=[FileSpec("data.bin", size=256,
                                      prestamp=PreStamp("valid"))]),
        argv=["--remove", "--verify"],
        check=check_remove_conflict,
    ))

    # 21. The mandatory-action model: a bare `sumtag <dir>` is a CLI error.
    def check_no_action(root, res, k):
        s = oracle.inspect(root / "data.bin")
        k.expect(res.exit_code == 2,
                 f"a run naming no action should exit 2, got {res.exit_code}")
        k.expect(not s.present, "a rejected run must not stamp anything")

    scenarios.append(Scenario(
        name="missing_action_is_a_cli_error",
        description="A run naming no action flag is rejected, exit 2.",
        corpus=Corpus(files=[FileSpec("data.bin", size=256)]),
        argv=[],
        check=check_no_action,
    ))

    # 22. --verify on a file with no usable xattr: unverifiable, exit 2
    #     (errors prevented a complete check -- distinct from corruption's 1).
    def check_unverifiable(root, res, k):
        k.expect(res.exit_code == 2,
                 f"unverifiable should exit 2, got {res.exit_code}")
        k.expect("unverifiable" in res.stdout,
                 "the unverifiable file should be reported by label")

    scenarios.append(Scenario(
        name="verify_unverifiable_exit_2",
        description="--verify reports an unstamped file as unverifiable, exit 2.",
        corpus=Corpus(files=[FileSpec("data.bin", size=256)]),  # no prestamp
        argv=["--verify"],
        check=check_unverifiable,
    ))

    # 23. --no-ignore processes a marked directory the plain run would prune
    #     (the marker file itself is still never stamped).
    def check_no_ignore(root, res, k):
        s = oracle.inspect(root / "vendor" / "blob.bin")
        k.expect(s.present and s.digest_matches_content,
                 "--no-ignore should stamp inside the marked directory")
        marker = oracle.inspect(root / "vendor" / "@sumtag-ignore")
        k.expect(not marker.present, "the marker file itself is never stamped")
        k.expect(res.exit_code == 0, f"expected exit 0, got {res.exit_code}")

    scenarios.append(Scenario(
        name="no_ignore_overrides_marker",
        description="--no-ignore disregards @sumtag-ignore and stamps the subtree.",
        corpus=Corpus(files=[FileSpec("vendor/blob.bin", size=256)],
                      ignore_dirs=["vendor"]),
        argv=["--sum", "--no-ignore"],
        check=check_no_ignore,
    ))

    # 24. --exclude skips matching basenames and prunes matching directories.
    def check_exclude(root, res, k):
        k.expect(oracle.inspect(root / "keep.txt").present,
                 "an unmatched file should be stamped")
        k.expect(not oracle.inspect(root / "movie.vob").present,
                 "a file matching --exclude '*.vob' must not be stamped")
        k.expect(not oracle.inspect(root / "VIDEO_TS" / "inner.txt").present,
                 "a directory matching --exclude is pruned whole")
        k.expect(res.exit_code == 0, f"expected exit 0, got {res.exit_code}")

    scenarios.append(Scenario(
        name="exclude_prunes_by_basename",
        description="--exclude skips matching files and prunes matching directories.",
        corpus=Corpus(files=[FileSpec("keep.txt", size=64),
                             FileSpec("movie.vob", size=64),
                             FileSpec("VIDEO_TS/inner.txt", size=64)]),
        argv=["--sum", "--exclude", "*.vob", "--exclude", "VIDEO_TS"],
        check=check_exclude,
    ))

    # 25. --prescan counts exactly the files the run will hash (the valid
    #     prestamp is excluded from mmm) and prefixes each announcement.
    #     Sizes are fixed (64+64) so the whole prefix is predictable.
    def check_prescan(root, res, k):
        k.expect("1/2 ( 50%)  0B/128B (  0%)" in res.stdout,
                 f"first counter prefix missing from: {res.stdout!r}")
        k.expect("2/2 (100%)  64B/128B ( 50%)" in res.stdout,
                 "second counter prefix missing")
        k.expect("3/" not in res.stdout,
                 "the up-to-date file must not be counted or prefixed")
        k.expect(res.exit_code == 0, f"expected exit 0, got {res.exit_code}")

    scenarios.append(Scenario(
        name="prescan_counts_only_what_will_hash",
        description="--prescan's mmm mirrors the re-hash decision; prefixes match.",
        corpus=Corpus(files=[FileSpec("a.bin", size=64),
                             FileSpec("b.bin", size=64),
                             FileSpec("z-done.bin", size=64,
                                      prestamp=PreStamp("valid"))]),
        argv=["--sum", "--prescan"],
        check=check_prescan,
    ))

    # 26. --prescan with --remove is a CLI error (nothing to count).
    def check_prescan_remove(root, res, k):
        k.expect(res.exit_code == 2,
                 f"--prescan --remove should exit 2, got {res.exit_code}")

    scenarios.append(Scenario(
        name="prescan_remove_conflict_is_cli_error",
        description="--prescan cannot combine with --remove; exit 2.",
        corpus=Corpus(files=[FileSpec("data.bin", size=64)]),
        argv=["--remove", "--prescan"],
        check=check_prescan_remove,
    ))

    # 27. --db-prescan consumes the totals a --prescan --database run stored.
    def check_db_prescan(root, res, k):
        k.expect(res.exit_code == 0, f"expected exit 0, got {res.exit_code}")
        k.expect("using stored prescan totals: 2 files" in res.stdout,
                 f"stored-totals announcement missing from: {res.stdout!r}")

    scenarios.append(Scenario(
        name="db_prescan_uses_stored_totals",
        description="--db-prescan reads mmm/bytes from the stored summary.",
        corpus=Corpus(files=[FileSpec("a.bin", size=64),
                             FileSpec("b.bin", size=64)]),
        extra_runs=[["--sum", "--database", "{db}", "--prescan"]],
        argv=["--sum", "--database", "{db}", "--db-prescan"],
        check=check_db_prescan,
    ))

    # 28. --db-prescan without a stored summary is a hard error before any
    #     side effect -- the database file is not even created.
    def check_db_prescan_missing(root, res, k):
        k.expect(res.exit_code == 2,
                 f"missing summary should exit 2, got {res.exit_code}")
        k.expect("no stored prescan totals" in res.stderr,
                 f"expected the run---prescan-first error, got: {res.stderr!r}")
        k.expect(not os.path.exists(_db_for(root)),
                 "the refusal must not create the database file")

    scenarios.append(Scenario(
        name="db_prescan_without_summary_is_exit_2",
        description="--db-prescan errors out before side effects when no summary exists.",
        corpus=Corpus(files=[FileSpec("data.bin", size=64)]),
        argv=["--sum", "--database", "{db}", "--db-prescan"],
        check=check_db_prescan_missing,
    ))

    # 29. --db-prescan's match-or-error: a summary stored under a different
    #     counting context (here: different --exclude patterns) is refused.
    def check_db_prescan_mismatch(root, res, k):
        k.expect(res.exit_code == 2,
                 f"context mismatch should exit 2, got {res.exit_code}")
        k.expect("--exclude patterns differ" in res.stderr,
                 f"expected the named mismatch, got: {res.stderr!r}")

    scenarios.append(Scenario(
        name="db_prescan_context_mismatch_is_refused",
        description="--db-prescan refuses totals stored under a different context.",
        corpus=Corpus(files=[FileSpec("data.bin", size=64)]),
        extra_runs=[["--sum", "--database", "{db}", "--prescan"]],
        argv=["--sum", "--database", "{db}", "--db-prescan",
              "--exclude", "*.zzz"],
        check=check_db_prescan_mismatch,
    ))

    return scenarios
