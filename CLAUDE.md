# sumtag

A command-line tool that recursively scans a directory and stamps each file with an XXH3 hash and metadata, stored as an extended attribute (xattr) on the file.

The intent of storing this metadata in a file is severalfold:

1. First, and most importantly, it's a means of checking for silent data corruption. Certain filesystems like ZFS can already detect such data corruption, but others cannot. Storing checksum information on each file gives us a makeshift means of checking for silent data corruption. It will be of most utility for archived files -- those that stay static for very long periods of time, such as in backups.
2. Secondly, other means can be made of the checksum metadata; for example, we can locate duplicate files by finding those with the same metadata. These files will be identical regardless of other factors such as filename/extension.

Sumtag on its own does not fulfill the *higher-level* intents, but makes them possible. It does carry one built-in primitive directly serving intent #1: `--verify` recomputes a file's checksum and compares it to the stored one, reporting disagreements (see Verification). That is a single-pass, single-file check — the cheap increment over machinery sumtag already has. Everything richer — finding duplicates, aggregate audit reporting, scheduled scrubs, quarantine, repair-from-replica — is left for more user-friendly apps built on top of the data and the `verify` primitive. In the unix way, this is standard: small programs that do one or just a few things and do them well. Sumtag computes and stores the data, and offers a basic verify; higher-level tools build audits from it.

## What it does

For each file in a directory tree, sumtag:

1. Reads the xattr (if present) and compares the recorded `file_mtime` to the file's current mtime.
2. Skips the file if the recorded mtime matches (already up-to-date).
3. Otherwise, computes the XXH3 hash of the file's contents and writes the xattr.

Traversal order is deterministic (added 2026-07-03 so output can be followed against an `ls` listing or Finder window): within each directory, files are processed in ascending alphabetical order, then subdirectories are recursed into in the same order. This applies to every mode — the same walker drives stamping, `--verify`, `--remove`, and `--prescan`'s counting pass, so the prescan and the real pass agree on order.

## Language & dependencies

- **Python 3.x** — standard library preferred throughout.
- **`xxhash`** (third-party, required) — XXH3 is not in the standard library.
- **xattr access**:
  - Linux: `os.setxattr` / `os.getxattr` / `os.listxattr` (stdlib, Python 3.3+).
  - macOS: `ctypes` bindings to `libSystem` (avoids adding a second third-party dependency).

## Extended attribute schema

- **Attribute name**: `user.sumtag`
- **Value**: UTF-8 encoded JSON

```json
{
  "version": "0.1.0",
  "digests": { "xxh3": "<lowercase hex string>" },
  "file_mtime": "<ISO 8601 UTC timestamp of the file's mtime at hash time>",
  "hashed_at": "<ISO 8601 UTC timestamp of when the xattr was written>",
  "run_started_at": "<ISO 8601 UTC timestamp of when this sumtag process was invoked>"
}
```

`run_started_at` is set once at process startup and written identically to every xattr in that run. It is passenger data — it plays no role in re-hash decisions.

### Digest container (`digests`)

Hash values live in a nested `digests` object keyed by algorithm name (`{ "xxh3": "<hex>" }`), **not** as a flat `digest` field. Today only `xxh3` is computed, so the map holds exactly one entry; keying by algorithm name (rather than a bare `digest` string) is what lets a future `--digest` flag select `md5` instead — `{ "md5": "<hex>" }` — with no change to the shape of the value itself.

The `xxh3` value is the **64-bit** XXH3 variant (`xxhash.xxh3_64_hexdigest`), a 16-character lowercase-hex string. (Pinned 2026-06-13. 64-bit is sufficient for per-file corruption detection — intent #1 compares a file to its own prior hash — and keeps the xattr compact; the higher collision odds of 64-bit only bear on cross-corpus dedup, intent #2, which is left to higher-level tooling.)

**The map holds exactly one entry, ever — never more than one algorithm at a time (decided 2026-07-02).** An earlier idea was to let the map accumulate additively (`{ "xxh3": "...", "md5": "..." }`), so that switching the active algorithm would only ever *add* coverage, never disturb what was already there. That was rejected for two reasons:

- **The shared `file_mtime` only makes sense for one digest.** Every entry in the map is assumed computed at the single top-level `file_mtime`. Keeping two algorithms "in sync" would mean every future re-hash has to recompute *every* previously-added algorithm, not just the current one — or else that shared-timestamp invariant silently breaks the moment only one gets refreshed. Per-digest timestamps were considered and rejected as unneeded complexity, not deferred as future work — so the shared timestamp holds only because there is exactly one digest.
- **It would outrun the database mirror.** The `files` table is already one-digest-per-file-location (see Database storage's Schema section); a multi-algorithm xattr would have nothing consistent to mirror into without a child-`digests`-table migration that isn't planned (see that section).

So instead: whichever algorithm is currently active computes the file's digest and **replaces** the map's single entry — it never merges alongside a prior algorithm's entry. Re-hashing logic below covers when that replacement happens. The map shape itself is unaffected by this policy — it was always generic enough to hold one algorithm today and a different one tomorrow:

- **No format migration for a new algorithm.** A future `--digest md5` run produces `{ "md5": "<hex>" }` instead of `{ "xxh3": "<hex>" }` — same shape, different key, no version bump.
- **Generic iteration.** Readers and the database walk `digests` without hardcoding which key is the algorithm; `--verify` in particular just recomputes and compares whichever key happens to be present.

## Re-hashing logic

A file is (re-)hashed when any of the following are true:

- The `--force` flag was given.
- The `user.sumtag` xattr is absent or unreadable.
- The `file_mtime` in the xattr is older than the file's current mtime.
- The `version` in the xattr has a lower major version number than the current software (semver major bump = re-hash by default).

Freshness is **algorithm-agnostic**: a file with *any* current digest — regardless of which algorithm produced it — counts as up to date. Switching which algorithm is currently active (a future `--digest` flag; today there is only `xxh3`) never by itself triggers a re-hash. This matters at scale: someone with terabytes already stamped under one algorithm shouldn't have every file re-read from disk just because a newer algorithm became the default. When a re-hash *does* happen for one of the reasons above, it computes under whichever algorithm is currently active and replaces the file's single stored digest (see "Digest container" above) rather than adding a second algorithm alongside the first. To deliberately re-stamp an entire archive under a new algorithm without waiting for files to change naturally, use `--force`.

The major-version rule may have exceptions: if a future major release determines that existing xattr metadata produced by an older major version is still valid (e.g., the hash algorithm and schema are unchanged), it may explicitly whitelist those older major versions and skip re-hashing. This is a case-by-case decision made at the time of each major release.

## Ignore markers

A directory can be exempted from sumtag by placing a marker file named **`@sumtag-ignore`** inside it. When the traversal encounters this file in a directory, it **prunes that directory and its entire subtree** — sumtag does not descend, so nothing beneath it is read, hashed, stamped, or mirrored to the database.

- **The `@` prefix is intentional**: it sorts the marker to (or near) the top of an alphabetical listing, making exempted directories easy to spot. `@` is also not a shell metacharacter, so `touch @sumtag-ignore` needs no quoting (unlike `!`, which would trigger history expansion).
- **Presence is the entire signal.** The file's contents are ignored. The interior is reserved for a future optional reason/comment line (e.g. `# excluded: vendor blob`); reading it is not implemented today and adding it later is additive.
- **The marker file itself is never hashed or stamped.**
- **The pruning applies in every mode** — it is a traversal-level exclusion, so it holds regardless of `--database`, `--import`, `--verify`, or `-n`/`--dry-run`.

### Precedence and overrides

- **The marker beats `--force`.** `--force` governs *how* re-hash decisions are made for files that get visited; the marker governs *whether a subtree is visited at all*. A directory deliberately fenced off must not be clobbered just because `-f` was passed. The marker wins.
- **`--no-ignore` is the intended escape hatch.** It disregards all `@sumtag-ignore` markers for the run, processing everything. This is how you override exemptions — not by weakening `--force`.
- **A marker on an explicit scan root is honored, with a warning.** If a path passed on the command line contains a top-level `@sumtag-ignore`, sumtag skips it like any other exempted directory but emits a warning to stderr, since silently doing nothing for a run the user explicitly requested would be confusing.

## Database storage

By default, sumtag stores metadata only in the per-file xattr. The optional `--database` flag names a database as a second sink; metadata is mirrored into it **in addition to** (not instead of) the xattr. The xattr remains the source of truth that travels with the file; the database is a detached, queryable mirror.

`--database` only names *where*; it takes no action by itself. *What* happens to that database is chosen by one or more of three parallel, combinable action flags:

- **`--sum`** — (re-)hash per the normal mtime-based decision (CLAUDE.md "Re-hashing logic") and mirror the result.
- **`--import`** — never compute; only propagate metadata already present in a file's xattr (see `--import` mode below).
- **`--locate`** — stat every file and write the `os.stat()` metadata to the database; implies `--import` (see below).

Each of `--sum`, `--import`, `--locate` **requires `--database`** (nothing to act on otherwise), and `--database` **requires at least one of them** (otherwise there is no action to take — an error, not a silent no-op). They may be combined freely: e.g. `--sum --locate` computes/mirrors and captures stat columns in the same pass. `--sum` and `--import` together is redundant (`--sum` already computes and mirrors, so `--import`'s refusal to compute has nothing left to refuse) but is not an error.

For now the database must be **SQLite**. The storage layer should be written so that other backends could be added later, but no other backend is supported yet.

### `--database` value grammar

The flag takes one string value (argparse accepts both `--database X` and `--database=X`). Its grammar is fixed now even though only SQLite is implemented, so that paths written into scripts today are never reinterpreted later:

- **No scheme** → a SQLite **file path**: `--database=/var/db/cb.sqlite` (relative paths and paths containing `:` are still paths). This is the common case. An explicit `sqlite:///abs/path` is also accepted.
- **`scheme://…`** → a network **DSN**, dispatched by scheme: `--database=mysql://user@host:3306/db`, `--database=postgresql://user@host:5432/db`.

The disambiguation rule: a value matching `^[a-z][a-z0-9+.-]*://` is a DSN; otherwise it is a SQLite file path. The `://` (with slashes) is required — a bare `mysql:host` is treated as a file path, because `:` alone is a legal filename character. MySQL/MariaDB and Postgres are **future work**; today a `scheme://` value is recognized and rejected with "not yet supported."

All values funnel through one resolver — `open_store(value) -> Store` — which today returns only the SQLite backend. The `Store` interface stays narrow (just the operations actually used, e.g. `ensure_mountpoint`, `upsert_file`); it is not grown speculatively for backends that don't exist yet.

### Why the database stores a path (and the xattr doesn't)

The xattr lives physically on the file, so it needs no path — it is self-locating. The database is detached, so each row must record which file it describes.

### Path strategy: mount-relative

Paths are stored **relative to the mount point**, not as absolute paths. Sumtag targets archives and backups — drives that get moved and remounted at different locations (`/mnt/backup` today; `/Volumes/backup` or `/mnt/disk2` tomorrow). A mount-relative path stays stable across remounts, whereas an absolute path goes stale the moment the mount point changes.

The mount point itself is recorded separately, so the absolute path can be reconstructed as `mount_point + rel_path`. How the mount point is found is platform-specific:

- **Linux and other platforms**: walk up from the file with `os.path.ismount()` (stdlib, no new dependency), which detects a mount by the change in `st_dev` between a directory and its parent.
- **macOS**: `os.path.ismount()` is **not reliable** and is not used. Under APFS the read-only System volume (`/`) and the writable Data volume (`/System/Volumes/Data`) are joined by *firmlinks* and share a single `st_dev`, so `ismount()` sees no boundary and walks straight past the real mount up to `/` — recording a file in `~/Development` under mount `/` instead of `/System/Volumes/Data`. Instead sumtag calls **`statfs(2)`** (bound via `ctypes` on libSystem, the same no-second-dependency approach as the xattr layer), whose `f_mntonname` field is the true mount point — exactly what `df(1)` reports. Because a firmlinked path (`/Users/x`) is not lexically beneath its own mount (`/System/Volumes/Data`), `rel_path` is computed by rebasing the whole rooted path under the mount (the Data volume's contents are firmlinked to root), verified with `samefile` so `mount_point + rel_path` always recomposes to the original file.

Known limitation (future work): a mount-relative path is not a *globally* stable filesystem identity — mounting two different filesystems at the same point at different times produces colliding keys. A filesystem UUID would be the truly stable identifier, but obtaining one portably is platform-specific and out of scope for now.

### Schema

```sql
CREATE TABLE mountpoints (
  id   INTEGER PRIMARY KEY,
  path TEXT UNIQUE NOT NULL          -- stored once, referenced by id
);

CREATE TABLE files (
  mountpoint_id  INTEGER NOT NULL REFERENCES mountpoints(id),
  rel_path       TEXT NOT NULL,
  inode          INTEGER NOT NULL,   -- filesystem inode number at stamp time
  algo           TEXT NOT NULL,      -- digest algorithm, e.g. 'md5'
  digest         TEXT NOT NULL,      -- the hash value
  file_mtime     TEXT NOT NULL,
  hashed_at      TEXT NOT NULL,
  run_started_at TEXT NOT NULL,
  version        TEXT NOT NULL,
  -- locate columns (os.stat metadata); NULL until a --locate run populates them
  size           INTEGER,            -- st_size: file size in bytes
  mode           INTEGER,            -- st_mode: permission and type bits
  uid            INTEGER,            -- st_uid: owner user ID
  gid            INTEGER,            -- st_gid: owner group ID
  nlink          INTEGER,            -- st_nlink: hard-link count
  dev            INTEGER,            -- st_dev: device number
  ctime          TEXT,               -- st_ctime: metadata-change time, ISO 8601 UTC
  atime          TEXT,               -- st_atime: last-access time, ISO 8601 UTC
  birthtime      TEXT,               -- st_birthtime: creation time (macOS only)
  UNIQUE (mountpoint_id, rel_path)   -- identity: one row per file location
);

CREATE INDEX idx_digest ON files(digest);
```

- The **mountpoints** table normalizes the mount point, which repeats across many files; rows in `files` reference it by integer id rather than storing the full path over and over.
- A file's **identity** is its location, `(mountpoint_id, rel_path)` — a given path on a given filesystem is exactly one file. This is the UPSERT target: re-scanning a file updates its row in place (`INSERT ... ON CONFLICT DO UPDATE`).
- The digest column is **generically named** (`algo` + `digest`, not `md5`) so the same schema holds whatever algorithm the xattr carries — the future-digest decision recorded under the xattr schema, applied to the mirror. Dedup queries group by `(algo, digest)`. **Known hazard:** grouping by `(algo, digest)` scopes duplicate detection to a single algorithm at a time — two byte-identical files stamped under different algorithms (e.g. one `xxh3`, one `md5`, from before/after a `--digest` switch) will *not* be found as duplicates by this query, since their digests are unrelated numbers even though the underlying bytes match. This is a silent false-negative risk, not a false-positive one. Closing it is higher-level tooling's job (see Future work), not sumtag's — e.g. a deliberate `--force` backfill to unify a corpus under one algorithm before deduping, or dedup tooling that detects a mixed-algorithm candidate set and falls back to a live comparison.
- `digest` is an **index, not a unique key**. Duplicate detection depends on multiple files sharing a hash, so the hash *must* be allowed to repeat. The index makes grouping/sorting by hash fast (`SELECT algo, digest, COUNT(*) FROM files GROUP BY algo, digest HAVING COUNT(*) > 1`).
- `inode` records the filesystem inode number at stamp time. Its primary use is a safety check before any dedup deletion: two files with the same digest but different inodes are distinct copies; two with the same inode are hard links to the same data — deleting one would not free space and could appear to delete the "only" copy. The inode is not indexed; dedup candidates are first narrowed by digest, then the inode check eliminates hard-link false positives.
- The **locate columns** (`size`, `mode`, `uid`, `gid`, `nlink`, `dev`, `ctime`, `atime`, `birthtime`) mirror `os.stat()` output. They are nullable: a row written without `--locate` has them NULL until a future `--locate` run fills them in; a stat-less update uses `COALESCE` in the UPSERT so it never clobbers data already written. `file_mtime` and `inode` are already in the primary columns and are not duplicated here. `birthtime` is macOS-only; it is NULL on Linux and other platforms.
- Both tables rely on SQLite's implicit `rowid` as their physical key; no primary key on `files` is needed beyond the `UNIQUE` constraint and the `digest` index.
- One row holds one digest per file location — this matches the xattr's own one-digest-at-a-time policy (see "Digest container"), so the database never needs to represent more than the xattr does. A child `digests` table keyed by file was considered for a hypothetical future where the xattr accumulates multiple simultaneous algorithms, but that idea was rejected (see "Digest container"), so this migration is not expected to be needed.

Optional future refinement: a `runs` table keyed on `run_started_at`, referenced from `files`, to normalize the repeated run timestamp and support "how many sessions stamped this corpus" analysis. Not built initially — a single denormalized timestamp column is fine to start.

Out of scope (future, higher-level tooling): the database accumulates rows for files that have since been deleted or moved. Pruning stale rows is a separate concern, consistent with sumtag's single-purpose scope.

### `--import` mode

Reading metadata, computing a checksum, and writing it are separable steps. The `--import` flag means **never read file contents or compute a checksum**; only propagate metadata that already exists in a file's xattr into the database. Files lacking a usable xattr are skipped and reported.

`--import` **requires `--database`** — its sole job is to feed the database from existing xattrs, so it is meaningless without a database to feed (error if given alone). It traverses the tree and imports existing xattr metadata without re-reading file contents — e.g. populating a database from an archive that was already hashed on a previous run. (The name is purpose-first: the older mechanism-named `--no-hash` was retired in favor of `--import`.)

`--import`'s refusal to compute is the default, not an absolute rule: combined with `--force` (see Flags), it re-hashes every file and mirrors the result, rather than only propagating what already exists. Combined with `--sum`, the normal mtime-based decision drives computation instead (`--sum`'s presence makes `--import` a no-op, since there is nothing left for it to refuse).

`--locate` **implies** `--import`: a `--locate` run propagates existing xattr metadata exactly as `--import` would, in addition to capturing stat columns — so `--locate` alone does everything `--import` alone would, plus more. Passing both explicitly is redundant but not an error.

`--import` is distinct from `--dry-run` (`-n`). `-n` means *no side effects anywhere* — no xattr writes and no database writes; it only reports. The two compose: `--database db.sqlite --import -n` previews what would be imported without touching the database.

To survey **which files carry usable metadata** without importing or computing anything, run a plain `--dry-run` (`-n`, or `-nv` for per-file reasons): its report distinguishes files that would be hashed (missing/stale metadata) from those that would be skipped (up-to-date), which is the coverage audit.

## Verification (`--verify`)

`--verify` recomputes each file's checksum from its current contents and compares it to the digest stored in the xattr, reporting any disagreement. It is **strictly read-only** — it never writes an xattr or a database row, *especially not on a mismatch*: silently "healing" the stored record would erase the very evidence of corruption. Restamping a changed file is a separate, deliberate run.

The value of `--verify` comes from cross-checking the digest against the stored `file_mtime`, which disambiguates corruption from a normal edit:

| stored mtime vs live mtime | digest | meaning |
|---|---|---|
| **same** | **match** | verified intact |
| **same** | **mismatch** | **silent corruption** — contents changed while mtime did not. The alarm case (intent #1). |
| changed | mismatch | file was legitimately modified; the stamp is merely stale (restamp needed) — not corruption |
| changed | match | touched but content identical — fine |

Without the mtime gate, every normally-edited file would read as a mismatch; with it, `--verify` isolates the genuinely alarming case (bits changed, mtime didn't) that a non-checksumming filesystem cannot catch on its own.

- A file with **no usable xattr** is reported as **unverifiable** (distinct from a mismatch — there is nothing to verify against), never as corruption.
- **Multiple digests (future):** every algorithm present in the `digests` map is recomputed and compared; generic iteration, no special-casing.
- `--verify` reads full file contents (like a full re-hash), so it is I/O-heavy and composes with `--progress` for large files.

**Exit codes** (so a backup/cron job can gate on the result):

| code | meaning |
|---|---|
| `0` | all checked files verified intact |
| `1` | one or more mismatches (corruption) found |
| `2` | unreadable files or other errors prevented a complete check |

**Conflicts:** `--verify` + `--database` (and so also `--sum`, `--import`, `--locate`, which all require `--database`) is an error — on a mismatch there is no non-arbitrary answer to *which* digest to store (the trusted stored one, or the freshly computed one under suspicion); the ambiguity is the proof they should not combine, and all three actions write to the database. `--verify` + `--force` is an error (force writes; verify must not). `--verify` + `-n` is a redundant no-op (verify is already side-effect-free) and is allowed.

## Removing stamps (`--remove`)

`--remove` strips the `user.sumtag` xattr from every file in the tree. It exists as a **testing/reset utility** — a fast way to return a scratch corpus to its unstamped state between test runs — not as a data-integrity primitive; it carries none of `--verify`'s ceremony because it isn't inspecting anything, just deleting an attribute.

There is no mtime comparison and nothing is computed: a file either has a `user.sumtag` xattr (deleted) or doesn't (silently left alone, reported as a skip gated behind `-v`, same as any other skip). The per-file announcement follows the same bare-path/`-v` rule as every other announcement (see Status lines): `remove <path>` with `-v`, bare path without it, `would remove <path>` under `--dry-run`.

**Conflicts:** `--remove` + `--database` (and so also `--sum`, `--import`, `--locate`) is an error — `--remove` only ever touches the xattr, never the database, so there is nothing for a database flag to do. `--remove` + `--verify` is an error (one reads and compares, the other deletes; they cannot both be the run's mode). `--remove` + `--force` is an error — `--force` overrides a re-hash *decision*, and `--remove` has no decision to override, it always removes whatever is present. `--remove` + `-n` is allowed and is the intended way to preview what would be removed before doing it.

## Future work (designed-for, not built)

These are deliberately deferred; the formats above are shaped now so adding them later is additive, not a migration.

- **Alternate digest algorithms** (e.g. `md5`) — selectable via a future `--digest` flag (default `xxh3`). Stored in the `digests` map (one entry at a time, replaced on re-hash — see "Digest container" and "Re-hashing logic"); DB columns are already generic (`algo`/`digest`). Switching the active algorithm never forces a re-hash of already-current files by itself (freshness is algorithm-agnostic); use `--force` to deliberately re-stamp an archive under a new algorithm.
- **Network database backends** — MySQL/MariaDB and Postgres via `--database=scheme://…`. The value grammar and `open_store()`/`Store` seam are fixed now; only SQLite is implemented.
- **`runs` table** — normalize the repeated `run_started_at` (see Database storage).
- **Stale-row pruning**, **duplicate detection**, and **richer audit tooling** (aggregate reporting, scheduled scrubs, quarantine, repair-from-replica) — higher-level tooling, outside sumtag's single-purpose scope. Note: basic single-pass verification *is* built in (`--verify`); what stays out is everything that aggregates or acts on the results. Whatever builds duplicate detection on top of the database should account for the mixed-algorithm hazard noted in Database storage's Schema section — e.g. by scanning the candidate file sets for more than one `algo` value before comparing, warning about the apples-to-oranges risk, and confirming before proceeding (with a non-interactive override for scripted use, since this guidance is for a separate tool and doesn't bind sumtag's own no-prompts CLI).

## Platform targets

macOS and Linux. The xattr read/write layer must abstract over the two platform APIs; the rest of the code is platform-neutral.

## Timestamp precision

`file_mtime`, `hashed_at`, and `run_started_at` are stored as ISO 8601 UTC timestamps with **microsecond precision** (6 decimal places, e.g. `2026-06-03T10:00:00.123456Z`). Mtime comparisons truncate both the stored and live values to the same precision before comparing, so the comparison is always symmetric.

## Installation & invocation

Sumtag is a real installable package, not a loose script. Once installed it is invoked as a bare command — `sumtag …` — exactly like `cp`, `mv`, or `tar`; the user never types `python` or knows it is written in Python.

### Package layout

```
sumtag/
  __init__.py
  __main__.py     # enables `python3 -m sumtag`
  cli.py          # defines main()
pyproject.toml
```

### Entry point

Packaging is **setuptools** via `pyproject.toml`, declaring a `console_scripts` entry point. On install, pip generates a launcher named `sumtag` on `PATH` (with a shebang pointing at the install environment's interpreter):

```toml
[build-system]
requires = ["setuptools>=61"]
build-backend = "setuptools.build_meta"

[project]
name = "sumtag"
version = "0.1.0"
dependencies = ["xxhash"]

[project.scripts]
sumtag = "sumtag.cli:main"
```

**Both invocation paths funnel through the same `sumtag.cli:main`** — there is no behavioral drift between development and installed use:

- `python3 -m sumtag …` — development; works from a source checkout. `__main__.py` does `sys.exit(main())`.
- `sumtag …` — the installed command, generated by the entry point.

### `main()` contract

`main()` takes no required arguments (it reads `sys.argv`), parses, and **returns an `int` exit code** (it must `return` the code, not `print` and fall off the end). The `console_scripts` launcher uses that return value as the process exit code, so the `0`/`1`/`2` codes defined for `--verify` propagate identically whether invoked as `sumtag` or `python3 -m sumtag` — a cron job gating on `sumtag --verify` behaves the same either way.

### Installing

- **Development:** `pip install -e .` — editable install; the `sumtag` command is available immediately while source stays editable in place.
- **Development environment for this repo specifically:** a venv is machine-specific — interpreter symlinks and compiled wheels built on one OS don't work on another — so each checkout keeps its own `.venv`, which `.gitignore` keeps out of version control. Run `scripts/setup-venv.sh` once per machine to build it: it creates `.venv` with the local `python3` (with a fallback for Debian/Ubuntu systems that lack `ensurepip`), upgrades pip, and does the editable install. Rerun it any time `.venv` is missing or broken.
- **End users:** `pipx install sumtag` is the recommended path. pipx isolates sumtag and its `xxhash` dependency in their own venv and puts `sumtag` on `PATH` globally, without polluting system or project environments — the modern norm for standalone Python CLIs. A plain `pip install sumtag` also works.

## CLI usage

The installed `sumtag` command is the primary form; `python3 -m sumtag` is equivalent and convenient during development.

```bash
# Scan the current directory
sumtag .

# Scan one or more directories
sumtag /data /backup

# Scan the current directory plus another directory
sumtag . /data

# Equivalent, from a source checkout (development)
python3 -m sumtag /data /backup
```

At least one directory argument is required; there is no cwd default (decided 2026-07-03). The rule: any operation that scans a directory requires an explicit directory argument — this guards against firing a recursive operation (a bare `sumtag --remove` or `sumtag --sum`) at whatever directory you happen to be in by omission. Every current mode scans, so the rule applies to all of them; to scan the current directory, pass `.` explicitly.

## Flags

| Flag | Short | Type | Description |
|---|---|---|---|
| `--dry-run` | `-n` | bool | Scan and report what would be done; do not write any xattrs. Output is identical to a real run. |
| `--quiet` | `-q` | count | `-q`: suppress normal output. `-qq`: also suppress errors (to stderr). |
| `--verbose` | `-v` | count | `-v`: show reason for each decision (including skips). `-vv`: deep internals for debugging. |
| `--progress` | | bool | Show a live within-file progress indicator, triggered once a single file's checksum has run for more than 2 seconds. User-friendly; distinct from verbose output. |
| `--force` | `-f` | bool | Re-hash every file unconditionally, ignoring any existing xattr metadata. |
| `--database` | | str | Names the database to act on (SQLite path, or a `scheme://` DSN — only SQLite is implemented; DSNs are reserved for future backends). Takes no action by itself; requires at least one of `--sum`, `--import`, `--locate`. |
| `--sum` | | bool | (Re-)hash per the normal mtime-based decision and mirror the result into `--database`. Requires `--database`. |
| `--import` | | bool | Never compute checksums; only copy metadata already present in xattrs into the database. Requires `--database`. |
| `--verify` | | bool | Read-only: recompute each file's checksum and compare it to the stored digest, reporting mismatches (corruption). Writes nothing. Exit `0`/`1`/`2` = intact/corruption/errors. |
| `--no-ignore` | | bool | Disregard all `@sumtag-ignore` marker files for this run, processing every directory regardless of markers. |
| `--locate` | | bool | Stat every file visited and write the `os.stat()` metadata to the database, regardless of whether xattr work was done; implies `--import`. Requires `--database`. Useful as a periodic filesystem inventory pass (analogous to `updatedb`). |
| `--si` | | bool | Display sizes and rates in `--progress` using decimal (SI, powers-of-1000: `kB`/`MB`/`GB`) units instead of the default binary (powers-of-1024: `KiB`/`MiB`/`GiB`) units. |
| `--remove` | | bool | Remove the `user.sumtag` xattr from every file in the tree (see Removing stamps). A testing/reset utility, not a data-integrity primitive. Composes with `-n` to preview. |
| `--prescan` | | bool | Walk the tree once before the real pass to count the files that will be checksummed and their total size, then prefix each hash/verify announcement with an nnn/mmm file counter and a bytes-so-far/total counter (see `--prescan` below). Cannot be combined with `--remove`. |

`-q` and `-v` together are an error. `--force` and `--dry-run` together are an error. `--sum`, `--import`, and `--locate` are parallel, combinable actions performed on `--database` (see Database storage); each requires `--database`, and `--database` requires at least one of them. `--force` and `--import` together are **allowed**: `--import`'s refusal to compute is a default, not a hard restriction, and `--force` is the flag whose whole job is overriding defaults about what gets (re-)computed — so `--force --import` re-hashes every file and mirrors the result. The same override applies to `--force --locate`, since `--locate` implies `--import`. `--verify` conflicts with `--database` (and so `--sum`, `--import`, `--locate`) and `--force` (see Verification); `--verify -n` is a redundant no-op and is allowed. `--remove` conflicts with `--database` (and so `--sum`, `--import`, `--locate`), `--verify`, and `--force` — for the same reason as `--verify`, it is its own standalone mode, and there is no re-hash decision for `--force` to override; `--remove -n` is allowed and is the way to preview it. `--prescan` conflicts with `--remove` for the same reason: `--remove` never computes anything, so there is nothing for `--prescan` to count. `--progress` and `-q` conflict: whichever appears later on the command line wins, with a warning to stderr. `--force` does **not** override `@sumtag-ignore` markers (see Ignore markers); use `--no-ignore` to process exempted directories.

### Status lines

Sumtag's default (non-quiet) output is an *announcement*, not a completion report: for any file about to be checksummed, a line prints **before** the read begins. **Without `-v`, that line is the bare path and nothing else** — no verb, no reason, just the path, so `sumtag --sum` over a large tree stays clean and skimmable. **With `-v`**, the same announcement expands to the full form — `hash <path> (<reason>)` for a stamp, `verify <path>` for `--verify`, `import <path>` for a propagated import, `would hash <path> (<reason>)` under `--dry-run` — using present/imperative verbs, not past tense (`hash`/`import`, not `hashed`/`imported`). This bare-path/`-v` split applies uniformly to every routine per-file announcement (stamp, dry-run preview, import, and the no-usable-metadata report under `--import`/`--locate`); it does **not** apply to `--progress`, which is unaffected by `-v` and appears regardless, nor to the deviation lines below. The path being on screen before the read begins is also what makes `--progress` legible: it's already there by the time a slow file's live bar appears at the 2-second mark, rather than the bar being the first anyone hears of that file.

With `--prescan`, the hash/`would hash`/`verify` announcement (bare-path or `-v` form alike) additionally gets an nnn/mmm and bytes-so-far/total counter prepended — see `--prescan` below. It does not touch `import`, `skip`, or the no-usable-metadata report; those aren't "the line before summing a file" in the first place.

A clean outcome earns no further line — silence means nothing bad happened. `--verify`'s successful case in particular prints nothing beyond its announcement (no `ok` line); the announcement already was the record. Only a *deviation* from clean earns a second line, and these are **unconditional — shown with their label regardless of `-v`**, since they are alarms, not the routine "why was this touched" detail that `-v` exists to add: `CORRUPT <path>` (mismatch), `stale <path> (modified since hash; restamp needed)` (legitimately edited, not corrupt — surfaced unconditionally, like `rsync` noting a file changed mid-transfer), `unverifiable <path>` (no usable xattr to check against; replaces the announcement outright since no read is even attempted), or an error via the normal error channel (`sumtag: <path>: <error>`; the file is skipped and the run continues).

`skip <path> (<reason>)` — a file already up-to-date, nothing done — stays gated behind `-v` entirely, with no bare-path line either: a repeat run over an already-stamped archive stays completely silent by default, with `-v` available for the full per-file accounting.

### Run summary (and Ctrl-C)

Every run ends with a brief summary block (added 2026-07-11) on the normal output channel — so `-q` suppresses it like any routine output. It is a small set of aligned `label: value` lines:

```
hashed:   42 files, 1.3GiB
database: /var/db/cb.sqlite
scanned:  /backup, /data
```

- The **headline line names what the mode did**, with the file count and cumulative byte size (byte figures honor `--si`): `hashed` for the stamp pass, `would hash` under `--dry-run`, `imported` for an `--import`/`--locate`-only run, `verified` for `--verify`, `removed: N stamps` for `--remove`. The headline always prints, even at zero — a run that did nothing says so. A run that both hashed and imported (e.g. `--force --import`, or `--sum` over a part-stamped tree) shows both lines; the zero one is dropped.
- **Deviation counts print only when nonzero**: `skipped`, `errors`, and `--verify`'s `CORRUPT` / `stale` / `unverifiable` tallies.
- `database:` appears when `--database` was given; `scanned:` always closes the block, listing the scan root(s) as given on the command line.

**Ctrl-C prints the same summary, not a Python traceback.** `KeyboardInterrupt` is caught; the run stops where it is, prints `interrupted` followed by the identical summary block, and exits **130** (128 + SIGINT, the shell convention — distinct from `--verify`'s 0/1/2, so a gating cron job can tell "interrupted" from "corrupt"). The counters only ever count *completed* files — a file whose hash was cut off mid-read is not claimed — so the interrupted summary is an honest statement of how far the run got. Any live `--progress` bar is cleared before the summary prints.

### `--progress` indicator

`--progress` is triggered by *time*, not file size: a modest file on a slow network mount is just as worth watching as a huge one on fast local storage, and a huge file that happens to finish quickly needs no indicator at all. Concretely: once a single file's checksum has been computing for more than 2 seconds, a live line appears on stderr — redrawn in place (`\r`, throttled to a few updates per second) — and is cleared the moment that file's hash completes. A file that finishes under the threshold shows nothing at all.

This is deliberately independent of `-v`/`--verbose`: verbosity is a durable, appended log of *why* each file got the decision it did (fine to redirect into a file), while `--progress` is an ephemeral, redrawn-in-place indicator of *how far through* the current file the run is (meaningless once redirected). Composing them is normal — with both given, the per-file announcement (`hash <path> (reason)` or `verify <path>`) prints first; then, if that file turns out to be slow, the live bar appears and redraws in place until the hash completes.

`--progress` is suppressed outright when stderr is not a terminal (a redirected log or pipe), since carriage-return redraws would just corrupt it rather than show anything useful.

#### Line format

```
{size:>8}  {rate:>10}  [{bar}] {pct:>3}%  {elapsed:>8}  ETA {eta:<7}
```

Example at 80 columns (binary units, the default):

```
 99.3GiB   40.1MiB/s  [========================>   ]  84%   0:05:12  ETA 58s
```

Same file with `--si`:

```
  106.6GB   42.1MB/s  [========================>   ]  84%   0:05:12  ETA 58s
```

Fields, left to right:

- **size** — the file's total size, human-readable: binary (powers-of-1024: `KiB`/`MiB`/`GiB`) by default, decimal (powers-of-1000: `kB`/`MB`/`GB`) with `--si`.
- **rate** — current computed throughput, same unit convention as size, suffixed `/s`.
- **bar** — pv-style: `=` for the filled portion, `>` as the leading edge, spaces for the remainder, enclosed in `[...]`.
- **pct** — percentage complete, outside the bar.
- **elapsed** — time spent hashing the current file, `H:MM:SS`.
- **eta** — literal `ETA` followed by the estimated time remaining, in a compact human duration (`45s`, `5m12s`, `1h05m`) — deliberately distinct from elapsed's clock style, since one is a fact and the other an estimate.

Every field except the bar has a fixed width, so the bar absorbs whatever width remains (28 characters at 80 columns given the widths above) and is the only field whose width varies. This is what lets the line track the terminal: sumtag handles `SIGWINCH` (added 2026-07-08), re-measuring the terminal's width on the next redraw after a resize and sizing the bar to fill it, without touching any other field's layout or triggering jitter in the numbers. The signal handler itself only marks the cached width stale; the actual re-measure happens on the redraw. When the width cannot be determined (or off macOS/Linux, where `SIGWINCH` may not exist), an 80-column budget is the fallback. `SIGWINCH` affects only the `--progress` bar — truncating long per-file announcement lines to the terminal width was considered alongside this and declined (2026-07-08): every other line always prints the full path, unconditionally.

### `--prescan`

On a very large tree, the default output gives no sense of *how far along* a run is — files stream by with no indication of what fraction of the work is done. `--prescan` fixes that by walking the tree once, up front, purely to count: how many files the run will actually checksum, and their total size. The real pass then runs exactly as it always has, except each hash/verify announcement gains a counter prefix:

```
nnn/mmm  bytes-so-far/bytes-total  <the usual announcement>
```

Example (`-v`, mid-run):

```
042/137  118.2MiB/4.2GiB  hash /backup/vault/photo0042.dng (file modified since last hash)
```

Or without `-v` (bare path, prefix still shown):

```
042/137  118.2MiB/4.2GiB  /backup/vault/photo0042.dng
```

- **nnn** is this file's ordinal position among the files being checksummed this run, zero-padded to the width of **mmm** (e.g. `007/137`, not `7/137`) so the column stays aligned as the count climbs.
- **mmm** is the total count `--prescan` found up front.
- **bytes-so-far** is the sum of the sizes of files *already completed* before this announcement (so it reads `0B` on the very first file) — not a live in-file counter like `--progress`; this line prints once per file, before that file's read begins.
- **bytes-total** is the total size `--prescan` found up front.
- Both byte figures are human-readable, honoring `--si` exactly like `--progress`'s size field.

**What counts as "will be checksummed" mirrors whichever mode is running:**

- For the normal hash/stamp pass (no `--database`, or `--sum`/`--import`/`--locate`), it is exactly the set of files the mtime-based re-hash decision (or `--force`) will cause to be hashed — the same files that would otherwise print `hash`/`would hash`. Files that will be skipped, imported without computing, or reported as having no metadata are not counted and do not get a prefix (CLAUDE.md "Status lines" — that line was never "the line before summing a file" to begin with).
- For `--verify`, it is every file with a usable stored digest — the same set that will be read and recomputed, as opposed to reported `unverifiable` outright.
- `--remove` computes nothing, so `--prescan` has nothing to count; the two are a CLI error together (see Flags).

**Cost and a known limitation:** the prescan walk duplicates the traversal and the xattr/stat reads the real pass is about to do anyway — a deliberate trade of one extra cheap (metadata-only, no file content read) pass for a progress indication that would otherwise be impossible to give up front. Because it is a separate pass, its counts are a prediction, not a guarantee: if the tree changes between the prescan and the real pass (a file is added, removed, or its own re-hash decision flips), `nnn`/`mmm` can drift slightly out of sync with what the real pass actually does. This is a display aid, not authoritative accounting — nothing about hashing, stamping, or exit codes depends on it. The prescan pass suppresses the traversal warnings the real pass already prints (e.g. a scan root's own `@sumtag-ignore`), so nothing is warned about twice.

The prescan walk announces each directory it visits, printing the directory's path before any file metadata inside it is read — following the same bare-path/`-v` split as every routine announcement (bare path by default, `prescan <path>` with `-v`) and suppressed by `-q`. This gives the otherwise-silent up-front counting pass its own sign of life on a large tree. Pruned (`@sumtag-ignore`) directories are not announced — they are not visited.

`-q` and `-v` use `action='count'` in argparse, so `-vv` and `-v -v` are equivalent.

Short forms are deliberately limited to the four frequently-typed flags — `-n`, `-q`, `-v`, `-f`. The rest (`--progress`, `--database`, `--sum`, `--import`, `--verify`, `--no-ignore`, `--locate`, `--si`, `--remove`, `--prescan`) are **long-only by design**: most are rare, deliberate operations where spelling out the name is a feature, not friction. (`--verify` would also collide awkwardly with `-v`/verbose.) This is a settled choice, not an oversight.
