# sumtag

A command-line tool that recursively scans a directory and stamps each file with an XXH3 hash and metadata, stored as an extended attribute (xattr) on the file.

The intent of storing this metadata in a file is severalfold:

1. First, and most importantly, it's a means of checking for silent data corruption. Certain filesystems like ZFS can already detect such data corruption, but others cannot. Storing checksum information on each file gives us a makeshift means of checking for silent data corruption. It will be of most utility for archived files -- those that stay static for very long periods of time, such as in backups.
2. Secondly, other means can be made of the checksum metadata; for example, we can locate duplicate files by finding those with the same metadata. These files will be identical regardless of other factors such as filename/extension.

Sumtag on its own does not fulfill the *higher-level* intents, but makes them possible. It does carry one built-in primitive directly serving intent #1: `--verify` recomputes a file's checksum and compares it to the stored one, reporting disagreements (see Verification). That is a single-pass, single-file check — the cheap increment over machinery sumtag already has. Everything richer — finding duplicates, aggregate audit reporting, scheduled scrubs, quarantine, repair-from-replica — is left for more user-friendly apps built on top of the data and the `verify` primitive. In the unix way, this is standard: small programs that do one or just a few things and do them well. Sumtag computes and stores the data, and offers a basic verify; higher-level tools build audits from it.

## Session start: fetch first

Development happens on **two machines**, both syncing through the GitHub remote — there is no other shared state (in particular, Claude's per-machine memory does not sync). Before doing anything else in a session, run `git fetch origin` and compare the local branches against their remote counterparts. If any local branch has diverged from (not merely trails or leads) its remote — or if `origin/main` has commits the current branch's base lacks — report that to the user before starting work, so new work is never built on a stale view of `main`. Added 2026-07-19, after the two machines independently diagnosed and fixed the same `--progress` display bug (see BUGS.md) and the duplicate fixes had to be hand-reconciled in a rebase.

## What it does

For each file in a directory tree, sumtag:

1. Reads the xattr (if present) and compares the recorded `file_mtime` to the file's current mtime.
2. Skips the file if the recorded mtime matches (already up-to-date).
3. Otherwise, computes the XXH3 hash of the file's contents and writes the xattr.

Traversal order is deterministic (added 2026-07-03 so output can be followed against an `ls` listing or Finder window): within each directory, files are processed in ascending **case-insensitive** alphabetical order (casefolded, with the raw name as a tie-break for case-only twins like `README`/`readme`; made case-insensitive 2026-07-21 to match Finder's ordering), then subdirectories are recursed into in the same order. This applies to every mode — the same walker drives stamping, `--verify`, `--remove`, and `--prescan`'s counting pass, so the prescan and the real pass agree on order.

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

## Symbolic links

Symlinks are **not content** (fixed 2026-07-16). The traversal never yields them, in any mode: a symlink is never hashed, stamped, verified, imported, located, removed, or counted by `--prescan` — silently skipped at the walker level, like every traversal-level exclusion (no output even at `-v`). Before the fix, `os.walk` listed symlinks-to-files among a directory's files and sumtag processed them: a *broken* link surfaced as a spurious per-file error (`--remove` reported "file not found" for links whose target was gone), and a *live* link was stamped **through** the link — the xattr landed on the target, possibly outside the scanned tree. Symlinks to directories were already never descended (`os.walk` default). An explicit scan-root argument that is itself a symlink is still honored — naming it is the user's explicit claim — but nothing encountered *during* traversal is followed.

## Command-line exclusion (`--exclude`)

`--exclude PATTERN` skips anything whose **basename** matches the glob `PATTERN` (design settled 2026-07-13). It complements `@sumtag-ignore`: the marker fences off a directory by touching the filesystem; `--exclude` does it per-run from the command line, leaving nothing behind.

- **Glob syntax, basename-only.** `PATTERN` is an `fnmatch`-style glob (`*.vob`, `VIDEO_TS`), matched **case-sensitively** (`fnmatchcase`, so the same name matches the same pattern on macOS and Linux) against the final path component only. There is no relative-path or anchored matching; a pattern containing `/` can never match a basename and therefore excludes nothing.
- **Repeatable.** Give the flag once per pattern; a name matching *any* pattern is excluded.
- **A matching directory is pruned** exactly like a marked one: not descended, nothing beneath it read, hashed, stamped, or mirrored, and the directory is not announced (no prescan line).
- **Traversal-level, every mode.** Exclusion happens in the shared walker, so it holds across stamping, `--verify`, `--remove`, `--prescan`, and the database modes — and `--force` does not override it, for the same reason it doesn't override the marker: `--force` governs re-hash decisions for files that get visited, not what gets visited.
- **Independent of `--no-ignore`.** `--no-ignore` governs markers only; an `--exclude` the user explicitly typed always applies. The silence rule also matches the marker: an excluded file earns no output at all, not even a `-v` skip line.
- **An excluded scan root warns.** A root named on the command line whose own basename matches a pattern is skipped with a warning to stderr (`matches --exclude '...' on scan root; skipping`), mirroring the marker-on-root rule. The prescan pass suppresses this warning like every traversal warning, so it prints once.

## Database storage

By default, sumtag stores metadata only in the per-file xattr. The optional `--database` flag names a database as a second sink; metadata is mirrored into it **in addition to** (not instead of) the xattr. The xattr remains the source of truth that travels with the file; the database is a detached, queryable mirror.

`--database` only names *where*; it takes no action by itself. *What* happens to that database is chosen by one or more of three parallel, combinable action flags:

- **`--sum`** — (re-)hash per the normal mtime-based decision (CLAUDE.md "Re-hashing logic") and mirror the result. (`--sum` is also the plain stamping action without `--database` — see "Actions" under CLI usage; adding `--database` is what makes it mirror.)
- **`--import`** — never compute; only propagate metadata already present in a file's xattr (see `--import` mode below).
- **`--locate`** — stat every file and write the `os.stat()` metadata to the database; implies `--import` (see below).

`--import` and `--locate` **require `--database`** (their sole job is feeding it), and `--database` **requires at least one of `--sum`, `--import`, `--locate`, `--prune-dirs`, `--prune-all`** (otherwise there is no action to take on it — an error, not a silent no-op). `--sum` does *not* require `--database` (changed 2026-07-13 when it became the general stamping action): without one it stamps xattrs only. The three may be combined freely: e.g. `--sum --locate` computes/mirrors and captures stat columns in the same pass. `--sum` and `--import` together is redundant (`--sum` already computes and mirrors, so `--import`'s refusal to compute has nothing left to refuse) but is not an error.

For now the database must be **SQLite**. The storage layer should be written so that other backends could be added later, but no other backend is supported yet.

**The database is a sink, never a source (decided 2026-07-13).** Metadata flows only *toward* the database — computation → xattr → database (`--sum`), or existing xattr → database (`--import`/`--locate`) — never back out. Sumtag never writes an xattr from database contents: every digest stamped into an xattr was freshly computed from the file's bytes in that same run. A restore-from-database feature (re-stamping files whose xattrs were lost by trusting stored digests) is **rejected, not deferred** — the same principle as `--verify`'s refusal to heal: a stored value can be wrong with no evidence trail, while a freshly computed digest cannot lie about what was read. `--db-prescan` respects this by construction: it reads only display totals and never influences what gets summed or stamped.

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

The database accumulates rows for files that have since been deleted or moved. Reconciliation is built in — `--prune-dirs` (added 2026-07-16; see Pruning stale database rows) deletes the rows of directories that no longer exist, and `--prune-all` (added the same day) extends the check to every file row, both pulled in as the same kind of cheap increment over existing machinery that justified `--verify`. Moved-file detection remains future, higher-level tooling, consistent with sumtag's single-purpose scope.

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

**Conflicts:** `--verify` + `--database` is an error — on a mismatch there is no non-arbitrary answer to *which* digest to store (the trusted stored one, or the freshly computed one under suspicion); the ambiguity is the proof they should not combine. `--verify` + `--sum`/`--import`/`--locate` is likewise an error (they are actions that write, and a run has one mode). `--verify` + `--force` is an error (force writes; verify must not). `--verify` + `-n` is a redundant no-op (verify is already side-effect-free) and is allowed.

## Removing stamps (`--remove`)

`--remove` strips the `user.sumtag` xattr from every file in the tree. It exists as a **testing/reset utility** — a fast way to return a scratch corpus to its unstamped state between test runs — not as a data-integrity primitive; it carries none of `--verify`'s ceremony because it isn't inspecting anything, just deleting an attribute.

There is no mtime comparison and nothing is computed: a file either has a `user.sumtag` xattr (deleted) or doesn't (silently left alone, reported as a skip gated behind `-v`, same as any other skip). The per-file announcement follows the same bare-path/`-v` rule as every other announcement (see Status lines): `remove <path>` with `-v`, bare path without it, `would remove <path>` under `--dry-run`.

**Conflicts:** `--remove` + `--database` is an error — `--remove` only ever touches the xattr, never the database, so there is nothing for a database flag to do — and `--remove` + `--sum`/`--import`/`--locate` likewise (one run, one mode). `--remove` + `--verify` is an error (one reads and compares, the other deletes; they cannot both be the run's mode). `--remove` + `--force` is an error — `--force` overrides a re-hash *decision*, and `--remove` has no decision to override, it always removes whatever is present. `--remove` + `-n` is allowed and is the intended way to preview what would be removed before doing it.

## Pruning stale database rows (`--prune-dirs`, `--prune-all`)

The database accumulates rows for files that have since been deleted — most acutely when whole directories are removed. `--prune-dirs` (added 2026-07-16) reconciles it: for each scan root it collects **every directory the database knows** under that root (the distinct dirnames of its file rows — the schema stores no directory table, so directories are derived from the rows themselves), checks each with one `stat`, and deletes the rows of directories that no longer exist. It was pulled into sumtag as the same kind of cheap increment over existing machinery that justified `--verify` — a step toward richer verify-style auditing — while per-file staleness and moved-file detection stay out (see Future work).

**`--prune-all`** (added 2026-07-16, same day, closing `--prune-dirs`'s documented trade) does everything `--prune-dirs` does **and** checks per-file staleness: after a directory is confirmed to still exist, each of its resident file rows gets one `lstat`, and rows whose file no longer exists — or is no longer a *regular* file (a path that turned into a directory or symlink is not the stamped file) — are deleted. The directory pass still runs first, so a vanished directory costs one `stat` total, never one per resident row; the extra cost of `--prune-all` over `--prune-dirs` is one `lstat` per row **in surviving directories only**, which is why the two stay separate flags: on an archive where whole-directory deletion is the norm, `--prune-dirs` is much cheaper and usually enough. Giving both flags together is redundant but not an error (`--prune-all` subsumes `--prune-dirs` — the `--locate`-implies-`--import` idiom). Everything below applies to both flags equally; per-file prune announcements follow the same bare-path/`-v` rule (`prune <path> (file row)` with `-v`), the summary's `checked:` line adds a file count, and `--progress`'s counter unit becomes `paths` (directories + file rows). What `--prune-all` still does **not** do: notice a moved file (pruned at its old location like any deletion), or anything content-based — no hashing, ever; one `lstat` is the entire per-file check.

- **Directory-level, deliberately** (`--prune-dirs`). A deleted directory takes exactly its *resident* file rows with it (dirname equality, never a recursive prefix delete). Recursion is unnecessary by construction: a vanished parent implies vanished children, and every child directory that held files is independently in the check list, so it is discovered by its own check. The trade accepted (decided 2026-07-16): a file deleted from a directory that still exists is not noticed (that is `--prune-all`'s job), and a directory moved elsewhere is pruned at its old location like any other deletion — the intended use is whole-directory deletions, where this is exactly right.
- **The unmounted-drive guard.** An absent drive must never read as a mass deletion. Every scan root must exist — a missing root is exit 2 *before the database is even opened*, with nothing deleted — and candidate rows are only those whose recorded mountpoint equals the root's **live** mountpoint (via the same `statfs`/`mount_relative` machinery as stamping), so a drive mounted somewhere unexpected simply matches no rows. A root that matches no rows at all earns a warning (`no database rows under this root`), not silence. The database itself must already exist: `--prune-dirs` opens it `rw` (or read-only under `-n`), never creating an empty mirror as a byproduct; a missing database is exit 2.
- **The database is still a sink.** Nothing flows database→filesystem; the filesystem is never modified (no xattr is touched). This is maintenance *of* the sink toward filesystem truth — the sink-never-a-source principle is untouched.
- **Output and summary.** Per-directory announcements follow the bare-path/`-v` rule: bare path for a pruned directory by default, `prune <path> (N file rows)` with `-v`, `would prune …` under `-n`; a directory that still exists is a `-v`-gated `skip <path> (exists)`. The run summary's headline is `pruned:` (or `would prune:`) with directory and file-row counts, plus a `checked:` line with the total examined. Deletes are committed per directory, so an interrupted run keeps the prunes it completed (the Ctrl-C summary stays honest, as everywhere).
- **Progress.** With `--progress`, a live `nnn/mmm dirs` counter (plus bar, percent, elapsed, ETA) redraws on stderr — `nnn` directories checked against the `mmm` known at the start. Same conventions as the within-file indicator: opt-in, appears after 2 seconds, cleared on completion, suppressed when stderr is not a terminal; `nnn` is zero-padded to `mmm`'s width like `--prescan`'s counter.
- **Exit codes are `--verify`-style, by design** (chosen 2026-07-16 — the flag is a step toward `--verify`): `0` = nothing stale (the database already matches), `1` = stale directories found and pruned (or would-prune under `-n`), `2` = errors prevented a complete check. A gating cron job can tell "database changed" from "all quiet".

**Conflicts** (identical for `--prune-dirs` and `--prune-all`): each requires `--database` (it acts only on the database) and requires explicit scan roots like every mode. Each conflicts with `--sum`/`--import`/`--locate`/`--verify`/`--remove` (one run, one mode), with `--force` (no re-hash decision to override), and with `--prescan`/`--db-prescan` (nothing is checksummed; its own counter is built in). `--exclude` and `--no-ignore` are errors with them, not silent no-ops: they govern filesystem traversal, and the prune flags walk the database, not the filesystem. `-n` is allowed with either and is the intended preview; the two together are redundant but allowed.

## Experimental companion programs

Installed commands that ship inside the `sumtag` package (`sumtag/grouper.py`, `sumtag/dedupe.py`, `sumtag/dbmerge.py`) with their own `console_scripts` entry points — `grouper`, `dedupe`, and `dbmerge` on `PATH`, exactly like `sumtag` itself (grouper/dedupe promoted 2026-07-16; they began as repo-root scripts outside the package; dbmerge added 2026-07-19 directly in the package). *First-class to invoke, still experimental in status* — the promotion changed how they are typed, not their maturity. From a source checkout, `python3 -m sumtag.grouper` / `python3 -m sumtag.dedupe` / `python3 -m sumtag.dbmerge` are the development equivalents, same as `python3 -m sumtag`. They are the "higher-level tooling" playground CLAUDE.md keeps pointing at: they consume the sumtag database and never compute a hash (grouper additionally never touches xattrs or file contents; dedupe reads xattrs for its trust vetoes and deletes files — see its section; dbmerge reads only databases and writes only its target database). Experimental status does not exempt a program from being documented here.

### grouper

`grouper` explores intent #2: it finds **groups of directories whose contents are identical or nearly so** (e.g. two slightly different versions of the same project) in a sumtag SQLite database. It reads sumtag's `files`/`mountpoints` tables and owns its derived tables inside the same database file (`dirs`/`dir_files` — the directory index; `dir_pairs` — pairwise similarity; `groups`/`group_dirs` — the persisted partition; `grouper_meta` — provenance for each built artifact).

The pipeline is three explicit stages, each persisted so later stages are cheap to re-run:

1. **`--index`** — distill `files.rel_path` into the distinct directories that directly contain at least one **visible** stamped file (direct children only, deliberately: a directory's own file listing is its signature). Two junk filters apply, each reporting its dropped-directory count on stderr: a directory whose **path contains a hidden component** — any part starting with `.`, e.g. `.git/hooks`, `.build/checkouts/x`, `.Trash-1000/…` — is never indexed at all (added 2026-07-17; the earlier rule alone left `.git` internals flooding the grouping, since their *files* — `pre-commit.sample`, `exclude` — are visible), and a directory whose **direct files are all hidden** is dropped (first junk filter, added 2026-07-16). **`--no-junk-filter` disables both** for the run — the escape hatch for deliberately grouping hidden trees (e.g. "is this `.Trash` content duplicated elsewhere?"), mirroring sumtag's `--no-ignore` idiom (renamed from `--no-junk` 2026-07-17: "no junk" read as a promise of junk-free output, when the flag in fact turns the junk *filter* off). The filters decide which *directories* exist, not which files count: a kept directory's hidden files stay in `dir_files` and its signatures. The regime is recorded in `grouper_meta` (an `index` provenance row with a `no_junk` column, both migrated in place on old databases; the row is tombstoned before table surgery and rewritten on success, the same idiom as `--pairs`), and the grouping report prints a note when the index is newer than the pair table — closing the reindex-without-repairing trap the filter change itself creates.
2. **`--pairs`** — compare every directory to every other with one named comparison function (`--fn`; default `name-score`) and store **all** nonzero similarities. The N²/2 comparison is the expensive part, so the threshold is deliberately *not* applied here — regrouping at a different threshold recomputes nothing.
3. **`--threshold X`** — walk stored pairs best-first, partition directories into groups (one placement decision per directory, no merging, ever — the partition is enforced by `group_dirs`' primary key), persist, and report. Skipped when the stored grouping is already current.

`--prep` = stages 1+2; a bare `grouper --database DB` prints the stored grouping. Inspection helpers: `--ls DIR`, `--compare A B`, `--dupes`, `--top [N]`.

**Report ordering (`--sort`, added 2026-07-16):** the grouping report (bare invocation or the one `--threshold` ends with) orders groups by `--sort K` — `bond` (default): creation order, strongest bonds first, i.e. ascending group id as minted by the best-first grouping walk; `files` / `size`: each group's total stamped-file count / byte size (summed over member directories' direct children, same scope as the index), largest first, group id as tiebreak. A display-time choice only — nothing is stored or rebuilt, and within a group members stay alphabetical by path. The stat sorts add the group's file count, byte total, and average bytes per member directory (`avg …/dir`, byte total ÷ member count; added 2026-07-20) to its header line. `size` reads the nullable, `--locate`-populated `files.size` column: if *some* files lack sizes it proceeds with a stderr note that totals undercount; if *all* do, it refuses up front with a `sumtag --locate` hint (exit 1) — never a silently meaningless ordering. `tree-size` (added 2026-07-16) is the recursive variant of `size`: each group's totals cover its member directories' **entire subtrees**, computed at report time by indexed range scans over `files(mountpoint_id, rel_path)` (every path under `dir/` sorts between `dir/` and `dir0`) — still display-only, no new tables. A member nested under another member of the same group is skipped, so nothing double-counts within a group. The semantics shift deliberately from matched-content to subtree-on-disk: tree totals count every stamped file under a member — including files the index never saw (hidden files, everything under dropped all-hidden junk directories) — the header weight says `in tree` to mark the scope, and the same bytes can legitimately appear in several groups' totals (a subdirectory's own group and an ancestor's), so totals across the report don't sum to anything meaningful. Missing sizes behave exactly as `size` (undercount note / `--locate` refusal). Comparison functions are registered by name in `COMPARISONS` as pure **(signature, scorer) pairs** (reshaped 2026-07-15): `signature(files)` distills one directory's file-list into a plain picklable value, built **once per directory** — profiling showed rebuilding it per pair, N−1 times each, dominated the whole `--pairs` phase (~16× once hoisted) — and `score(sig_a, sig_b)` does the pairwise math. The functions: `digest` — content only, renames free; `name-digest` — all-or-nothing per (name, digest); `name-score` — name-anchored partial credit: 1 point for a shared basename, +2 if the digests also agree.

`--pairs` fans the scoring out across `--jobs` worker processes (default: all CPUs; `--jobs 1` disables it, and small corpora run serially regardless since process startup would cost more than it saves). Each worker loads its own signature table straight from the database over a read-only connection — nothing large is ever pickled across the spawn pipe (the first cut shipped the parent's signature table via `initargs`, which at real-archive scale — ~7–8GB of signatures for a 24M-file corpus — wedged the run before the first task was dispatched; fixed 2026-07-15). The corollary: every worker holds a full signature copy in RAM, so **`--jobs` is a memory knob as much as a speed knob**. Workers only score; every database write stays in the parent (SQLite has one writer), with the triangular outer loop dealt out in interleaved stripes so workers get even shares. The run holds the database in WAL journal mode so worker reads and parent commits don't serialize (restored on success; an interrupted run leaves WAL behind, which is harmless). The stored pair set is bit-identical at any `--jobs` value; only insertion order varies, which nothing depends on (the grouping walk orders by its own index).

**Candidate nomination (`--max-df`, added 2026-07-15):** exhaustive all-pairs is infeasible at archive scale (a 4.7M-directory corpus is ~11 trillion comparisons and ~10¹¹ nonzero rows under `name-score`). Above ~50M comparisons (~10k directories) — or with an explicit `--max-df N` — `--pairs` switches from the exhaustive triangular loop to *candidate nomination*: an inverted index over signature keys (basenames for `name-score`, digests for `digest`) nominates only pairs sharing at least one key present in ≤ N directories (default 1000). Nominated pairs get **exact** scores from full signatures — the cap governs who gets looked at, never what a look sees. What's given up: pairs whose *only* overlap is ubiquitous tokens (`.DS_Store` and friends), whose similarity is necessarily tiny except for degenerate boilerplate-only directories. Every comparison function scores 0 for key-disjoint signatures (a documented invariant new functions must preserve), so with a large enough cap nomination is provably lossless — verified in testing: at an unbounded cap the pair set is bit-identical to exhaustive, and at cap 1000 on a dense test corpus every pair ≥ 0.5 similarity survived while runtime fell 16×. `--max-df 0` forces exhaustive at any size; candidate mode is announced on stderr and the cap is recorded in `grouper_meta` (schema migrated in place on old databases) — approximation by consent, never silently, the same idiom as sumtag's `--db-prescan`.

**Storage floor (`--min-sim`, added 2026-07-16):** `--pairs` normally stores every nonzero similarity so `--threshold` stays a free exploratory knob — but at archive scale the weak tail (pairs scoring 0.01 off a few shared names) dwarfs everything interesting. `--min-sim X` keeps only pairs scoring ≥ X: the knob stays free at and above the floor and is deliberately sold below it. The floor governs what is *kept*, never what is *looked at* (`--max-df` governs that) — scores are still computed for every nominated pair, so what's stored is exact. The floor is recorded in `grouper_meta` (column migrated in place, like `max_df`), and a `--threshold` below the stored floor is refused outright with a rerun hint — never answered silently from an incomplete table. A size-ratio scoring prefilter was considered and declined: its bound holds for the current three comparison functions but not for the commented `MAX_SCORES` denominator variants, so it would plant a registry trap (decision recorded in grouper's docstring).

**Cleanup (`--clean-db`, added 2026-07-16):** every grouper table is derived — rebuildable with one `--prep` — and `dir_pairs` in particular can dwarf the `files` table it came from, so `--clean-db` drops all six grouper-owned tables and `VACUUM`s (which is what actually returns the space; SQLite otherwise keeps freed pages). All-or-nothing on purpose: the artifacts are interdependent (the group report joins `dirs` and reads `dir_pairs`), so a partial clean would leave a grouping that can't be reported. Sumtag's own tables are never touched. Standalone — combining it with any other action is a CLI error.

**File-count tie-break (added 2026-07-16):** similarity is deliberately size-normalized (two identical 10,000-file archives and two identical 3-file folders both score 1.0), so at equal similarity the pair sharing **more files** now ranks first: every scorer returns a `(similarity, matched)` tuple — `matched` being the files in common under that function's own notion of matching (shared basenames for `name-score`, the multiset intersection for the Jaccard functions) — stored as a `matched` column in `dir_pairs`, and the grouping walk's order is `(similarity DESC, matched DESC, dir_a, dir_b)`. Big overlaps therefore seed groups (and get low group ids, which lead the report) ahead of trivial ones. A `--threshold` run against a pair table from before the column is refused with a rerun hint (`rerun --pairs`), the same idiom as the `--min-sim` floor — the pair table itself needs no migration since `--pairs` rebuilds it from scratch. A cumulative-byte-size boost is planned as a later companion (it needs the nullable, `--locate`-populated `size` column, so it carries a design question the file count doesn't).

**Name gate (`--name`, added 2026-07-16):** two directories whose basenames differ score 0, unconditionally — only same-named directories can pair, and therefore group. The typical prey is versioned copies of one tree (`proj/`, `backup/proj/`, `old/proj/`), where the directory's own name survives a copy even as contents drift; the gate also slashes what `--pairs` stores. It applies to `--pairs` and `--compare` alike, and is recorded in `grouper_meta` (`name_match` column, migrated in place like `max_df`/`min_sim`). It is a filter *before* scoring — under `--max-df`, nominated candidates are name-checked before the scorer sees them — so every kept score is exact and the nomination invariant is untouched. The mount point itself (`rel_path` `''`) has basename `''` and only ever matches another mount root.

Commits are incremental (per completed stripe), so an interrupted `--pairs` keeps its completed inserts rather than rolling back to zero — but the `pairs` provenance row is deleted (and committed) *before any table surgery* and rewritten only on success, so a partial table can never masquerade as a finished one: `--threshold` refuses it with "no stored pairs" until a `--pairs` run completes. The tombstone-first ordering matters: dropping a large `dir_pairs` is one long uninterruptible C call, and a Ctrl-C during it lands *after* it, so deleting the meta second would leave an empty table wearing valid metadata.

`--progress` adds a live bar on stderr to the build stages (`--index`, `--pairs`), following sumtag's `--progress` conventions: opt-in flag, appears once a stage has run 2 seconds, redrawn in place a few times a second, cleared on completion, suppressed outright when stderr is not a terminal. Redraws are **time-based, not event-based** (added 2026-07-15 after the first cut only redrew when a worker stripe completed — minutes of apparent freeze on a large corpus): the bar keeps ticking while all workers are mid-stripe and during large row inserts, and `--pairs` covers its pre-scoring phases too (file rows loaded, directories signed, then pairs scored). Unlike sumtag's within-file bar the unit is work items, and the denominator is known a priori — N files for `--index`, N(N−1)/2 comparisons for `--pairs` — shown as done/total with rate, percent, elapsed, and ETA. Caveats: derived tables go stale when sumtag rescans (rerun the pipeline), and `dir_files` references `files.rowid`, which `VACUUM` can renumber — both acceptable for artifacts rebuilt in one command.

### dedupe

`dedupe` (added 2026-07-16) is the dangerous one: **it deletes files**. `dedupe --database DB ACTUAL CULL...` dismantles one or more redundant copies of a tree: ACTUAL is the copy being kept (never modified, ever); every file in a CULL whose digest duplicates a file in the *corresponding* ACTUAL directory is deleted **regardless of name** (all copies — three cull files matching one witness all die), and directories emptied by this are removed on the way back up. What survives in cull is always a signpost — something unique, unknown, or suspicious — and the **cull root itself always survives**, even empty, as a placeholder and receipt. **Multiple culls (added 2026-07-21):** the `CULL` positional is repeatable — each cull is walked in sync against the *same* ACTUAL in turn (one shared run, so counters and the summary aggregate across them, and the summary's `cull:` line lists them all). It's sequential single-cull passes, not a new comparison mode: ACTUAL, never modified, is safe to reuse across all of them.

- **The synchronized flat walk** (the load-bearing decision, settled 2026-07-16): both trees are walked depth-first in lockstep by relative path, never descending into a subdirectory name that isn't a real directory on both sides. Comparison and deletion are flat, one directory pair at a time — same name not required, same *relative directory* required. Reorganized duplicates are deliberately out of scope in exchange for a comparison verifiable one directory pair at a time. The one sync exception is the **empty-directory carve-out**: a cull-side subdirectory with no real files anywhere beneath it (only ignorables, qualifying symlinks, empty dirs) is swept even without an actual-side counterpart.
- **Ignorables and symlinks**: `.DS_Store`-grade junk (`IGNORABLE_NAMES`) never blocks a directory's removal and is swept exactly when it's the last thing standing before the rmdir — never earlier. Symlinks are never followed or digest-matched; one is sweepable at exit iff **relative** (target not starting with `/`) and pointing into the cull tree — where it points is what matters, not whether anything is still there (the deletion phase has usually just emptied it). An **absolute symlink is never deletable, wherever it points** (confirmed 2026-07-16), and neither is any link escaping the cull tree: content that blocks, never deleted.
- **`@sumtag-ignore` is honored as a fence** (re-settled 2026-07-16 post-crash): a cull directory containing the marker is pruned outright — not descended, nothing inside deleted, swept, or removed, and the directory never empties; it blocks its parent like any survivor. Sumtag never scanned inside it, so the database should hold no rows there anyway; the fence makes that a guarantee and keeps the junk sweep out too. A marker on the cull root itself is honored with a warning (sumtag's marker-on-root rule) — the run does nothing. Actual-side markers need no handling: an unscanned directory has no rows, so it witnesses nothing.
- **Trust model**: stored digests are trusted — no re-hashing on either side (terabyte-scale, and deleting a true duplicate is reversible since actual keeps the bytes). That argument only holds if stored digests describe current bytes, so three zero-content-I/O vetoes gate every candidate: the database digest must equal the file's own xattr digest (**both** sides), live mtime must equal the xattr's `file_mtime` (**both** sides), and the cull file's live size must equal its witness's (XXH3-64 is a 64-bit non-crypto hash; the size check is free collision insurance — a digest match with differing sizes is a loud `MISMATCH`, kept, exit 2). Failing files are kept with a warning; a failing actual-side file just witnesses nothing. Files the database doesn't know are invisible — never matched, never deleted, left blocking their directory ("so be it").
- **Safety checks**, all before anything is at risk: every root must exist, and ACTUAL must be **disjoint from each cull under `realpath`** (not equal, neither nested — symlinks can't disguise overlap); culls are deliberately **not** checked against each other (settled 2026-07-21 — two overlapping culls only cost redundant work, never the kept copy; the one guarantee that matters is ACTUAL never appearing as a cull); the database must already exist and know every root (live mountpoint recorded, rows present — an unmounted drive errors instead of matching nothing); mixed digest algorithms across the **union of ACTUAL and all culls** are refused without `--allow-mixed` (the documented mixed-algorithm hazard: cross-algorithm duplicates silently survive). At the moment of deletion, `realpath(cull file) == realpath(witness)` means one directory entry reached through links — refused loudly (exit 2), since deleting "the copy" would delete the only name; same inode with *different* realpaths is a genuine hard link — safe, deleted, noted.
- **Arming model**: a bare run is a full preview (`would delete`/`would sweep`/`would rmdir`, database opened read-only); actual deletion requires the explicit `--delete` flag. This deliberately inverts sumtag's `-n` convention — lethality earns opt-in. Deleted files' database rows are deleted in the same run (dedupe cleans up its own kills; `--prune-dirs` wouldn't notice files missing from surviving directories), committed per directory so an interrupted run stays consistent.
- **Command-line echo** (added 2026-07-22): immediately above the summary block, every run prints its own invocation under a `command line:` heading, shell-quoted via `shlex.quote` (paths with whitespace stay one argument) and prefixed with the clean program name (`argparse` `prog`, not `sys.argv[0]`), reconstructed from the actual argv (`sys.argv[1:]`, or the passed list under test). The point is copy-paste convenience — turn a preview into the armed run by copying the line and appending `--delete`. Always printed (dedupe has no `-q`), including under `-n` and after Ctrl-C.
- **Offline prediction (`-n`/`--offline`, added 2026-07-17)**: answers "what *might* be deleted" from the database alone — no filesystem access, so neither root needs to be mounted (dedupe's `-n` is this flag, not a dry-run; the bare run is already the preview). Roots are resolved against the *stored* mountpoints (both rel forms `store._relativize` can emit are generated lexically — plain relpath, which for a firmlinked path is the escaping `../../../var/...` form stamping actually records, and the rooted rebase — with the database's own rows arbitrating which is real), the flat same-relative-directory match runs over the rows, and candidates print as `might delete` — deliberately not `would delete`, because everything the filesystem would have contributed is skipped: the three trust vetoes, the realpath/identity checks, fences, and all sweep/rmdir predictions (unstamped files are invisible, so the database can never know a directory would empty). Two row-only checks survive: the collision-insurance size comparison where `--locate` populated sizes on both sides (a recorded-size mismatch on a digest match is the same loud `MISMATCH`, exit 2), and a shared-recorded-inode note (hard link or alias — a live run decides which). The run is announced (`offline: predicting from database contents alone …` — approximation by consent, the `--db-prescan` idiom), the database opens read-only, the summary headline is `might delete:` (rows without sizes add `(+N of unknown size)` to the byte total), and `-n --delete` is a CLI error — a prediction cannot arm. The live bare preview remains the only exact one. The safety checks that survive offline: ACTUAL-vs-each-cull disjointness (on the resolved mountpoint + rel-prefix identities), the no-rows-under-root refusal, and the mixed-algorithm refusal.
- **Path discipline**: the walk and row lookups use `abspath` — exactly how sumtag records rel_paths — not `realpath`, or a symlinked path component (macOS's `/var` → `/private/var`) would silently match zero rows; pass the roots spelled as sumtag scanned them (a wrong spelling fails the no-rows check with a clear error). `realpath` is reserved for the safety checks. Exit codes are the house 0/1/2: nothing redundant / duplicates found (deleted or would-delete) / errors-or-refusal — plus 130 on Ctrl-C after printing the same summary a completed run prints (counters only count completed work; commits are per directory, so the database matches what actually happened).

### dbmerge

`dbmerge` (added 2026-07-19) combines per-volume sumtag databases into one: `dbmerge --database TARGET SOURCE...` folds each SOURCE (opened read-only, always) into TARGET, so cross-volume `dedupe`/`grouper` runs get the single database they consume — with zero changes to those tools, since the schema was always multi-volume (the `mountpoints` table exists precisely so one database can describe many filesystems; one-database-per-volume is an operational choice that buys parallel scanning, SQLite being single-writer). The intended workflow: per-volume databases stay the scanning targets, the merged database is a rebuildable analysis artifact, re-merged whenever the sources move on (after a rescan or `--prune-all`).

- **Replace-per-mountpoint** (the load-bearing semantic, settled 2026-07-19): for each mountpoint present in a source, the target's existing rows for that mountpoint are deleted and the source's inserted fresh. A row-level upsert would never remove target rows whose source rows were pruned — ghosts forever; replacement makes the merge idempotent and each source authoritative for its mountpoints, including an empty one (a source mountpoint with zero rows still clears the target's — authoritative emptiness). Target mountpoints no source names are left untouched, so one volume can be re-merged without feeding all of them.
- **The collision refusal is the corollary**: the same mountpoint path recorded in two sources is an error (`sources must partition by mountpoint`) — the second replacement would delete the first's just-merged rows. This doubles as the guard for the known limitation that a mount path is not a globally stable filesystem identity: two different filesystems recorded under one path are never silently interleaved.
- **What never flows**: grouper's derived tables are not copied (`dir_files` references `files.rowid`, which the merge renumbers), and artifacts already in the target are **dropped** with an announcement — a merge that changed `files` has invalidated them (rerun `grouper --prep`; its `--clean-db` VACUUM is there if the space matters). The `prescan_summary` row is neither copied nor touched: it describes a filesystem walk, which the merge does not invalidate. Sources are never modified.
- **Guards**, all before the target is opened for writing: every source must exist and carry the sumtag tables; the target may not also be a source, nor a source be given twice (checked under `realpath`); the mountpoint collision above; and a merged corpus spanning more than one `algo` (sources plus the target's surviving rows) is refused without `--allow-mixed` — the documented mixed-algorithm hazard.
- **`-n`/`--dry-run`** previews everything (`would merge`/`would replace`/`would drop`, per-mountpoint row counts) with no side effects: the target is opened read-only if present and not created if absent. `--progress` shows **one aggregate bar across all sources** — rows-merged/rows-total via the house `CountIndicator` (unit `rows`; the denominator is a cheap up-front `COUNT(*)` per source), all the usual conventions (2-second threshold, stderr-tty only, cleared on completion).
- **Commits are per mountpoint**, so an interrupted run keeps completed mountpoints, counters only count committed work, and Ctrl-C prints the normal summary and exits 130. Exit codes are the house 0/1/2 with one honest wrinkle: `1` (target modified — or would be, under `-n`) is the *normal* successful merge, because replacement always rewrites; `0` (nothing to do) occurs only for empty sources; `2` = errors or a refusal. The summary block is the house style: `merge:` (rows, mountpoints, sources headline — prints even at zero), `replace:`/`drop:` when nonzero, `database:`, `sources:`.

### query (planned)

A companion query program has been discussed but no code exists in the repo yet; this heading is the placeholder so it gets documented the moment it lands.

## Future work (designed-for, not built)

These are deliberately deferred; the formats above are shaped now so adding them later is additive, not a migration.

- **A subcommand CLI** (`sumtag sum /data`, `sumtag verify /backup`) — the mandatory-action-flag model (built 2026-07-13; see CLI usage) is its stepping stone: every action flag maps one-to-one onto a future subcommand, and modifiers stay flags on those subcommands. The stamping verb's name can be revisited at that migration (`sum` reads like a promise to compute; `update` or `stamp` may be more honest about the converge-to-current, skip-when-fresh semantics).
- **Alternate digest algorithms** (e.g. `md5`) — selectable via a future `--digest` flag (default `xxh3`). Stored in the `digests` map (one entry at a time, replaced on re-hash — see "Digest container" and "Re-hashing logic"); DB columns are already generic (`algo`/`digest`). Switching the active algorithm never forces a re-hash of already-current files by itself (freshness is algorithm-agnostic); use `--force` to deliberately re-stamp an archive under a new algorithm.
- **Network database backends** — MySQL/MariaDB and Postgres via `--database=scheme://…`. The value grammar and `open_store()`/`Store` seam are fixed now; only SQLite is implemented.
- **`runs` table** — normalize the repeated `run_started_at` (see Database storage).
- **Fine-grained stale-row pruning**, **duplicate detection**, and **richer audit tooling** (aggregate reporting, scheduled scrubs, quarantine, repair-from-replica) — higher-level tooling, outside sumtag's single-purpose scope (`grouper` is the experimental playground for this — see Experimental companion programs). Note: basic single-pass verification *is* built in (`--verify`), as is stale-row pruning (`--prune-dirs` and `--prune-all`, added 2026-07-16); what stays out is everything that aggregates or acts on the results, plus moved-file detection. Whatever builds duplicate detection on top of the database should account for the mixed-algorithm hazard noted in Database storage's Schema section — e.g. by scanning the candidate file sets for more than one `algo` value before comparing, warning about the apples-to-oranges risk, and confirming before proceeding (with a non-interactive override for scripted use, since this guidance is for a separate tool and doesn't bind sumtag's own no-prompts CLI).

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
  grouper.py      # companion program; `grouper` entry point (see Experimental companion programs)
  dedupe.py       # companion program; `dedupe` entry point (see Experimental companion programs)
  dbmerge.py      # companion program; `dbmerge` entry point (see Experimental companion programs)
pyproject.toml
```

### Entry point

Packaging is **setuptools** via `pyproject.toml`, declaring `console_scripts` entry points. On install, pip generates launchers named `sumtag`, `grouper`, `dedupe`, and `dbmerge` on `PATH` (each with a shebang pointing at the install environment's interpreter):

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
grouper = "sumtag.grouper:main"
dedupe = "sumtag.dedupe:main"
dbmerge = "sumtag.dbmerge:main"
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

### Man pages

Each installed command has a man page under `man/` — `sumtag.1`, `grouper.1`, `dedupe.1` (added 2026-07-16 with the commands' promotion), `dbmerge.1` (added 2026-07-19) — and each page is kept in **three formats**, all committed:

- **`.1`** — troff source, the source of truth. Edit this first for any CLI or behavior change.
- **`.1.txt`** — plain-text rendering, regenerated with `MANWIDTH=80 man ./man/<page>.1 | col -b`. Caveat for `sumtag.1.txt` specifically: its original baseline predates that command and formats slightly differently (wrap points, bullet glyphs), so regenerate it wholesale only if the whole-file diff is acceptable — otherwise **hand-edit it to mirror the `.1` change** in its existing style, which is how it has been maintained so far. `grouper.1.txt`, `dedupe.1.txt`, and `dbmerge.1.txt` were generated by that exact command from the start, so wholesale regeneration is diff-stable for them.
- **`.1.pdf`** — PDF rendering, regenerated wholesale each time the `.1` changes: `mandoc -T pdf man/<page>.1 > man/<page>.1.pdf` (kept per explicit user request, 2026-07-02).

Whenever a `.1` file is edited, update its `.txt` and `.pdf` in the same change — the three formats are never allowed to drift.

## CLI usage

The installed `sumtag` command is the primary form; `python3 -m sumtag` is equivalent and convenient during development.

```bash
# Stamp the current directory
sumtag --sum .

# Stamp one or more directories
sumtag --sum /data /backup

# Stamp the current directory plus another directory
sumtag --sum . /data

# Equivalent, from a source checkout (development)
python3 -m sumtag --sum /data /backup
```

**An explicit action flag is required** (decided 2026-07-13): every run must name what it does — one of `--sum`, `--verify`, `--remove`, `--import`, `--locate`, `--prune-dirs`, `--prune-all` — and a bare `sumtag /data` is a CLI error naming the choices. `--sum` is the plain stamping action (it does not require `--database`; adding one makes it also mirror — see Database storage). Before this change the stamp mode was the unnamed default; requiring the verb finishes the same principle as the no-cwd-default rule below. `--prescan` and `--db-prescan` are modifiers, never actions — with the unnamed default gone, `sumtag --prescan /data` fails the action requirement automatically, with no extra conflict rule. The action-flag requirement is also the stepping stone to a future subcommand CLI (`sumtag sum /data` — see Future work), where an action subcommand is structurally mandatory.

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
| `--sum` | | bool | The stamping action: (re-)hash per the normal mtime-based decision and write the xattr. With `--database`, also mirror the result into it. |
| `--import` | | bool | Never compute checksums; only copy metadata already present in xattrs into the database. Requires `--database`. |
| `--verify` | | bool | Read-only: recompute each file's checksum and compare it to the stored digest, reporting mismatches (corruption). Writes nothing. Exit `0`/`1`/`2` = intact/corruption/errors. |
| `--no-ignore` | | bool | Disregard all `@sumtag-ignore` marker files for this run, processing every directory regardless of markers. |
| `--exclude` | | str (repeatable) | Skip files/directories whose basename matches the glob PATTERN; a matching directory's whole subtree is pruned. May be given multiple times. Applies in every mode; unaffected by `--no-ignore` (see Command-line exclusion). |
| `--locate` | | bool | Stat every file visited and write the `os.stat()` metadata to the database, regardless of whether xattr work was done; implies `--import`. Requires `--database`. Useful as a periodic filesystem inventory pass (analogous to `updatedb`). |
| `--si` | | bool | Display sizes and rates in `--progress` using decimal (SI, powers-of-1000: `kB`/`MB`/`GB`) units instead of the default binary (powers-of-1024: `KiB`/`MiB`/`GiB`) units. |
| `--remove` | | bool | Remove the `user.sumtag` xattr from every file in the tree (see Removing stamps). A testing/reset utility, not a data-integrity primitive. Composes with `-n` to preview. |
| `--prescan` | | bool | Walk the tree once before the real pass to count the files that will be checksummed and their total size, then prefix each hash/verify announcement with an nnn/mmm file counter and a bytes-so-far/total counter, each followed by its percentage (see `--prescan` below). On a `--database` run (and not `-n`), also stores the totals as the database's one-row prescan summary (see `--db-prescan`). Cannot be combined with `--remove`. |
| `--db-prescan` | | bool | Like `--prescan`, but load the counters' totals from the summary a previous `--prescan --database` run stored, instead of walking the filesystem — an approximate progress report bought without the extra walk (see `--db-prescan` below). Requires `--database`; cannot be combined with `--prescan` or `--remove`. |
| `--prune-dirs` | | bool | Check every directory the database knows under the given roots; delete the rows of directories that no longer exist (see Pruning stale database rows). Requires `--database`; the filesystem is never modified. Exit `0`/`1`/`2` = nothing stale/pruned/errors. Composes with `-n` to preview and `--progress` for a live nnn/mmm counter. |
| `--prune-all` | | bool | Like `--prune-dirs`, and additionally check every file row in directories that still exist (one `lstat` each), deleting the rows of files that no longer exist (see Pruning stale database rows). Same requirements, exit codes, and compositions as `--prune-dirs`; giving both is redundant but allowed. |

**An action flag is required** — one of `--sum`, `--verify`, `--remove`, `--import`, `--locate`, `--prune-dirs`, `--prune-all`; a run naming none is an error (see CLI usage). `-q` and `-v` together are an error. `--force` and `--dry-run` together are an error. `--sum`, `--import`, and `--locate` are parallel, combinable actions (see Database storage); `--import` and `--locate` each require `--database`, `--sum` does not (alone it stamps xattrs only), and `--database` requires at least one of the three. `--force` and `--import` together are **allowed**: `--import`'s refusal to compute is a default, not a hard restriction, and `--force` is the flag whose whole job is overriding defaults about what gets (re-)computed — so `--force --import` re-hashes every file and mirrors the result. The same override applies to `--force --locate`, since `--locate` implies `--import`. `--verify` conflicts with `--database`, `--sum`, `--import`, `--locate`, and `--force` (see Verification); `--verify -n` is a redundant no-op and is allowed. `--remove` conflicts with `--database`, `--sum`, `--import`, `--locate`, `--verify`, and `--force` — for the same reason as `--verify`, it is its own standalone mode, and there is no re-hash decision for `--force` to override; `--remove -n` is allowed and is the way to preview it. `--prescan` conflicts with `--remove` for the same reason: `--remove` never computes anything, so there is nothing for `--prescan` to count. `--db-prescan` requires `--database` (the summary lives there) and conflicts with `--prescan` (two sources for the same counters) and `--remove` (same reason as `--prescan`); `--verify` cannot take it, since `--verify` conflicts with `--database` outright. `--prune-dirs` and `--prune-all` each require `--database` and conflict with every other action, with `--force`, with `--prescan`/`--db-prescan`, and with `--exclude`/`--no-ignore` (see Pruning stale database rows); each composes with `-n` as the intended preview, and the two together are redundant but allowed (`--prune-all` subsumes `--prune-dirs`). `--progress` and `-q` conflict: whichever appears later on the command line wins, with a warning to stderr. `--force` does **not** override `@sumtag-ignore` markers (see Ignore markers); use `--no-ignore` to process exempted directories.

### Status lines

Sumtag's default (non-quiet) output is an *announcement*, not a completion report: for any file about to be checksummed, a line prints **before** the read begins. **Without `-v`, that line is the bare path and nothing else** — no verb, no reason, just the path, so `sumtag --sum` over a large tree stays clean and skimmable. **With `-v`**, the same announcement expands to the full form — `hash <path> (<reason>)` for a stamp, `verify <path>` for `--verify`, `import <path>` for a propagated import, `would hash <path> (<reason>)` under `--dry-run` — using present/imperative verbs, not past tense (`hash`/`import`, not `hashed`/`imported`). This bare-path/`-v` split applies uniformly to every routine per-file announcement (stamp, dry-run preview, import, and the no-usable-metadata report under `--import`/`--locate`); it does **not** apply to `--progress`, which is unaffected by `-v` and appears regardless, nor to the deviation lines below. The path being on screen before the read begins is also what makes `--progress` legible: it's already there by the time a slow file's live bar appears at the 2-second mark, rather than the bar being the first anyone hears of that file.

With `--prescan`, the hash/`would hash`/`verify` announcement (bare-path or `-v` form alike) additionally gets an nnn/mmm and bytes-so-far/total counter prepended, each with its parenthesized percentage — see `--prescan` below. It does not touch `import`, `skip`, or the no-usable-metadata report; those aren't "the line before summing a file" in the first place.

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

**Every redraw is hard-clamped to the terminal width before writing** (fixed 2026-07-17; see BUGS.md). The fixed field budgets can't guarantee fit on their own — binary sizes render 9 characters in the 1000–1023.9 band of a unit (`1010.0MiB`), overflowing the 8-wide size and 10-wide rate fields, and a terminal narrower than the ~52-char fixed budget overflows regardless — and an over-width line wraps onto a second row, where the single-line `\r`-and-erase discipline can only reach the continuation row, stranding the bar's first row onscreen amid the per-file announcements. Clamping loses at most the tail of the left-aligned ETA field in those rare bands, which beats a wrapped line every time. For the same never-strand-the-bar reason, the indicator's clear runs in a `finally`: a file whose read fails mid-hash still clears the bar before the error line prints, in both the stamp and verify passes.

### `--prescan`

On a very large tree, the default output gives no sense of *how far along* a run is — files stream by with no indication of what fraction of the work is done. `--prescan` fixes that by walking the tree once, up front, purely to count: how many files the run will actually checksum, and their total size. The real pass then runs exactly as it always has, except each hash/verify announcement gains a counter prefix:

```
nnn/mmm (pp%)  bytes-so-far/bytes-total (pp%)  <the usual announcement>
```

Example (`-v`, mid-run):

```
042/137 ( 31%)  118.2MiB/4.2GiB (  3%)  hash /backup/vault/photo0042.dng (file modified since last hash)
```

Or without `-v` (bare path, prefix still shown):

```
042/137 ( 31%)  118.2MiB/4.2GiB (  3%)  /backup/vault/photo0042.dng
```

- **nnn** is this file's ordinal position among the files being checksummed this run, zero-padded to the width of **mmm** (e.g. `007/137`, not `7/137`) so the column stays aligned as the count climbs.
- **mmm** is the total count `--prescan` found up front.
- **bytes-so-far** is the sum of the sizes of files *already completed* before this announcement (so it reads `0B` on the very first file) — not a live in-file counter like `--progress`; this line prints once per file, before that file's read begins.
- **bytes-total** is the total size `--prescan` found up front.
- Both byte figures are human-readable, honoring `--si` exactly like `--progress`'s size field.
- **Each fraction is followed by its whole-number percentage in parens** (added 2026-07-19: every fraction shown to indicate progress carries one). The percentage is right-padded to three digits — `(  0%)` through `(100%)` — so the token keeps one width and the columns after it never jitter, the same fixed-width convention that keeps the `--progress` bar's own `pct` field from overflowing at 100%. Under `--db-prescan` drift `nnn` can overshoot `mmm` and the percentage can pass 100; that line simply widens by a character, harmless in an appended log (unlike a redrawn bar, nothing can be stranded).

**What counts as "will be checksummed" mirrors whichever mode is running:**

- For the normal hash/stamp pass (`--sum`, with or without `--database`, or `--import`/`--locate`), it is exactly the set of files the mtime-based re-hash decision (or `--force`) will cause to be hashed — the same files that would otherwise print `hash`/`would hash`. Files that will be skipped, imported without computing, or reported as having no metadata are not counted and do not get a prefix (CLAUDE.md "Status lines" — that line was never "the line before summing a file" to begin with).
- For `--verify`, it is every file with a usable stored digest — the same set that will be read and recomputed, as opposed to reported `unverifiable` outright.
- `--remove` computes nothing, so `--prescan` has nothing to count; the two are a CLI error together (see Flags).

**Cost and a known limitation:** the prescan walk duplicates the traversal and the xattr/stat reads the real pass is about to do anyway — a deliberate trade of one extra cheap (metadata-only, no file content read) pass for a progress indication that would otherwise be impossible to give up front. Because it is a separate pass, its counts are a prediction, not a guarantee: if the tree changes between the prescan and the real pass (a file is added, removed, or its own re-hash decision flips), `nnn`/`mmm` can drift slightly out of sync with what the real pass actually does. This is a display aid, not authoritative accounting — nothing about hashing, stamping, or exit codes depends on it. The prescan pass suppresses the traversal warnings the real pass already prints (e.g. a scan root's own `@sumtag-ignore`), so nothing is warned about twice.

The prescan walk announces each directory it visits, printing the directory's path before any file metadata inside it is read — following the same bare-path/`-v` split as every routine announcement (bare path by default, `prescan <path>` with `-v`) and suppressed by `-q`. This gives the otherwise-silent up-front counting pass its own sign of life on a large tree. Pruned (`@sumtag-ignore`) directories are not announced — they are not visited.

On a `--database` run (and not under `-n`), `--prescan` additionally **persists its totals** as the database's one-row prescan summary — file count, byte total, normalized scan roots, the full counting context (`--sum`, `--force`, `--exclude` patterns, `--no-ignore` — everything that determined which files got counted), and a timestamp — replacing any previous summary; one per database. That row is what `--db-prescan` consumes.

### `--db-prescan`

On a very large tree the `--prescan` walk is itself expensive (the motivating case: a 48TB filesystem whose prescan alone took over an hour) — and an interrupted `--sum` run pays it again on restart. `--db-prescan` (added 2026-07-13) is a `--prescan` alternative that reads mmm/bytes-total from the summary a previous `--prescan --database` run stored, instead of walking the filesystem: seconds instead of an hour. The real pass then runs exactly as it always has, with the counter prefix driven by the stored totals.

- **Display-only, by hard rule.** The stored data never influences the summing pass in any way: the real pass walks the filesystem and makes every per-file mtime decision exactly as it does without the flag; nothing is skipped or trusted based on stored data (see Database storage's sink-never-a-source principle). Only the two totals shown in the counter prefix come from the database.
- **Approximation by consent.** `nnn` counts what actually happens this run, against a `mmm` frozen at prescan time — so it may overshoot `mmm` (the tree grew or changed) or the run may finish below it (some of the counted work was already done, e.g. by the interrupted run the totals came from). Choosing the flag is accepting an approximate progress report; nothing about hashing, stamping, or exit codes depends on it.
- **Match or error.** The stored summary is used **only if it answers this run's question**: the scan roots (compared as sets of normalized absolute paths, so `/data` vs `/data/` vs a relative respelling never falsely mismatches) and the full counting context must all equal the current run's. A missing summary or any mismatch is a hard error at startup (exit 2, before any side effect — the check runs before the store is even opened, so not even the database file is created): `no stored prescan totals; run --prescan --database first`, or `stored prescan totals do not match this run (<what differs>)`. Never a silent fallback to the filesystem walk the flag exists to avoid, and never a counterless multi-hour run — "an error, not a silent no-op."
- **Announced at startup.** `using stored prescan totals: 137 files, 4.2GiB, from <timestamp>` prints on the normal output channel (`-q` suppresses it; the byte figure honors `--si`), so a consumer of stale totals is told exactly what it is consuming.
- **Composition.** Requires `--database`. Conflicts with `--prescan` (two sources for the same counters) and `--remove` (nothing to count). `--verify` cannot take it since `--verify` conflicts with `--database` outright — and verify could never write its own mode-correct summary (it is strictly read-only), so this stays excluded unless that trade-off is ever revisited. `-n` composes: the summary is *read* (read-only open — a missing database file is not created as a byproduct) and the dry-run counters display, while `-n` separately keeps `--prescan` from persisting.

The typical resume flow after an interrupted big run: the original `sumtag --sum --database db --prescan /data` stored the totals while it counted; the restart is `sumtag --sum --database db --db-prescan /data`, which skips straight to summing with counters that continue against the original plan.

`-q` and `-v` use `action='count'` in argparse, so `-vv` and `-v -v` are equivalent.

Short forms are deliberately limited to the four frequently-typed flags — `-n`, `-q`, `-v`, `-f`. The rest (`--progress`, `--database`, `--sum`, `--import`, `--verify`, `--no-ignore`, `--exclude`, `--locate`, `--si`, `--remove`, `--prescan`, `--db-prescan`) are **long-only by design**: most are rare, deliberate operations where spelling out the name is a feature, not friction. (`--verify` would also collide awkwardly with `-v`/verbose.) This is a settled choice, not an oversight.
