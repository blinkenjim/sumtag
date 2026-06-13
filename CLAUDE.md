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

Hash values live in a nested `digests` object keyed by algorithm name (`{ "xxh3": "<hex>" }`), **not** as top-level keys. Today only `xxh3` is computed, so the map has one entry; the nested shape exists so future versions can support alternate digests (e.g. `md5`) with no format migration.

The `xxh3` value is the **64-bit** XXH3 variant (`xxhash.xxh3_64_hexdigest`), a 16-character lowercase-hex string. (Pinned 2026-06-13. 64-bit is sufficient for per-file corruption detection — intent #1 compares a file to its own prior hash — and keeps the xattr compact; the higher collision odds of 64-bit only bear on cross-corpus dedup, intent #2, which is left to higher-level tooling.)

Rationale — this is the cheapest moment to fix the on-disk shape, since no files have been stamped yet, and the xattr is the source of truth that travels into static archives (expensive to migrate later):

- **Additive, not breaking.** Adding a digest later is `{ "xxh3": "...", "md5": "..." }`. Old readers still find `xxh3`; it does not force a major-version bump or a re-hash of files that don't need the new algorithm.
- **Generic iteration.** Readers and the database walk `digests` without a hardcoded list of which top-level keys are algorithms.
- **One shape for one or many.** Selecting a single algorithm and carrying several at once are the same format.

All digests present in the map are assumed computed at the single top-level `file_mtime`. If a future version adds a digest to a file that already has one, restamp them together so they stay consistent. If per-digest timing ever becomes necessary, a map value can grow from a bare hex string into a small object — covered by the `version` major-bump escape hatch. (Future work; not now.)

## Re-hashing logic

A file is (re-)hashed when any of the following are true:

- The `--force` flag was given.
- The `user.sumtag` xattr is absent or unreadable.
- The `file_mtime` in the xattr is older than the file's current mtime.
- The `version` in the xattr has a lower major version number than the current software (semver major bump = re-hash by default).
- A requested digest algorithm is missing from the `digests` map. (Future: when alternate algorithms are selectable, asking for one a file lacks computes *that* algorithm; existing digests are untouched. Today only `xxh3` is requested.)

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
- **A marker on an explicit scan root is honored, with a warning.** If a path passed on the command line (or cwd, when no paths are given) contains a top-level `@sumtag-ignore`, sumtag skips it like any other exempted directory but emits a warning to stderr, since silently doing nothing for a run the user explicitly requested would be confusing.

## Database storage

By default, sumtag stores metadata only in the per-file xattr. The optional `--database` flag adds a database as a second sink: each file's metadata is mirrored into the database **in addition to** (not instead of) the xattr. The xattr remains the source of truth that travels with the file; the database is a detached, queryable mirror.

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

The mount point is found by walking up from the file with `os.path.ismount()` (stdlib, cross-platform, no new dependency). The mount point itself is recorded separately, so the absolute path can be reconstructed as `mount_point + rel_path`.

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
  algo           TEXT NOT NULL,      -- digest algorithm, e.g. 'xxh3'
  digest         TEXT NOT NULL,      -- the hash value
  file_mtime     TEXT NOT NULL,
  hashed_at      TEXT NOT NULL,
  run_started_at TEXT NOT NULL,
  version        TEXT NOT NULL,
  UNIQUE (mountpoint_id, rel_path)   -- identity: one row per file location
);

CREATE INDEX idx_digest ON files(digest);
```

- The **mountpoints** table normalizes the mount point, which repeats across many files; rows in `files` reference it by integer id rather than storing the full path over and over.
- A file's **identity** is its location, `(mountpoint_id, rel_path)` — a given path on a given filesystem is exactly one file. This is the UPSERT target: re-scanning a file updates its row in place (`INSERT ... ON CONFLICT DO UPDATE`).
- The digest column is **generically named** (`algo` + `digest`, not `xxh3`) so the same schema holds whatever algorithm the xattr carries — the future-digest decision recorded under the xattr schema, applied to the mirror. Dedup queries group by `(algo, digest)`.
- `digest` is an **index, not a unique key**. Duplicate detection depends on multiple files sharing a hash, so the hash *must* be allowed to repeat. The index makes grouping/sorting by hash fast (`SELECT algo, digest, COUNT(*) FROM files GROUP BY algo, digest HAVING COUNT(*) > 1`).
- Both tables rely on SQLite's implicit `rowid` as their physical key; no primary key on `files` is needed beyond the `UNIQUE` constraint and the `digest` index.
- One row holds one digest per file location. If a future version stores **multiple** digests per file simultaneously, this moves to a child `digests` table keyed by file. That migration is deferred and acceptably cheap: the database is a rebuildable mirror of the xattrs, unlike the xattr format itself.

Optional future refinement: a `runs` table keyed on `run_started_at`, referenced from `files`, to normalize the repeated run timestamp and support "how many sessions stamped this corpus" analysis. Not built initially — a single denormalized timestamp column is fine to start.

Out of scope (future, higher-level tooling): the database accumulates rows for files that have since been deleted or moved. Pruning stale rows is a separate concern, consistent with sumtag's single-purpose scope.

### `--import` mode

Reading metadata, computing a checksum, and writing it are separable steps. The `--import` flag means **never read file contents or compute a checksum**; only propagate metadata that already exists in a file's xattr into the database. Files lacking a usable xattr are skipped and reported.

`--import` **requires `--database`** — its sole job is to feed the database from existing xattrs, so it is meaningless without a database to feed (error if given alone). It traverses the tree and imports existing xattr metadata without re-reading file contents — e.g. populating a database from an archive that was already hashed on a previous run. (The name is purpose-first: the older mechanism-named `--no-hash` was retired in favor of `--import`.)

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

**Conflicts:** `--verify` + `--database` is an error — on a mismatch there is no non-arbitrary answer to *which* digest to store (the trusted stored one, or the freshly computed one under suspicion); the ambiguity is the proof they should not combine. `--verify` + `--force` is an error (force writes; verify must not). `--verify` + `--import` is an error (import refuses to compute; verify must compute). `--verify` + `-n` is a redundant no-op (verify is already side-effect-free) and is allowed.

## Future work (designed-for, not built)

These are deliberately deferred; the formats above are shaped now so adding them later is additive, not a migration.

- **Alternate digest algorithms** (e.g. `md5`) — selectable via a future `--digest` flag (default `xxh3`). Stored in the `digests` map; DB columns are already generic (`algo`/`digest`).
- **Network database backends** — MySQL/MariaDB and Postgres via `--database=scheme://…`. The value grammar and `open_store()`/`Store` seam are fixed now; only SQLite is implemented.
- **`runs` table** — normalize the repeated `run_started_at` (see Database storage).
- **Stale-row pruning**, **duplicate detection**, and **richer audit tooling** (aggregate reporting, scheduled scrubs, quarantine, repair-from-replica) — higher-level tooling, outside sumtag's single-purpose scope. Note: basic single-pass verification *is* built in (`--verify`); what stays out is everything that aggregates or acts on the results.

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
- **End users:** `pipx install sumtag` is the recommended path. pipx isolates sumtag and its `xxhash` dependency in their own venv and puts `sumtag` on `PATH` globally, without polluting system or project environments — the modern norm for standalone Python CLIs. A plain `pip install sumtag` also works.

## CLI usage

The installed `sumtag` command is the primary form; `python3 -m sumtag` is equivalent and convenient during development.

```bash
# Scan cwd
sumtag

# Scan one or more directories
sumtag /data /backup

# Scan cwd plus another directory
sumtag . /data

# Equivalent, from a source checkout (development)
python3 -m sumtag /data /backup
```

With no directory arguments, cwd is processed. Explicit paths override that default; to include cwd alongside other paths, pass `.` explicitly.

## Flags

| Flag | Short | Type | Description |
|---|---|---|---|
| `--dry-run` | `-n` | bool | Scan and report what would be done; do not write any xattrs. Output is identical to a real run. |
| `--quiet` | `-q` | count | `-q`: suppress normal output. `-qq`: also suppress errors (to stderr). |
| `--verbose` | `-v` | count | `-v`: show reason for each decision (including skips). `-vv`: deep internals for debugging. |
| `--progress` | | bool | Show a live within-file progress indicator for large files. User-friendly; distinct from verbose output. |
| `--force` | `-f` | bool | Re-hash every file unconditionally, ignoring any existing xattr metadata. |
| `--database` | | str | Mirror metadata into a database in addition to the xattr. Value is a SQLite file path, or a `scheme://` DSN (`mysql://…`, `postgresql://…`) — only SQLite is implemented; DSNs are reserved for future backends. |
| `--import` | | bool | Never compute checksums; only copy metadata already present in xattrs into the database. Requires `--database`. |
| `--verify` | | bool | Read-only: recompute each file's checksum and compare it to the stored digest, reporting mismatches (corruption). Writes nothing. Exit `0`/`1`/`2` = intact/corruption/errors. |
| `--no-ignore` | | bool | Disregard all `@sumtag-ignore` marker files for this run, processing every directory regardless of markers. |

`-q` and `-v` together are an error. `--force` and `--dry-run` together are an error. `--force` and `--import` together are an error (force demands re-hashing everything; `--import` forbids computing). `--import` requires `--database` (it has nothing to feed otherwise). `--verify` conflicts with `--database`, `--force`, and `--import` (see Verification); `--verify -n` is a redundant no-op and is allowed. `--progress` and `-q` conflict: whichever appears later on the command line wins, with a warning to stderr. `--force` does **not** override `@sumtag-ignore` markers (see Ignore markers); use `--no-ignore` to process exempted directories.

`-q` and `-v` use `action='count'` in argparse, so `-vv` and `-v -v` are equivalent.

Short forms are deliberately limited to the four frequently-typed flags — `-n`, `-q`, `-v`, `-f`. The rest (`--progress`, `--database`, `--import`, `--verify`, `--no-ignore`) are **long-only by design**: most are rare, deliberate operations where spelling out the name is a feature, not friction. (`--verify` would also collide awkwardly with `-v`/verbose.) This is a settled choice, not an oversight.
