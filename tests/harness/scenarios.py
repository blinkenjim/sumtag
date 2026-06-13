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
        argv=[],
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
        argv=[],
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
        argv=[],
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
        argv=["--force"],
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
        argv=[],
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
        argv=["-n"],
        check=check_dryrun,
    ))

    return scenarios
