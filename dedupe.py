#!/usr/bin/env python3
"""dedupe.py -- delete files from a cull tree that duplicate an actual tree.

The most dangerous companion program: it deletes files. Given a sumtag
database and two directories known in it -- ACTUAL (the copy being kept,
never touched) and CULL (the redundant copy being dismantled) -- it walks
the two trees in lockstep and deletes every cull-side file whose digest
matches a file in the *corresponding* actual directory, regardless of name.
A directory left empty by this is removed on the way back up. What survives
in cull is therefore always a signpost: something unique, unknown, or
suspicious is still in there. The cull root itself is never removed, even
when empty -- it stays as a placeholder and a receipt of where the cull
happened.

The synchronized walk (the load-bearing design decision):

    Both trees are walked depth-first AT THE SAME TIME, locked in step by
    relative path: while visiting actual/foo/bar the walk is in
    cull/foo/bar, and it never descends into a subdirectory name that does
    not exist as a real directory on both sides. Comparison and deletion
    are therefore FLAT, one directory pair at a time: a cull file dies iff
    a same-digest file sits in the corresponding actual directory -- same
    name not required, same relative directory required. Reorganized
    duplicates are out of scope by design, in exchange for a comparison a
    human (or an auditor of the deletion log) can verify by looking at one
    directory pair at a time. All copies die: if cull holds three files
    with a witnessed digest, all three are deleted.

    The one exception to strict sync is the empty-directory carve-out: a
    cull-side subdirectory with NO real files anywhere beneath it (only
    ignorables, qualifying symlinks, and empty directories) is swept even
    without an actual-side counterpart -- deleting it destroys nothing,
    and it preserves the signpost invariant.

What is deleted, and what never is:

    Only plain files positively identified as duplicates, plus the exit
    junk described below, and only ever on the cull side. Files the
    database does not know (never stamped/mirrored) are invisible: never
    matched, never deleted, left in place even if that leaves cull
    non-empty -- so be it. The actual tree is never modified. The
    filesystem rows deleted from disk are also deleted from the database
    (dedupe cleans up its own kills; --prune-dirs would not notice files
    missing from directories that survive).

    Ignorable junk (IGNORABLE_NAMES, notably .DS_Store) is not content: it
    never blocks a directory's removal and is swept exactly when it is the
    last thing standing between an otherwise-empty directory and its rmdir
    -- never earlier ("(b)", settled 2026-07-16).

Symbolic links:

    Never followed, never digest-matched. A symlink is sweepable at exit
    (like .DS_Store) iff it is RELATIVE (its target does not begin with /)
    and points into the cull tree -- a link into territory being culled is
    junk once that territory empties, and whether anything is still at the
    target does not matter (the deletion phase has usually just emptied
    it). Any absolute symlink is non-deletable, wherever it points, as is
    any link escaping the cull tree: real content, blocks its directory's
    removal, never deleted. (Confirmed 2026-07-16 after the session crash;
    a post-crash draft briefly dropped the relative-only requirement.)

@sumtag-ignore markers (the fence, re-settled 2026-07-16 post-crash):

    A cull-side directory containing an @sumtag-ignore marker is fenced:
    not descended, nothing inside it deleted, swept, or removed, and the
    directory itself never empties -- exactly sumtag's own traversal rule.
    (Sumtag never scanned inside it either, so the database should hold no
    rows there anyway; the fence makes that a guarantee rather than an
    expectation, and keeps the junk sweep out too.) A fenced directory
    blocks its parent's removal like any other survivor. A marker on the
    CULL root itself is honored with a warning, sumtag's marker-on-root
    rule: the run does nothing. Actual-side markers need no handling --
    an unscanned directory has no rows, so it witnesses nothing.

Trust model (settled 2026-07-16): stored digests are trusted -- at
terabyte scale re-hashing either side is off the table, and deleting a
true duplicate is reversible in the sense that actual still holds the
bytes. That argument only holds if the stored digests describe the
CURRENT bytes, so three zero-content-I/O checks veto any candidate:

    - the database digest must equal the file's own xattr digest (the two
      sinks agree), on BOTH sides, and
    - the file's live mtime must equal the xattr's file_mtime (the file
      has not been modified since it was stamped), on BOTH sides, and
    - the cull file's live size must equal its witness's live size. XXH3-64
      is a 64-bit non-cryptographic hash; the size check is free collision
      insurance, and a digest match with a size mismatch is a loud
      MISMATCH warning (and exit 2), never a deletion.

A file failing a check is kept, with a warning; an actual-side failure
just means that file witnesses nothing.

Safety checks, all before anything is deleted:

    - ACTUAL and CULL must exist, be directories, and be disjoint after
      realpath resolution: not the same directory and neither nested
      inside the other (symlinks cannot disguise an overlap).
    - The database must already exist (never created here) and must know
      both roots: each root's live mount point must be recorded, with
      file rows under the root -- an absent or elsewhere-mounted drive is
      an error, not a mass match failure.
    - A candidate set spanning more than one digest algorithm is refused
      unless --allow-mixed: digests only compare within one algorithm, so
      mixed stamping makes duplicates silently survive (the CLAUDE.md
      mixed-algorithm hazard).
    - Identity check at the moment of deletion: if realpath(cull file) ==
      realpath(actual witness), the two paths are one directory entry
      reached twice (aliasing through links) -- refused loudly, since
      "deleting the copy" would delete the only name. Same digest with
      same inode but DIFFERENT realpaths is a genuine hard link: safe to
      delete (the data keeps its actual-side name) and merely noted.

Arming model: a bare run is a full PREVIEW -- every would-delete,
would-sweep, and would-rmdir printed, nothing touched, database opened
read-only. Deletion requires the explicit --delete flag. This inverts
sumtag's -n convention on purpose; lethality earns opt-in, not opt-out.

Exit status (house 0/1/2): 0 = nothing redundant found; 1 = duplicates
found (deleted, or would-delete under preview); 2 = errors or a safety
refusal prevented a complete, clean run. Ctrl-C prints the same summary
(counters only ever count completed work; database commits are per
directory, so the database matches what actually happened) and exits 130,
sumtag's convention.

Usage:
    dedupe.py --database DB ACTUAL CULL              # preview (default)
    dedupe.py --database DB ACTUAL CULL --delete     # actually delete
    dedupe.py --database DB ACTUAL CULL --allow-mixed ...
"""

from __future__ import annotations

import argparse
import os
import sqlite3
import sys

from sumtag import schema, xattr
from sumtag.progress import human_size
from sumtag.store import mount_relative

#: Junk that never blocks a directory's removal and is swept at exit.
IGNORABLE_NAMES = {".DS_Store"}

#: Sumtag's ignore-marker filename; its presence fences a directory off.
MARKER = "@sumtag-ignore"

EXIT_NOTHING = 0        # nothing redundant found
EXIT_CULLED = 1         # duplicates found (deleted or would-delete)
EXIT_ERRORS = 2         # errors or safety refusal
EXIT_INTERRUPTED = 130  # Ctrl-C (128 + SIGINT, the shell convention)


# --- Database access ---------------------------------------------------------

class Db:
    """Thin read (and, armed, delete) access to the sumtag files table."""

    def __init__(self, path: str, writable: bool) -> None:
        if not os.path.exists(path):
            raise FileNotFoundError(f"no such database: {path}")
        mode = "rw" if writable else "ro"
        self._conn = sqlite3.connect(f"file:{path}?mode={mode}", uri=True)
        self._conn.row_factory = sqlite3.Row

    def mountpoint_id(self, mount: str) -> int | None:
        row = self._conn.execute(
            "SELECT id FROM mountpoints WHERE path = ?", (mount,)).fetchone()
        return None if row is None else row[0]

    def _prefix_where(self, rel_prefix: str) -> tuple[str, list]:
        if not rel_prefix:
            return "", []
        p = rel_prefix.rstrip("/") + "/"
        return " AND substr(rel_path, 1, ?) = ?", [len(p), p]

    def count_under(self, mp_id: int, rel_prefix: str) -> int:
        where, params = self._prefix_where(rel_prefix)
        return self._conn.execute(
            f"SELECT COUNT(*) FROM files WHERE mountpoint_id = ?{where}",
            [mp_id] + params).fetchone()[0]

    def algos_under(self, mp_id: int, rel_prefix: str) -> set[str]:
        where, params = self._prefix_where(rel_prefix)
        return {r[0] for r in self._conn.execute(
            f"SELECT DISTINCT algo FROM files WHERE mountpoint_id = ?{where}",
            [mp_id] + params)}

    def rows_in_dir(self, mp_id: int, dir_rel: str) -> list[sqlite3.Row]:
        """The rows *directly* inside dir_rel (dirname equality, the same
        pattern as sumtag's --prune-dirs)."""
        if dir_rel:
            prefix = dir_rel + "/"
            return self._conn.execute(
                "SELECT rel_path, algo, digest FROM files "
                "WHERE mountpoint_id = ? AND substr(rel_path, 1, ?) = ? "
                "AND instr(substr(rel_path, ?), '/') = 0",
                (mp_id, len(prefix), prefix, len(prefix) + 1)).fetchall()
        return self._conn.execute(
            "SELECT rel_path, algo, digest FROM files "
            "WHERE mountpoint_id = ? AND instr(rel_path, '/') = 0",
            (mp_id,)).fetchall()

    def delete_row(self, mp_id: int, rel_path: str) -> None:
        self._conn.execute(
            "DELETE FROM files WHERE mountpoint_id = ? AND rel_path = ?",
            (mp_id, rel_path))

    def commit(self) -> None:
        self._conn.commit()

    def close(self) -> None:
        self._conn.commit()
        self._conn.close()


class Side:
    """One root and where its rows live in the database.

    The walk and the row lookups use abspath, NOT realpath: sumtag records
    rel_paths from the paths it walked (abspath, symlinks unresolved), so
    dedupe must compute them the same way or a path with a symlinked
    component (macOS's /var -> /private/var, say) silently matches zero
    rows. Pass the roots spelled the way sumtag scanned them; a wrong
    spelling fails the no-rows safety check with a clear error rather
    than deleting nothing quietly. realpath is reserved for the safety
    checks: root disjointness and per-file identity.
    """

    def __init__(self, given: str, db: Db) -> None:
        self.given = given
        self.root = os.path.abspath(given)
        self.real = os.path.realpath(given)   # safety checks only
        self.mount, rel = mount_relative(self.root)
        self.base_rel = "" if rel == "." else rel
        self.mp_id = db.mountpoint_id(self.mount)

    def dir_rel(self, walk_rel: str) -> str:
        """The database rel_path of a walk-relative directory."""
        parts = [p for p in (self.base_rel, walk_rel) if p]
        return "/".join(parts)


# --- The run -----------------------------------------------------------------

class Run:
    """State for one dedupe pass: counters, the armed/preview switch, output."""

    def __init__(self, args, db: Db, actual: Side, cull: Side) -> None:
        self.armed = args.delete
        self.db = db
        self.actual = actual
        self.cull = cull
        self.deleted = 0
        self.deleted_bytes = 0
        self.swept = 0
        self.rmdirs = 0
        self.kept_unique = 0
        self.kept_unknown = 0
        self.kept_stale = 0
        self.fenced = 0
        self.errors = 0

    def say(self, verb: str, path: str) -> None:
        print(f"{'' if self.armed else 'would '}{verb} {path}")

    def warn(self, msg: str) -> None:
        print(f"dedupe: {msg}", file=sys.stderr)


def _read_meta(path: str) -> dict | None:
    raw = xattr.get(path, schema.XATTR_NAME)
    if raw is None:
        return None
    try:
        return schema.loads(raw)
    except (ValueError, KeyError):
        return None


def _fresh_reason(path: str, st: os.stat_result, row) -> str | None:
    """Why a file's stored digest can't be trusted, or None if it can.

    Zero content I/O (the trust model above): the xattr must agree with the
    database row, and the live mtime must equal the stamped file_mtime.
    """
    meta = _read_meta(path)
    if meta is None:
        return "xattr missing or unreadable"
    if meta.get("digests", {}).get(row["algo"]) != row["digest"]:
        return "xattr and database disagree"
    if meta.get("file_mtime") != schema.iso_utc_ns(st.st_mtime_ns):
        return "modified since stamped"
    return None


def _symlink_sweepable(link_path: str, cull: Side) -> bool:
    """A symlink is exit-junk iff RELATIVE and pointing into the cull tree.

    An absolute symlink is never deletable, wherever it points (confirmed
    2026-07-16): it is content that blocks its directory's removal. A
    relative link is junk iff its target lies inside the cull tree --
    where it points is what matters, not whether anything is still there
    (the deletion phase has usually just emptied it). The target is checked
    against both spellings of the cull root (abspath and realpath), itself
    resolved both lexically and via realpath, so neither a symlinked root
    component nor a multi-hop link disguises which tree it points into.
    """
    try:
        target = os.readlink(link_path)
    except OSError:
        return False
    if os.path.isabs(target):
        return False
    target = os.path.join(os.path.dirname(link_path), target)
    for cand in (os.path.normpath(target), os.path.realpath(target)):
        for root in (cull.root, cull.real):
            if cand == root or cand.startswith(root + os.sep):
                return True
    return False


def _fenced(dir_path: str) -> bool:
    """Whether a directory carries the @sumtag-ignore marker (any file type;
    presence is the entire signal, same as sumtag's own rule)."""
    return os.path.lexists(os.path.join(dir_path, MARKER))


def _scan(dir_path: str):
    """Partition a directory: (real subdir names, real file entries, symlink
    names, other names). Ignorable-named files land in files like any other;
    the exit sweep is where their special treatment happens."""
    subdirs, files, symlinks, other = [], [], [], []
    with os.scandir(dir_path) as it:
        for e in sorted(it, key=lambda e: e.name):
            if e.is_symlink():
                symlinks.append(e.name)
            elif e.is_dir(follow_symlinks=False):
                subdirs.append(e.name)
            elif e.is_file(follow_symlinks=False):
                files.append(e)
            else:
                other.append(e.name)  # fifos and friends: opaque, kept
    return subdirs, files, symlinks, other


def _sweep_and_rmdir(run: Run, dir_path: str, sweepables: list[str],
                     dir_rel: str, is_root: bool) -> bool:
    """The exit: sweep the junk, then remove the directory (never the root).
    Only called once the directory holds nothing but ``sweepables``."""
    for name in sweepables:
        path = os.path.join(dir_path, name)
        run.say("sweep ", path)
        run.swept += 1
        if run.armed:
            os.remove(path)
            # A stamped ignorable (.DS_Store gets stamped like anything
            # else) leaves a row; deleting is a no-op when there is none.
            run.db.delete_row(run.cull.mp_id,
                              f"{dir_rel}/{name}" if dir_rel else name)
    if is_root:
        return False  # the placeholder rule: the cull root always survives
    run.say("rmdir ", dir_path)
    run.rmdirs += 1
    if run.armed:
        os.rmdir(dir_path)
    return True


def sweep_only(run: Run, dir_path: str, dir_rel: str) -> bool:
    """The carve-out: a cull-only directory with no real files anywhere
    beneath it is swept despite having no actual-side counterpart. Returns
    True if the directory is (or would be) gone."""
    if _fenced(dir_path):
        run.fenced += 1
        return False
    subdirs, files, symlinks, other = _scan(dir_path)
    blockers = len(other)
    for name in subdirs:
        if not sweep_only(run, os.path.join(dir_path, name),
                          f"{dir_rel}/{name}" if dir_rel else name):
            blockers += 1
    sweepables = []
    for e in files:
        if e.name in IGNORABLE_NAMES:
            sweepables.append(e.name)
        else:
            blockers += 1
            run.kept_unique += 1
    for name in symlinks:
        if _symlink_sweepable(os.path.join(dir_path, name), run.cull):
            sweepables.append(name)
        else:
            blockers += 1
    if blockers:
        return False
    return _sweep_and_rmdir(run, dir_path, sweepables, dir_rel, is_root=False)


def process_pair(run: Run, walk_rel: str, is_root: bool = False) -> bool:
    """One synced directory pair, depth-first. Returns True if the cull-side
    directory is (or, in preview, would be) gone afterwards."""
    a_dir = os.path.join(run.actual.root, walk_rel) if walk_rel else run.actual.root
    c_dir = os.path.join(run.cull.root, walk_rel) if walk_rel else run.cull.root
    if _fenced(c_dir):
        if is_root:
            run.warn(f"{c_dir}: cull root contains {MARKER}; nothing to do")
        run.fenced += 1
        return False
    a_subdirs, _, _, _ = _scan(a_dir)
    c_subdirs, c_files, c_symlinks, c_other = _scan(c_dir)
    a_subdirs = set(a_subdirs)
    c_dir_rel = run.cull.dir_rel(walk_rel)

    blockers = len(c_other)

    # Children first (post-order), sync rule for common names, the
    # empty-directory carve-out for cull-only names.
    for name in c_subdirs:
        child_rel = f"{walk_rel}/{name}" if walk_rel else name
        if name in a_subdirs:
            gone = process_pair(run, child_rel)
        else:
            gone = sweep_only(run, os.path.join(c_dir, name),
                              run.cull.dir_rel(child_rel))
        if not gone:
            blockers += 1

    # The actual side's witnesses: digest -> [(abs path, lstat)], verified
    # fresh; the stat rides along for the size and hard-link checks below.
    witnesses: dict[tuple[str, str], list[tuple[str, os.stat_result]]] = {}
    for row in run.db.rows_in_dir(run.actual.mp_id,
                                  run.actual.dir_rel(walk_rel)):
        path = os.path.join(run.actual.mount, row["rel_path"])
        try:
            st = os.lstat(path)
        except OSError:
            continue  # row for a vanished file: witnesses nothing
        if not os.path.isfile(path) or os.path.islink(path):
            continue
        reason = _fresh_reason(path, st, row)
        if reason is not None:
            run.warn(f"{path}: {reason}; not used as a witness")
            continue
        witnesses.setdefault((row["algo"], row["digest"]), []).append((path, st))

    # The flat comparison: cull-side real files against this directory's
    # witnesses. Ignorable names are exit business, not content.
    cull_rows = {os.path.basename(r["rel_path"]): r
                 for r in run.db.rows_in_dir(run.cull.mp_id, c_dir_rel)}
    sweepables = []
    for e in c_files:
        if e.name in IGNORABLE_NAMES:
            sweepables.append(e.name)
            continue
        path = os.path.join(c_dir, e.name)
        row = cull_rows.get(e.name)
        if row is None:
            run.kept_unknown += 1
            blockers += 1
            continue
        st = e.stat(follow_symlinks=False)
        reason = _fresh_reason(path, st, row)
        if reason is not None:
            run.warn(f"{path}: {reason}; kept")
            run.kept_stale += 1
            blockers += 1
            continue
        matches = witnesses.get((row["algo"], row["digest"]))
        if not matches:
            run.kept_unique += 1
            blockers += 1
            continue
        sized = [(w, wst) for w, wst in matches if wst.st_size == st.st_size]
        if not sized:
            run.warn(f"{path}: MISMATCH: digest matches "
                     f"{matches[0][0]} but live sizes differ "
                     f"({st.st_size} vs {matches[0][1].st_size}); kept")
            run.errors += 1
            blockers += 1
            continue
        rp = os.path.realpath(path)
        alias = next((w for w, _ in sized
                      if os.path.realpath(w) == rp), None)
        if alias is not None:
            run.warn(f"{path}: same file as {alias} (one directory entry "
                     f"reached through links); kept")
            run.errors += 1
            blockers += 1
            continue
        hardlink = next((w for w, wst in sized
                         if wst.st_ino == st.st_ino
                         and wst.st_dev == st.st_dev), None)
        if hardlink is not None:
            run.warn(f"{path}: hard link of {hardlink}; deleting the name "
                     f"is safe (the data keeps its actual-side name)")
        run.say("delete", path)
        run.deleted += 1
        run.deleted_bytes += st.st_size
        if run.armed:
            os.remove(path)
            run.db.delete_row(run.cull.mp_id, row["rel_path"])

    for name in c_symlinks:
        if _symlink_sweepable(os.path.join(c_dir, name), run.cull):
            sweepables.append(name)
        else:
            blockers += 1

    if run.armed:
        run.db.commit()  # per directory, so an interrupted run keeps its kills

    if blockers:
        return False
    return _sweep_and_rmdir(run, c_dir, sweepables, c_dir_rel, is_root)


# --- Safety checks and CLI ---------------------------------------------------

def _safety_checks(args) -> tuple[Db, Side, Side] | str:
    """Everything that must hold before a single byte is at risk.
    Returns (db, actual, cull) or an error message."""
    ra = os.path.realpath(args.actual)
    rc = os.path.realpath(args.cull)
    for given, rp in ((args.actual, ra), (args.cull, rc)):
        if not os.path.isdir(rp):
            return f"{given}: no such directory (is the drive mounted?)"
    if ra == rc:
        return "ACTUAL and CULL are the same directory"
    if os.path.commonpath([ra, rc]) in (ra, rc):
        return "ACTUAL and CULL overlap (one contains the other)"

    try:
        db = Db(args.database, writable=args.delete)
    except (FileNotFoundError, sqlite3.OperationalError) as e:
        return str(e)

    actual, cull = Side(args.actual, db), Side(args.cull, db)
    algos: set[str] = set()
    for side, label in ((actual, "ACTUAL"), (cull, "CULL")):
        if side.mp_id is None or \
                db.count_under(side.mp_id, side.base_rel) == 0:
            db.close()
            return (f"{side.given}: no database rows under this {label} root "
                    f"(mounted at {side.mount}); scan it with sumtag first")
        algos |= db.algos_under(side.mp_id, side.base_rel)
    if len(algos) > 1 and not args.allow_mixed:
        db.close()
        return (f"mixed digest algorithms under these roots "
                f"({', '.join(sorted(algos))}): cross-algorithm duplicates "
                f"cannot match and would silently survive; pass "
                f"--allow-mixed to proceed anyway")
    return db, actual, cull


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="dedupe.py",
        description="Delete files in CULL whose digests duplicate files in "
                    "the corresponding directory of ACTUAL (walked in sync). "
                    "A bare run only previews; --delete arms it.")
    parser.add_argument("--database", required=True, metavar="DB",
                        help="path to the sumtag SQLite database that knows "
                             "both trees")
    parser.add_argument("actual", metavar="ACTUAL",
                        help="the directory being kept; never modified")
    parser.add_argument("cull", metavar="CULL",
                        help="the redundant directory to dismantle")
    parser.add_argument("--delete", action="store_true",
                        help="actually delete (default: preview only, "
                             "database opened read-only)")
    parser.add_argument("--allow-mixed", dest="allow_mixed",
                        action="store_true",
                        help="proceed even when the roots carry more than "
                             "one digest algorithm (cross-algorithm "
                             "duplicates silently survive)")
    args = parser.parse_args(argv)

    checked = _safety_checks(args)
    if isinstance(checked, str):
        print(f"dedupe: {checked}", file=sys.stderr)
        return EXIT_ERRORS
    db, actual, cull = checked

    run = Run(args, db, actual, cull)
    interrupted = False
    try:
        process_pair(run, "", is_root=True)
    except KeyboardInterrupt:
        interrupted = True  # commits are per directory; counters are honest
    finally:
        db.close()

    if interrupted:
        print("interrupted")
    verb = "deleted" if run.armed else "would delete"
    kept = run.kept_unique + run.kept_unknown + run.kept_stale
    pairs = [(verb, f"{run.deleted} file{'s' if run.deleted != 1 else ''}, "
                    f"{human_size(run.deleted_bytes, False)}")]
    if run.swept:
        pairs.append(("swept" if run.armed else "would sweep",
                      f"{run.swept} item{'s' if run.swept != 1 else ''}"))
    if run.rmdirs:
        pairs.append(("removed" if run.armed else "would remove",
                      f"{run.rmdirs} director"
                      f"{'y' if run.rmdirs == 1 else 'ies'}"))
    if kept:
        detail = ", ".join(f"{n} {label}" for n, label in
                           ((run.kept_unique, "unique"),
                            (run.kept_unknown, "unknown"),
                            (run.kept_stale, "stale")) if n)
        pairs.append(("kept", f"{kept} file{'s' if kept != 1 else ''} "
                              f"({detail})"))
    if run.fenced:
        pairs.append(("fenced", f"{run.fenced} director"
                                f"{'y' if run.fenced == 1 else 'ies'} "
                                f"({MARKER})"))
    if run.errors:
        pairs.append(("errors", str(run.errors)))
    pairs.append(("database", args.database))
    pairs.append(("actual", args.actual))
    pairs.append(("cull", args.cull))
    width = max(len(label) for label, _ in pairs) + 1
    for label, value in pairs:
        print(f"{label + ':':<{width}} {value}")

    if interrupted:
        return EXIT_INTERRUPTED
    if run.errors:
        return EXIT_ERRORS
    return EXIT_CULLED if run.deleted else EXIT_NOTHING


if __name__ == "__main__":
    raise SystemExit(main())
