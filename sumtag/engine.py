"""Orchestration: walk the tree and stamp, or verify, each file.

Ties together the I/O-free decision logic (:mod:`decide`) with the I/O edges
(:mod:`walk`, :mod:`xattr`, :mod:`hashing`). Returns the process exit code
(CLAUDE.md "main() contract" / EXIT STATUS).
"""

from __future__ import annotations

import os
import sys
from dataclasses import dataclass

from stat import S_ISDIR, S_ISREG

from . import decide, hashing, progress as progress_mod, schema, store as store_mod, walk, xattr
from .store import StatData

EXIT_OK = 0
EXIT_CORRUPTION = 1     # --verify: mismatches; --prune-dirs: stale dirs found
EXIT_ERRORS = 2
EXIT_INTERRUPTED = 130  # 128 + SIGINT, the shell convention for Ctrl-C


@dataclass
class RunStats:
    """Per-run counters behind the end-of-run summary (CLAUDE.md "Run summary").

    Counters reflect *completed* work: a file is counted only once its hash
    (or verify read, or removal) finished, so the summary printed after a
    Ctrl-C never claims a file whose work was cut short.
    """
    hashed: int = 0
    hashed_bytes: int = 0
    imported: int = 0
    imported_bytes: int = 0
    verified: int = 0
    verified_bytes: int = 0
    corrupt: int = 0
    stale: int = 0
    unverifiable: int = 0
    removed: int = 0
    skipped: int = 0
    errors: int = 0
    checked_dirs: int = 0   # --prune-dirs/-all: directories whose check completed
    pruned_dirs: int = 0    # --prune-dirs/-all: directories found gone
    pruned_files: int = 0   # --prune-dirs/-all: file rows deleted (or would-delete)
    checked_files: int = 0  # --prune-all: files individually checked


class _Reporter:
    """Honors -q/-qq (quiet count) and -v/-vv (verbose count)."""

    def __init__(self, args) -> None:
        self._quiet = args.quiet
        self._verbose = args.verbose

    def info(self, msg: str) -> None:        # normal output, unconditional label
        if self._quiet == 0:
            print(msg)

    def announce(self, path: str, verbose_msg: str, prefix: str = "") -> None:
        """A routine per-file announcement (CLAUDE.md "Status lines").

        Bare path without -v; the full action/reason form with -v. Unlike
        info(), the caller's message is only shown at -v -- by default this
        prints nothing but the path. ``prefix`` is --prescan's nnn/mmm and
        bytes-so-far/total counter, prepended ahead of either form.
        """
        if self._quiet == 0:
            print(prefix + (verbose_msg if self._verbose >= 1 else path))

    def detail(self, msg: str) -> None:      # only with -v
        if self._verbose >= 1 and self._quiet == 0:
            print(msg)

    def error(self, msg: str) -> None:       # stderr; -qq suppresses
        if self._quiet < 2:
            print(msg, file=sys.stderr)


def _stat_data(st: os.stat_result) -> StatData:
    """Build a StatData from an os.stat_result, handling platform differences."""
    birthtime = None
    if hasattr(st, "st_birthtime"):  # macOS
        birthtime = schema.iso_utc_ns(int(st.st_birthtime * 1_000_000_000))
    return StatData(
        size=st.st_size,
        mode=st.st_mode,
        uid=st.st_uid,
        gid=st.st_gid,
        nlink=st.st_nlink,
        dev=st.st_dev,
        ctime=schema.iso_utc_ns(st.st_ctime_ns),
        atime=schema.iso_utc_ns(st.st_atime_ns),
        birthtime=birthtime,
    )


def _read_meta(path: str) -> dict | None:
    """Return the parsed sumtag xattr, or None if absent/unreadable/malformed."""
    raw = xattr.get(path, schema.XATTR_NAME)
    if raw is None:
        return None
    try:
        return schema.loads(raw)
    except (ValueError, KeyError):
        return None


def run(args) -> int:
    """Entry point from the CLI; dispatches to verify, import, or stamp.

    Catches Ctrl-C (KeyboardInterrupt) so an interrupted run ends with the
    same summary a completed run prints, not a Python traceback, and exits
    EXIT_INTERRUPTED (130, the shell convention).
    """
    rep = _Reporter(args)
    stats = RunStats()
    interrupted = False
    try:
        code = _dispatch(args, rep, stats)
    except KeyboardInterrupt:
        interrupted = True
        code = EXIT_INTERRUPTED
        if args.progress and sys.stderr.isatty():
            sys.stderr.write("\r\033[K")  # clear any live progress bar
            sys.stderr.flush()
    _print_summary(args, rep, stats, interrupted)
    return code


def _dispatch(args, rep: _Reporter, stats: RunStats) -> int:
    roots = args.directories

    if args.verify:
        prescan = _prescan_verify(roots, args, rep) if args.prescan else None
        return _verify(roots, args, rep, stats, prescan)

    if args.remove:
        return _remove(roots, args, rep, stats)

    if args.prune_dirs or args.prune_all:
        return _prune_dirs(roots, args, rep, stats,
                           prune_files=args.prune_all)

    # --db-prescan's read-only fetch runs before the store is opened, so a
    # missing or non-matching summary errors out before any side effect at
    # all -- not even creating the database file (CLAUDE.md "--db-prescan":
    # a hard error before any work begins, never a silent fallback to the
    # filesystem walk this flag exists to avoid).
    prescan = None
    if args.db_prescan:
        try:
            summary = store_mod.read_prescan_summary(args.database)
        except NotImplementedError as e:
            rep.error(f"sumtag: {e}")
            return EXIT_ERRORS
        if summary is None:
            rep.error(f"sumtag: {args.database}: no stored prescan totals; "
                      f"run --prescan --database first")
            return EXIT_ERRORS
        mismatch = _summary_mismatch(summary, args)
        if mismatch is not None:
            rep.error(f"sumtag: {args.database}: stored prescan totals do not "
                      f"match this run ({mismatch}); run --prescan --database "
                      f"to refresh them")
            return EXIT_ERRORS
        prescan = (summary.file_count, summary.total_bytes)
        rep.info(f"using stored prescan totals: {summary.file_count} files, "
                 f"{progress_mod.human_size(summary.total_bytes, args.si)}, "
                 f"from {summary.created_at}")

    # A database is opened only when there is something to write to it: never
    # under -n, so --dry-run has no side effect at all (not even creating the
    # file). --import always carries a database (enforced by the CLI).
    store = None
    if args.database and not args.dry_run:
        try:
            store = store_mod.open_store(args.database)
        except NotImplementedError as e:
            rep.error(f"sumtag: {e}")
            return EXIT_ERRORS

    if args.prescan:
        prescan = _prescan_stamp(roots, args, rep)
        if store is not None:
            store.save_prescan_summary(store_mod.PrescanSummary(
                file_count=prescan[0], total_bytes=prescan[1],
                roots=_normalized_roots(roots),
                sum_mode=bool(args.sum), force=bool(args.force),
                exclude=list(args.exclude), no_ignore=bool(args.no_ignore),
                created_at=schema.now_iso()))

    try:
        return _stamp(roots, args, rep, stats, store, prescan)
    finally:
        if store is not None:
            store.close()


def _normalized_roots(roots) -> list[str]:
    """Scan roots as a sorted, deduplicated list of absolute paths -- the
    stored/compared form, so /data vs /data/ vs a relative spelling of the
    same directory never reads as a roots mismatch."""
    return sorted({os.path.abspath(r) for r in roots})


def _summary_mismatch(summary, args) -> str | None:
    """Why the stored prescan summary doesn't answer this run's question,
    or None if it matches. Every field that changes which files get counted
    participates (CLAUDE.md "--db-prescan": match-or-error, full context)."""
    if _normalized_roots(summary.roots) != _normalized_roots(args.directories):
        return "different scan roots"
    if summary.sum_mode != bool(args.sum):
        return "different action"
    if summary.force != bool(args.force):
        return "--force differs"
    if sorted(summary.exclude) != sorted(args.exclude):
        return "--exclude patterns differ"
    if summary.no_ignore != bool(args.no_ignore):
        return "--no-ignore differs"
    return None


def _print_summary(args, rep: _Reporter, stats: RunStats, interrupted: bool) -> None:
    """The end-of-run summary (CLAUDE.md "Run summary").

    Printed after every run -- completed or Ctrl-C'd alike -- on the normal
    output channel, so -q suppresses it. Lines are (label, value) pairs with
    the labels padded to a common column; zero-count deviation lines
    (skipped, errors, CORRUPT, ...) are omitted, but the mode's headline
    count prints even at zero so an interrupted-immediately run still says so.
    """
    hsize = lambda n: progress_mod.human_size(n, args.si)
    plural = lambda count, noun: f"{count} {noun}{'' if count == 1 else 's'}"
    dirs_word = lambda n: f"{n} director{'y' if n == 1 else 'ies'}"
    pairs: list[tuple[str, str]] = []

    if args.prune_dirs or args.prune_all:
        verb = "would prune" if args.dry_run else "pruned"
        pairs.append((verb, f"{dirs_word(stats.pruned_dirs)}, "
                            f"{plural(stats.pruned_files, 'file row')}"))
        checked = dirs_word(stats.checked_dirs)
        if args.prune_all:
            checked += f", {plural(stats.checked_files, 'file')}"
        pairs.append(("checked", checked))
    elif args.remove:
        verb = "would remove" if args.dry_run else "removed"
        pairs.append((verb, plural(stats.removed, "stamp")))
    elif args.verify:
        pairs.append(("verified", f"{plural(stats.verified, 'file')}, {hsize(stats.verified_bytes)}"))
        if stats.corrupt:
            pairs.append(("CORRUPT", plural(stats.corrupt, "file")))
        if stats.stale:
            pairs.append(("stale", plural(stats.stale, "file")))
        if stats.unverifiable:
            pairs.append(("unverifiable", plural(stats.unverifiable, "file")))
    else:
        verb = "would hash" if args.dry_run else "hashed"
        hashed_line = (verb, f"{plural(stats.hashed, 'file')}, {hsize(stats.hashed_bytes)}")
        imported_line = ("imported", f"{plural(stats.imported, 'file')}, {hsize(stats.imported_bytes)}")
        # The mode's natural headline always prints, even at zero; the other
        # line appears only if it counted something (e.g. --force --import
        # hashes; --sum over a part-stamped tree imports nothing).
        if args.sum:
            pairs.append(hashed_line)
            if stats.imported:
                pairs.append(imported_line)
        else:
            if stats.hashed:
                pairs.append(hashed_line)
            pairs.append(imported_line)

    if stats.skipped:
        pairs.append(("skipped", plural(stats.skipped, "file")))
    if stats.errors:
        pairs.append(("errors", str(stats.errors)))
    if args.database:
        pairs.append(("database", args.database))
    pairs.append(("scanned", ", ".join(args.directories)))

    width = max(len(label) for label, _ in pairs) + 1  # +1 for the colon
    if interrupted:
        rep.info("interrupted")
    for label, value in pairs:
        rep.info(f"{label + ':':<{width}} {value}")


def _hash_decision(meta, live: str, args, current_major: int,
                    use_standard_decision: bool) -> tuple[bool, str]:
    """Whether a file gets (re-)hashed, and why -- the one branch _stamp()
    and --prescan's counting pass must agree on bit-for-bit, or the nnn/mmm
    counter shown during the real pass won't line up with what --prescan
    predicted.
    """
    if use_standard_decision:
        return decide.should_rehash(meta, live, args.force, current_major)
    rehash = args.force
    reason = "forced" if args.force else "not computing (--import/--locate only)"
    return rehash, reason


def _prescan_stamp(roots, args, rep: _Reporter) -> tuple[int, int]:
    """Predict how many files the real pass will hash, and their total size,
    before it begins (--prescan). Mirrors _stamp()'s own decision exactly
    (via _hash_decision) so the nnn/mmm and byte counters it prints line up.

    Each directory visited is announced before its files' metadata is read
    (bare path, or ``prescan <path>`` with -v; suppressed by -q like every
    routine announcement).

    Traversal warnings (e.g. a scan root's own @sumtag-ignore) are suppressed
    here -- the real pass reports them -- so nothing gets warned about twice.
    Errors are swallowed the same way: the real pass will hit and report them.
    """
    current_major = schema.major_of(schema.VERSION)
    use_standard_decision = args.sum
    count = 0
    total_bytes = 0
    for path in walk.iter_files(roots, respect_ignore=not args.no_ignore,
                                exclude=args.exclude,
                                on_warn=lambda msg: None,
                                on_dir=lambda d: rep.announce(d, f"prescan {d}")):
        try:
            st = os.stat(path)
            live = schema.iso_utc_ns(st.st_mtime_ns)
            meta = _read_meta(path)
            rehash, _ = _hash_decision(meta, live, args, current_major, use_standard_decision)
            if rehash:
                count += 1
                total_bytes += st.st_size
        except OSError:
            pass
    return count, total_bytes


def _prescan_verify(roots, args, rep: _Reporter) -> tuple[int, int]:
    """Predict how many files --verify will actually read, and their total
    size (--prescan): every file with a usable stored digest -- the same
    set that won't come back UNVERIFIABLE.

    Each directory visited is announced before its files' metadata is read,
    same as _prescan_stamp.
    """
    count = 0
    total_bytes = 0
    for path in walk.iter_files(roots, respect_ignore=not args.no_ignore,
                                exclude=args.exclude,
                                on_warn=lambda msg: None,
                                on_dir=lambda d: rep.announce(d, f"prescan {d}")):
        try:
            meta = _read_meta(path)
            if meta is not None and meta.get("digests"):
                count += 1
                total_bytes += os.stat(path).st_size
        except OSError:
            pass
    return count, total_bytes


def _pct(done: float, total: float) -> str:
    """A progress fraction's whole-number percentage, parenthesized.

    Follows every fraction shown to indicate progress (requested
    2026-07-19). A zero total reads as complete, matching progress.py's
    bar convention. Right-padded to three digits so the token keeps one
    width from (  0%) through (100%) and the columns after it never
    jitter -- the same fixed-width convention as the bar's pct field.
    (--db-prescan drift can push past 100%; that line simply widens,
    harmless in an appended log.)
    """
    # Zero total reads as complete (the progress-bar convention); the >3
    # width keeps (  0%) through (100%) one token wide, and drift past 100
    # simply widens -- harmless in an appended log.
    frac = (done / total) if total else 1.0
    return f"({frac * 100:>3.0f}%)"


def _prescan_prefix(index: int, total_count: int, bytes_so_far: int,
                    total_bytes: int, si: bool) -> str:
    """Render --prescan's "nnn/mmm (pp%)  so-far/total (pp%)  " prefix."""
    width = len(str(total_count)) if total_count else 1
    so_far_h = progress_mod.human_size(bytes_so_far, si)
    total_h = progress_mod.human_size(total_bytes, si)
    return (f"{index:0{width}d}/{total_count} {_pct(index, total_count)}  "
            f"{so_far_h}/{total_h} {_pct(bytes_so_far, total_bytes)}  ")


def _mirror(store, path: str, meta: dict, inode: int,
            stat: StatData | None = None) -> None:
    """Mirror a file's metadata into the database (one row per location)."""
    mount, rel = store_mod.mount_relative(path)
    mp_id = store.ensure_mountpoint(mount)
    for algo, digest in meta["digests"].items():
        store.upsert_file(mp_id, rel, inode, algo, digest, meta["file_mtime"],
                          meta["hashed_at"], meta["run_started_at"], meta["version"],
                          stat=stat)


def _stamp(roots, args, rep: _Reporter, stats: RunStats, store,
           prescan: tuple[int, int] | None = None) -> int:
    """Walk the tree, (re-)hashing and/or mirroring per the given flags.

    Without --database, or with --sum, files are (re-)hashed per the normal
    mtime-based decision (CLAUDE.md "Re-hashing logic"). With only --import
    and/or --locate, hashing is skipped by default -- their job is to
    propagate existing metadata and/or capture stat columns, not compute --
    unless --force overrides that refusal. --locate implies --import: existing
    metadata is mirrored either way; --locate additionally captures stat
    columns and stats files that have no metadata to mirror at all.

    ``prescan``, when --prescan supplied one, is the (count, total_bytes) of
    files this pass is expected to hash; it drives the nnn/mmm and
    bytes-so-far/total counter prepended to each hash announcement.
    """
    run_started = schema.now_iso()
    current_major = schema.major_of(schema.VERSION)
    exit_code = EXIT_OK
    need_stat = args.locate
    use_standard_decision = args.sum
    hashed_index = 0
    bytes_so_far = 0
    prescan_count, prescan_bytes = prescan if prescan is not None else (0, 0)

    for path in walk.iter_files(roots, respect_ignore=not args.no_ignore,
                                exclude=args.exclude,
                                on_warn=rep.error):
        try:
            st = os.stat(path)
            live = schema.iso_utc_ns(st.st_mtime_ns)
            meta = _read_meta(path)

            rehash, reason = _hash_decision(meta, live, args, current_major,
                                            use_standard_decision)

            if rehash:
                prefix = ""
                if prescan is not None:
                    hashed_index += 1
                    prefix = _prescan_prefix(hashed_index, prescan_count,
                                            bytes_so_far, prescan_bytes, args.si)
                if args.dry_run:
                    rep.announce(path, f"would hash {path} ({reason})", prefix)
                else:
                    rep.announce(path, f"hash {path} ({reason})", prefix)
                    ind = progress_mod.make(st.st_size, args.progress, args.si)
                    try:
                        digest = hashing.hash_file(path, progress=ind)
                    finally:
                        # A read that fails mid-hash must still clear the bar,
                        # or the error line prints appended to the stranded bar.
                        if ind is not None:
                            ind.finish()
                    meta = schema.build_meta({schema.ALGO: digest}, live, run_started)
                    xattr.set(path, schema.XATTR_NAME, schema.dumps(meta))
                stats.hashed += 1
                stats.hashed_bytes += st.st_size
                if prescan is not None:
                    bytes_so_far += st.st_size
            elif meta is not None and meta.get("digests"):
                if use_standard_decision:
                    rep.detail(f"skip   {path} ({reason})")
                    stats.skipped += 1
                else:
                    rep.announce(path, f"import {path}")
                    stats.imported += 1
                    stats.imported_bytes += st.st_size
            else:
                rep.announce(path, f"skip (no metadata) {path}")
                stats.skipped += 1

            # Mirror in addition to the xattr: re-hashed and pre-existing
            # metadata both get mirrored, so the database reflects the whole
            # tree, not just changed files. Stat columns are only captured
            # with --locate; a file with neither still gets stat-only if
            # --locate is set (update_stat is a no-op if the row is absent).
            if store is not None and not args.dry_run:
                stat_data = _stat_data(st) if need_stat else None
                if meta is not None and meta.get("digests"):
                    _mirror(store, path, meta, st.st_ino, stat_data)
                elif need_stat:
                    mount, rel = store_mod.mount_relative(path)
                    mp_id = store.ensure_mountpoint(mount)
                    store.update_stat(mp_id, rel, stat_data)
        except OSError as e:
            exit_code = EXIT_ERRORS
            stats.errors += 1
            rep.error(f"sumtag: {path}: {e}")

    return exit_code


def _verify(roots, args, rep: _Reporter, stats: RunStats,
            prescan: tuple[int, int] | None = None) -> int:
    any_corruption = False
    any_error = False
    verify_index = 0
    bytes_so_far = 0
    prescan_count, prescan_bytes = prescan if prescan is not None else (0, 0)

    for path in walk.iter_files(roots, respect_ignore=not args.no_ignore,
                                exclude=args.exclude,
                                on_warn=rep.error):
        try:
            meta = _read_meta(path)
            st = os.stat(path)
            live = schema.iso_utc_ns(st.st_mtime_ns)

            if meta is None or not meta.get("digests"):
                rep.info(f"unverifiable {path}")
                stats.unverifiable += 1
                any_error = True  # the check could not be completed for this file
                continue

            prefix = ""
            if prescan is not None:
                verify_index += 1
                prefix = _prescan_prefix(verify_index, prescan_count,
                                        bytes_so_far, prescan_bytes, args.si)
            rep.announce(path, f"verify {path}", prefix)
            computed = {}
            for algo in meta["digests"]:
                ind = progress_mod.make(st.st_size, args.progress, args.si)
                try:
                    computed[algo] = hashing.hash_file(path, progress=ind)
                finally:
                    # Same rule as the stamp pass: a mid-read failure must
                    # still clear the bar before the error prints.
                    if ind is not None:
                        ind.finish()
            if prescan is not None:
                bytes_so_far += st.st_size
            stats.verified += 1
            stats.verified_bytes += st.st_size
            outcome = decide.classify_verify(meta, live, computed)

            if outcome == decide.CORRUPTION:
                rep.info(f"CORRUPT {path}")
                stats.corrupt += 1
                any_corruption = True
            elif outcome == decide.STALE:
                rep.info(f"stale  {path} (modified since hash; restamp needed)")
                stats.stale += 1
            # else: clean verify -- silence means nothing bad happened
        except OSError as e:
            any_error = True
            stats.errors += 1
            rep.error(f"sumtag: {path}: {e}")

    if any_corruption:
        return EXIT_CORRUPTION
    if any_error:
        return EXIT_ERRORS
    return EXIT_OK


def _prune_dirs(roots, args, rep: _Reporter, stats: RunStats,
                prune_files: bool = False) -> int:
    """Reconcile the database with a changed filesystem (--prune-dirs/-all).

    Checks every directory the database knows under the given roots -- the
    distinct dirnames of its file rows -- and deletes the rows of directories
    that no longer exist. Directory-level only, and deliberately never
    recursive: a deleted directory takes exactly its *resident* file rows
    with it, and its vanished subdirectories are discovered by their own
    checks, since every known directory is in the list. The filesystem is
    never modified, and nothing flows database->filesystem (the
    sink-never-a-source principle is untouched; this is maintenance *of*
    the sink toward filesystem truth).

    With ``prune_files`` (--prune-all), a directory that still exists gets
    its resident file rows checked too, one lstat each, deleting the rows
    of files that no longer exist (or are no longer regular files) -- the
    per-file staleness --prune-dirs deliberately skips. The directory pass
    still runs first, so a vanished directory costs one stat, never one
    per file. Row deletes stay batched and committed per directory.

    The unmounted-drive guard: every scan root must exist (exit 2 before
    the database is even opened -- an absent or elsewhere-mounted drive
    must not read as a mass deletion), and rows are candidates only where
    the recorded mountpoint equals the root's *live* mountpoint, so a
    swapped drive's rows never match. Exit codes are --verify-style:
    0 = nothing stale, 1 = stale directories pruned (or would-prune under
    -n), 2 = errors prevented a complete check.
    """
    bad = False
    for root in roots:
        if not os.path.isdir(root):
            rep.error(f"sumtag: {root}: no such directory "
                      f"(nothing pruned; is the drive mounted?)")
            bad = True
    if bad:
        return EXIT_ERRORS

    # rw (or ro under -n, whose contract is no side effects at all): a
    # missing database is an error, never created -- pruning an empty
    # mirror nobody wrote is meaningless.
    try:
        store = store_mod.open_store(args.database,
                                     mode="ro" if args.dry_run else "rw")
    except (FileNotFoundError, NotImplementedError) as e:
        rep.error(f"sumtag: {e}")
        return EXIT_ERRORS

    exit_code = EXIT_OK
    try:
        # (mount, dir_rel) -> resident file-row count; a dict so overlapping
        # roots never check (or count) the same directory twice.
        candidates: dict[tuple[str, str], int] = {}
        for root in roots:
            mount, rel = store_mod.mount_relative(root)
            found = store.iter_file_dirs(mount, "" if rel == "." else rel)
            if not found:
                rep.error(f"sumtag: {root}: no database rows under this root "
                          f"(mounted at {mount}); nothing to check")
                continue
            for dir_rel, count in found:
                candidates[(mount, dir_rel)] = count

        # --prune-all's progress denominator counts the file checks too (a
        # gone directory's residents tick as done in one lump -- their check
        # was the directory's).
        total = len(candidates)
        unit = "dirs"
        if prune_files:
            total += sum(candidates.values())
            unit = "paths"
        done = 0
        ind = progress_mod.make_count(total, args.progress, unit)
        try:
            for (mount, dir_rel), count in sorted(candidates.items()):
                path = os.path.join(mount, dir_rel) if dir_rel else mount
                try:
                    st = os.stat(path)
                    gone = not S_ISDIR(st.st_mode)
                except (FileNotFoundError, NotADirectoryError):
                    gone = True
                except OSError as e:
                    exit_code = EXIT_ERRORS
                    stats.errors += 1
                    if ind is not None:
                        ind.interrupt()
                    rep.error(f"sumtag: {path}: {e}")
                    continue

                if ind is not None and (gone or args.verbose >= 1):
                    ind.interrupt()
                rows = lambda n: f"{n} file row{'' if n == 1 else 's'}"
                if not gone:
                    rep.detail(f"skip   {path} (exists)")
                    if prune_files:
                        done += _prune_dir_residents(
                            store, mount, dir_rel, args, rep, stats, ind)
                elif args.dry_run:
                    rep.announce(path, f"would prune {path} ({rows(count)})")
                    stats.pruned_dirs += 1
                    stats.pruned_files += count
                    done += count if prune_files else 0
                else:
                    deleted = store.delete_dir_files(mount, dir_rel)
                    rep.announce(path, f"prune  {path} ({rows(deleted)})")
                    stats.pruned_dirs += 1
                    stats.pruned_files += deleted
                    done += count if prune_files else 0
                stats.checked_dirs += 1
                done += 1
                if ind is not None:
                    ind(done)
        finally:
            if ind is not None:
                ind.finish()
    finally:
        store.close()

    if stats.errors:
        exit_code = EXIT_ERRORS  # helper errors escalate the same way
    if exit_code == EXIT_OK and (stats.pruned_dirs or stats.pruned_files):
        return EXIT_CORRUPTION  # 1: something stale found -- gateable
    return exit_code


def _prune_dir_residents(store, mount: str, dir_rel: str, args,
                         rep: _Reporter, stats: RunStats, ind) -> int:
    """--prune-all's file pass for one surviving directory: lstat each
    resident file row, delete the rows of files that no longer exist (or
    are no longer regular files -- a path that turned into a directory or
    symlink is not the stamped file). Deletes are batched into one commit
    per directory, matching delete_dir_files. Returns the rows checked,
    for the caller's progress counter.
    """
    gone_rels: list[str] = []
    checked = 0
    for rel_path in store.iter_dir_file_paths(mount, dir_rel):
        fpath = os.path.join(mount, rel_path)
        try:
            fst = os.lstat(fpath)
            fgone = not S_ISREG(fst.st_mode)
        except (FileNotFoundError, NotADirectoryError):
            fgone = True
        except OSError as e:
            stats.errors += 1
            if ind is not None:
                ind.interrupt()
            rep.error(f"sumtag: {fpath}: {e}")
            continue
        checked += 1
        stats.checked_files += 1
        if not fgone:
            rep.detail(f"skip   {fpath} (exists)")
            continue
        if ind is not None:
            ind.interrupt()
        verb = "would prune" if args.dry_run else "prune "
        rep.announce(fpath, f"{verb} {fpath} (file row)")
        gone_rels.append(rel_path)
    if gone_rels and not args.dry_run:
        stats.pruned_files += store.delete_files(mount, gone_rels)
    else:
        stats.pruned_files += len(gone_rels)
    return checked


def _remove(roots, args, rep: _Reporter, stats: RunStats) -> int:
    """Strip the user.sumtag xattr from every file in the tree (--remove).

    A testing/reset utility, not a data-integrity primitive: there is no
    mtime gating or hashing, just an unconditional delete of whatever
    attribute happens to be present. Composes with --dry-run to preview
    which files carry a stamp without touching anything.
    """
    exit_code = EXIT_OK

    for path in walk.iter_files(roots, respect_ignore=not args.no_ignore,
                                exclude=args.exclude,
                                on_warn=rep.error):
        try:
            if args.dry_run:
                if xattr.get(path, schema.XATTR_NAME) is None:
                    rep.detail(f"skip {path} (no metadata)")
                    stats.skipped += 1
                else:
                    rep.announce(path, f"would remove {path}")
                    stats.removed += 1
            elif xattr.remove(path, schema.XATTR_NAME):
                rep.announce(path, f"remove {path}")
                stats.removed += 1
            else:
                rep.detail(f"skip {path} (no metadata)")
                stats.skipped += 1
        except OSError as e:
            exit_code = EXIT_ERRORS
            stats.errors += 1
            rep.error(f"sumtag: {path}: {e}")

    return exit_code
