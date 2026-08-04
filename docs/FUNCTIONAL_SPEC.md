# sumtag — Functional Specification

**Status:** reference specification, derived from the current implementation and
`CLAUDE.md`.
**Purpose:** describe the *observable behavior* of `sumtag` and its three companion
programs (`grouper`, `dedupe`, `dbmerge`) in enough detail to build a second,
functionally equivalent implementation from scratch using Test-Driven Development.

---

## How to read this document

### What "functionally equivalent" means

A reimplementation is equivalent if, for the same inputs, it produces the same
**externally observable results**:

- **xattr contents** — a parseable `user.sumtag` JSON document with the same keys and
  the same values (digest, timestamps, version). *Byte-for-byte* JSON layout is **not**
  contractual: the reference reads xattrs back by parsing JSON, and the conformance
  oracle does the same, so key ordering and separator whitespace are free to differ.
- **database rows** — the same SQLite schema and the same row values for a given tree.
- **stdout / stderr text** — the status lines, summary blocks, warnings, and error
  messages described in Part VI (and the companion sections). Progress output on a
  non-terminal is suppressed, so it is not part of the contract.
- **process exit code** — exactly as specified.

Anything *not* in that list — module layout, function names, data structures, whether the
decision logic is a pure function or inlined — is an implementation choice. The reference
happens to isolate all I/O-free decision logic into pure functions; a reimplementation is
encouraged to do the same because it makes Part II directly unit-testable, but it is not
required to.

### Requirement IDs

Requirements are numbered `REQ-<AREA>-<n>`. Each is meant to map to one or more tests.
Behavior is stated as prose, given/when/then, or truth tables, whichever is clearest.
Areas: `SCHEMA`, `TIME`, `PATH`, `DB`, `REHASH`, `VERIFY`, `WALK`, `CLI`, `EXIT`, `SUM`,
`REMOVE`, `PRUNE`, `OUT`, `PROG`, `PRESCAN`, `PKG`, `PLAT`, `GROUPER`, `DEDUPE`, `DBMERGE`.

### The reference implementation at a glance

`sumtag` recursively walks one or more directory trees. For each regular file it computes
an XXH3-64 checksum and stores it, with metadata, in an extended attribute named
`user.sumtag`. A file whose recorded modification time still matches the file is skipped,
so re-runs over a static archive are cheap. The checksum is a makeshift detector of silent
data corruption (`--verify` recomputes and compares) and a basis for higher-level tooling
(duplicate finding, etc.). Metadata can optionally be mirrored into a SQLite database.

---

# Part I — Domain model & data formats

## The extended attribute

- **REQ-SCHEMA-1** — The attribute name is the literal string `user.sumtag`, identical on
  Linux and macOS.
- **REQ-SCHEMA-2** — The value is UTF-8-encoded JSON encoding a single object with exactly
  these five keys:

  ```json
  {
    "version": "0.1.0",
    "digests": { "xxh3": "<16-char lowercase hex>" },
    "file_mtime": "<ISO 8601 UTC, microseconds, Z>",
    "hashed_at": "<ISO 8601 UTC, microseconds, Z>",
    "run_started_at": "<ISO 8601 UTC, microseconds, Z>"
  }
  ```

- **REQ-SCHEMA-3** — `version` is the software/schema version (the reference uses the
  package version, currently `0.1.0`). It is stamped into every xattr the run writes.
- **REQ-SCHEMA-4** — `digests` is a JSON object mapping an algorithm name to a lowercase
  hex digest. **It holds exactly one entry, ever.** When a re-hash occurs, the active
  algorithm's entry *replaces* whatever entry was present; entries are never accumulated
  (no `{ "xxh3": …, "md5": … }`). Readers must iterate the map generically rather than
  hard-coding a key.
- **REQ-SCHEMA-5** — `file_mtime` is the file's modification time captured at hash time.
  `hashed_at` is when the xattr was written. `run_started_at` is set once at process
  startup and written identically into every xattr in that run. `run_started_at` is
  passenger data: it plays no part in any re-hash or verify decision.
- **REQ-SCHEMA-6** (parse / validation) — Parsing xattr bytes must **fail softly**: if the
  bytes are not valid UTF-8 JSON, not a JSON object, missing any of the five required keys,
  or `digests` is not a JSON object, the file is treated exactly as if it had **no xattr**
  (`meta = None`). A malformed xattr is never an error; it just means "no usable metadata".

## The digest

- **REQ-SCHEMA-7** — The active algorithm is `xxh3`: the **64-bit** XXH3 variant
  (`xxhash.xxh3_64`), rendered as a 16-character lowercase hex string.
- **REQ-SCHEMA-8** — The algorithm is a single switch point. The same constant names the
  `digests` map key and the database `algo` column. The reference also has `md5` wired into
  its hasher table for future use, but there is **no CLI flag** to select it today; `xxh3`
  is always what gets computed. A reimplementation needs only `xxh3` to be
  behavior-equivalent, but must keep the map/column generic (REQ-SCHEMA-4).
- **REQ-SCHEMA-9** — Files are hashed by streaming in chunks (the reference uses 1 MiB), so
  arbitrarily large files hash in bounded memory. The digest value must equal a whole-file
  XXH3-64 of the bytes.

## Timestamps

- **REQ-TIME-1** — All three timestamps are ISO 8601 UTC with **microsecond** precision and
  a trailing `Z`: `YYYY-MM-DDTHH:MM:SS.ffffffZ` (exactly 6 fractional digits), e.g.
  `2026-06-03T10:00:00.123456Z`.
- **REQ-TIME-2** — When formatting a file's mtime, compute from integer **nanoseconds**
  (`st_mtime_ns`) and truncate to microseconds (`ns // 1000`), not from a float, so two
  equal `st_mtime_ns` values always format to the same string.
- **REQ-TIME-3** — Mtime comparison is done on these fixed-width strings and is therefore a
  lexicographic comparison that orders the same as time. Because the stored `file_mtime`
  and the recomputed live mtime are formatted by the identical function, the comparison is
  symmetric and truncation-consistent.
- **REQ-TIME-4** — `major_of(version)` is the integer before the first `.` (e.g.
  `major_of("0.1.0") == 0`).

## Mount-relative path strategy (database only)

The xattr needs no path (it rides on the file). The database is detached, so every row
records *which* file it describes — stored **relative to the file's mount point**, with the
mount point recorded separately, so paths survive remounts.

- **REQ-PATH-1** — `mount_relative(path)` returns `(mount_point, rel_path)` such that
  `os.path.join(mount_point, rel_path)` locates the original file, verified with
  `samefile`.
- **REQ-PATH-2** (Linux and other platforms) — the mount point is found by walking up
  parents until `os.path.ismount()` reports a boundary (a change in `st_dev`).
- **REQ-PATH-3** (macOS) — `os.path.ismount()` is **not** used, because APFS firmlinks make
  the read-only System volume (`/`) and the writable Data volume
  (`/System/Volumes/Data`) share one `st_dev`; `ismount` would walk past the real mount to
  `/`. Instead call `statfs(2)` and read `f_mntonname` (bound via `ctypes` to libSystem, to
  avoid a second third-party dependency). A firmlinked path such as `/Users/x` is not
  lexically beneath its mount `/System/Volumes/Data`, so `rel_path` is computed by rebasing
  the whole rooted path under the mount and verifying with `samefile`; the plain
  (upward-escaping) relpath is used only as a fallback if the rebase does not recompose to
  the same file.
- **REQ-PATH-4** — Known limitation (not a bug to reproduce, but the behavior must match):
  a mount-relative path is not a globally stable identity — two filesystems mounted at the
  same point at different times collide. This is accepted.

## SQLite schema

- **REQ-DB-1** — The database has these tables (created on first open):

  ```sql
  CREATE TABLE mountpoints (
    id   INTEGER PRIMARY KEY,
    path TEXT UNIQUE NOT NULL
  );
  CREATE TABLE files (
    mountpoint_id  INTEGER NOT NULL REFERENCES mountpoints(id),
    rel_path       TEXT NOT NULL,
    inode          INTEGER NOT NULL,
    algo           TEXT NOT NULL,
    digest         TEXT NOT NULL,
    file_mtime     TEXT NOT NULL,
    hashed_at      TEXT NOT NULL,
    run_started_at TEXT NOT NULL,
    version        TEXT NOT NULL,
    size           INTEGER,     -- locate columns: NULL until --locate fills them
    mode           INTEGER,
    uid            INTEGER,
    gid            INTEGER,
    nlink          INTEGER,
    dev            INTEGER,
    ctime          TEXT,        -- ISO 8601 UTC
    atime          TEXT,        -- ISO 8601 UTC
    birthtime      TEXT,        -- macOS only; NULL elsewhere
    UNIQUE (mountpoint_id, rel_path)
  );
  CREATE INDEX idx_digest ON files(digest);
  CREATE TABLE prescan_summary ( ... );   -- one row; see Part VI / --db-prescan
  ```

- **REQ-DB-2** — A file's **identity** is `(mountpoint_id, rel_path)`: exactly one row per
  file location. Writing a file is an UPSERT on that key (`INSERT … ON CONFLICT DO UPDATE`).
  Re-scanning updates the row in place; there are never duplicate rows for one location.
- **REQ-DB-3** — `mountpoints` normalizes the repeated mount path; `files` references it by
  integer id. A mount path is inserted once (`INSERT OR IGNORE`) and cached.
- **REQ-DB-4** — `digest` is indexed but **not unique**: duplicate detection depends on the
  hash repeating. Dedup queries group by `(algo, digest)`.
- **REQ-DB-5** — The **locate columns** (`size`, `mode`, `uid`, `gid`, `nlink`, `dev`,
  `ctime`, `atime`, `birthtime`) mirror `os.stat()`. They are nullable. A write **without**
  stat data must preserve existing locate columns: the UPSERT uses
  `COALESCE(excluded.col, col)` for every locate column, so a later stat-less update never
  clobbers values a prior `--locate` run wrote. `file_mtime` and `inode` live in the
  primary columns and are not duplicated among locate columns. `birthtime` is macOS-only.
- **REQ-DB-6** — There is a separate `update_stat(mountpoint_id, rel_path, stat)` operation
  that writes only the locate columns and is a **no-op if the row is absent** (used by
  `--locate` for files that have no xattr to mirror but may already have a row).
- **REQ-DB-7** — The database is a **sink, never a source**. Metadata flows only *toward*
  the database (computed→xattr→db, or existing xattr→db). Sumtag never writes an xattr from
  database contents. There is no restore-from-database feature; it is rejected by design.

## The `--database` value grammar

- **REQ-DB-8** — The `--database` value is resolved by one function
  (`open_store(value, mode) -> Store`). Grammar:
  - A value matching `^[a-z][a-z0-9+.-]*://` is a **DSN**, dispatched by scheme. `sqlite://`
    is accepted (the remainder after `://` is the path). Any other scheme is recognized and
    **rejected** with a "not yet supported" `NotImplementedError`.
  - Any other value (no `scheme://`) is a **SQLite file path**. A bare `mysql:host` (colon,
    no `//`) is a *path*, because `:` is a legal filename character.
- **REQ-DB-9** — Open modes: `rwc` (default) creates a missing database file; `rw` and `ro`
  require it to already exist (a missing file raises `FileNotFoundError`); `ro` additionally
  skips schema creation (a strictly read-only open). See the per-mode requirements for which
  mode each uses.

---

# Part II — Pure decision logic (the crown jewels for unit tests)

These two functions take plain values and return a verdict with no I/O. They should be the
very first thing built and tested.

## Re-hash decision

**REQ-REHASH-1** — `should_rehash(meta, live_mtime, force, current_major) -> (bool, reason)`
returns whether a file must be (re-)hashed and a human reason string, per this table
(first matching row wins, top to bottom):

| # | Condition | Result | reason |
|---|---|---|---|
| 1 | `force` is true | **re-hash** | `forced` |
| 2 | `meta is None` (absent/unreadable/malformed) | **re-hash** | `no usable metadata` |
| 3 | `major_of(meta["version"]) < current_major` | **re-hash** | `older major version` |
| 4 | `meta["digests"]` is empty/falsey | **re-hash** | `no digest present` |
| 5 | `meta["file_mtime"] < live_mtime` | **re-hash** | `file modified since last hash` |
| 6 | otherwise | **skip** | `up-to-date` |

- **REQ-REHASH-2** (algorithm-agnostic freshness) — Row 4 fires only when the map has *no*
  digest at all. A file carrying *any* current digest (any algorithm) is up-to-date;
  switching which algorithm is active never by itself forces a re-hash. (An archive stamped
  under one algorithm is not re-read wholesale when a new default arrives; use `--force`.)
- **REQ-REHASH-3** — A newer major *version* in the file than the software does not force a
  re-hash; only an **older** stored major does (row 3). Equal major does not.

## Verify classification

**REQ-VERIFY-1** — `classify_verify(meta, live_mtime, computed) -> outcome`, where
`computed` maps each algorithm present in the xattr to its freshly recomputed digest,
returns one of `INTACT` / `CORRUPTION` / `STALE` / `UNVERIFIABLE`:

| # | Condition | Outcome |
|---|---|---|
| 1 | `meta is None` | `UNVERIFIABLE` |
| 2 | `meta["digests"]` empty | `UNVERIFIABLE` |
| 3 | every stored digest equals its recomputed value | `INTACT` |
| 4 | (some digest differs) and `file_mtime == live_mtime` | `CORRUPTION` |
| 5 | (some digest differs) and `file_mtime != live_mtime` | `STALE` |

- **REQ-VERIFY-2** — The mtime gate is what separates corruption from a legitimate edit:
  contents changed **but mtime did not** is the alarm case (`CORRUPTION`); contents changed
  **and mtime advanced** is merely a stale stamp (`STALE`), not corruption.
- **REQ-VERIFY-3** — `INTACT` wins regardless of mtime: if the bytes match every stored
  digest, the file is intact even if the mtime moved (a touch that did not change content).
- **REQ-VERIFY-4** — Multiple digests are handled generically: every algorithm present is
  recomputed and compared; `INTACT` requires *all* to match.

## The verify truth table (end-to-end view)

For reference, the observable meaning of the four combinations (matches REQ-VERIFY-1):

| stored vs live mtime | digest | outcome | meaning |
|---|---|---|---|
| same | match | `INTACT` | verified intact |
| same | mismatch | `CORRUPTION` | silent corruption (the alarm case) |
| changed | mismatch | `STALE` | legitimately modified; stamp is stale |
| changed | match | `INTACT` | touched but content identical |

---

# Part III — Traversal semantics

One shared walker drives **every** mode (stamp, verify, remove, import, locate, and the
prescan counting pass), so all modes agree on order and on what is included.

- **REQ-WALK-1** (deterministic order) — Within each directory, entries are processed in
  ascending **case-insensitive** order: the sort key is `(name.casefold(), name)`, so the
  raw name breaks case-only ties (`README` vs `readme`) deterministically. Files in a
  directory are yielded first, then subdirectories are recursed into, both in that order
  (the `os.walk` top-down shape). Multiple roots are processed in the order given on the
  command line.
- **REQ-WALK-2** (`@sumtag-ignore` marker) — If a directory directly contains a file named
  `@sumtag-ignore`, that directory and its **entire subtree** are pruned: not descended,
  and none of its own files are yielded. This is a traversal-level exclusion and applies in
  every mode.
- **REQ-WALK-3** — The marker file itself is never yielded (never hashed/stamped), even in a
  directory that is otherwise processed.
- **REQ-WALK-4** — The marker **beats `--force`**: `--force` governs how a *visited* file's
  re-hash is decided, not *whether* a subtree is visited. A fenced subtree is untouched even
  under `-f`.
- **REQ-WALK-5** — `--no-ignore` disables all markers for the run (everything is processed).
  This is the intended override, not a weakening of `--force`.
- **REQ-WALK-6** — A marker on an **explicit scan root** is honored (the root is skipped),
  but emits a warning to stderr: `sumtag: <root>: @sumtag-ignore on scan root; skipping`.
- **REQ-WALK-7** (`--exclude PATTERN`) — Skips anything whose **basename** matches the glob
  `PATTERN`, matched **case-sensitively** on every platform (`fnmatch.fnmatchcase`).
  Repeatable; a name matching *any* pattern is excluded. A matching **directory** is pruned
  like a marker (not descended, not announced). A pattern containing `/` can never match a
  basename and so excludes nothing.
- **REQ-WALK-8** — `--exclude` is independent of `--no-ignore` (which governs markers only)
  and applies in every mode; `--force` does not override it. A scan **root** whose basename
  matches is skipped with a warning:
  `sumtag: <root>: matches --exclude '<pat>' on scan root; skipping`.
- **REQ-WALK-9** (symlinks) — A symlink is **never yielded**, in any mode, silently (no
  output even at `-v`). Broken and live links alike are skipped at the walker. Symlinks to
  directories are never descended.
- **REQ-WALK-10** — An explicit scan-root argument that *is* a symlink (or a plain file) is
  still honored: a root that is a file is yielded directly; naming a symlink root is the
  user's explicit claim. Only links *encountered during* traversal are skipped.
- **REQ-WALK-11** — The walker announces each **visited** directory via a callback (used by
  `--prescan` to print directory lines) *after* the prune check and *before* yielding any of
  its files. Pruned directories and file-roots draw no announcement.

---

# Part IV — CLI grammar, conflicts, exit codes

## Invocation and required arguments

- **REQ-CLI-1** — At least one directory argument is required (`nargs="+"`). There is **no
  current-directory default**; to scan the cwd, pass `.` explicitly. This guards against
  firing a recursive operation at whatever directory you happen to be in.
- **REQ-CLI-2** (mandatory action) — Exactly one *category* of action must be named. A run
  that names none of `--sum`, `--verify`, `--remove`, `--import`, `--locate`,
  `--prune-dirs`, `--prune-all` is a usage error:
  `an action is required: --sum, --verify, --remove, --import, --locate, --prune-dirs, or --prune-all`.
  (`--prescan`/`--db-prescan` are modifiers, not actions, so a bare
  `sumtag --prescan /d` fails this rule automatically.)

## Full flag list

| Flag | Short | Kind | Meaning |
|---|---|---|---|
| `directories` | | positional (1+) | trees to scan; `.` for cwd |
| `--dry-run` | `-n` | bool | report only; write nothing anywhere |
| `--quiet` | `-q` | count | `-q` suppress normal output; `-qq` also suppress stderr |
| `--verbose` | `-v` | count | `-v` show per-decision detail; `-vv` deep internals |
| `--progress` | | bool | live within-file progress bar |
| `--si` | | bool | decimal (SI) size/rate units instead of binary |
| `--force` | `-f` | bool | re-hash every file, ignoring existing metadata |
| `--database` | | str | database sink (SQLite path or `scheme://` DSN) |
| `--sum` | | bool | stamping action (compute per mtime decision, write xattr) |
| `--import` | | bool | copy existing xattr metadata to db without computing |
| `--verify` | | bool | recompute & compare (read-only) |
| `--remove` | | bool | strip the `user.sumtag` xattr |
| `--prune-dirs` | | bool | delete db rows of directories that no longer exist |
| `--prune-all` | | bool | `--prune-dirs` plus per-file staleness |
| `--prescan` | | bool | count first, then show nnn/mmm + bytes counters |
| `--db-prescan` | | bool | like `--prescan` but read totals from the stored summary |
| `--no-ignore` | | bool | disregard all `@sumtag-ignore` markers |
| `--exclude` | | str (repeat) | prune basenames matching a glob |
| `--locate` | | bool | stat every file into the db; implies `--import` |
| `--version` | | action | print `sumtag <version>` and exit 0 |

- **REQ-CLI-3** — Short forms exist only for `-n`, `-q`, `-v`, `-f`. Everything else is
  long-only by design. `-q`/`-v` use counting, so `-vv` and `-v -v` are equivalent.
- **REQ-CLI-3a** — `--version` prints `sumtag <version>` (argparse `action="version"`) and
  exits 0. The positional-directory requirement (REQ-CLI-1) does **not** apply to
  `--version` (nor to `--help`), since argparse handles them before that check.

> **Note on output whitespace.** The status-line *labels, verbs, and content* (Part VI) are
> the contract; the exact column-padding/alignment spaces within a line are cosmetic and
> not guaranteed identical across implementations. The conformance oracle asserts on xattr
> bytes, database rows, and exit codes — not on inter-field spacing.

## Conflict matrix

Every rule below is enforced at parse time and produces a usage error (nonzero exit from
argparse), *except* the `--progress`/`-q` rule (REQ-CLI-14), which is resolved by order.

- **REQ-CLI-4** — `-q` and `-v` together → error (`mutually exclusive`).
- **REQ-CLI-5** — `--force` and `--dry-run` together → error (`mutually exclusive`).
- **REQ-CLI-6** — `--import` requires `--database`; `--locate` requires `--database`.
- **REQ-CLI-7** — `--database` requires at least one of `--sum`, `--import`, `--locate`,
  `--prune-dirs`, `--prune-all` (naming a database with no action to take on it is an
  error). `--sum` does **not** require `--database` (alone it stamps xattrs only).
- **REQ-CLI-8** — `--sum`, `--import`, `--locate` are parallel and freely combinable.
  `--sum --import` is redundant but allowed. `--locate` **implies** `--import`.
  `--force --import` and `--force --locate` are **allowed** (force overrides import's
  refusal to compute).
- **REQ-CLI-9** — `--verify` conflicts with each of `--database`, `--sum`, `--import`,
  `--locate`, `--force` (individually reported). `--verify -n` is a redundant no-op and is
  **allowed**.
- **REQ-CLI-10** — `--remove` conflicts with each of `--database`, `--sum`, `--import`,
  `--locate`, `--verify`, `--force`. `--remove -n` is **allowed** (the preview).
- **REQ-CLI-11** — `--prune-dirs` / `--prune-all` each require `--database`; each conflicts
  with `--sum`, `--import`, `--locate`, `--verify`, `--remove` (one run, one mode), with
  `--force` (no re-hash decision to override), with `--prescan`/`--db-prescan` (nothing is
  checksummed; the prune counter is built in), and with `--exclude`/`--no-ignore` (they
  walk the database, not the filesystem). `-n` is allowed. Giving **both** prune flags is
  redundant but allowed (`--prune-all` subsumes `--prune-dirs`).
- **REQ-CLI-12** — `--prescan` conflicts with `--remove` (nothing to count).
- **REQ-CLI-13** — `--db-prescan` requires `--database`, conflicts with `--prescan` (two
  sources for one counter) and `--remove`. It cannot combine with `--verify` because
  `--verify` conflicts with `--database` outright.
- **REQ-CLI-14** (`--progress` vs `-q`) — Not rejected. Whichever appears **later on the
  command line** wins outright (the loser's flag is discarded), and a warning is printed:
  `sumtag: --progress overrides -q (appears later on the command line)` or the symmetric
  `-q overrides --progress …`. Bundled short clusters containing `q` (e.g. `-fq`) count as
  a `-q` occurrence at their position.

## Exit codes

- **REQ-EXIT-1** — `0` = normal success / all verified intact / nothing stale.
- **REQ-EXIT-2** — `1` = `--verify` found corruption (≥1 mismatch), **or** a prune found
  (or, under `-n`, would have found) stale rows to delete.
- **REQ-EXIT-3** — `2` = unreadable files or other errors prevented a complete run
  (including `--verify` `UNVERIFIABLE` files, and `--db-prescan` startup errors).
- **REQ-EXIT-4** — `130` = the run was interrupted by Ctrl-C (128 + SIGINT). It prints the
  normal summary (Part VI), not a traceback.
- **REQ-EXIT-5** — The entry point returns the code as an `int`; installed command and
  `python3 -m sumtag` behave identically.

---

# Part V — Per-mode behavior

Common to all scanning modes: iterate files via the shared walker (Part III); each file is
`os.stat`'d, its live mtime formatted (REQ-TIME-2), and its xattr read+parsed
(REQ-SCHEMA-6). An `OSError` on a file increments the error count, prints
`sumtag: <path>: <error>` to stderr, and continues; the run's exit becomes 2.

## `--sum` (and import/locate) — the stamp pass

- **REQ-SUM-1** — For each file, compute the re-hash decision. Which decision function
  applies depends on the flags:
  - With `--sum`: the standard `should_rehash` decision (Part II).
  - With only `--import`/`--locate` (no `--sum`): do **not** compute unless `--force` — the
    "decision" is `force` (reason `forced`) else no-compute (reason
    `not computing (--import/--locate only)`).
- **REQ-SUM-2** (re-hash branch) — When the decision says re-hash and not `-n`: announce the
  file (Part VI), stream-hash it, build a fresh metadata document
  (`digests={xxh3: <digest>}`, `file_mtime=<live>`, `hashed_at=now`,
  `run_started_at=<run start>`), and write the xattr. Increment `hashed` count and
  `hashed_bytes` by the file size. Under `-n`, announce `would hash …` and write nothing.
- **REQ-SUM-3** (skip / import branch) — When the decision says do-not-rehash **and** the
  file already has usable digests:
  - In `--sum` mode: it is a **skip** (reason `up-to-date`), counted as `skipped`, shown
    only at `-v` (REQ-OUT-3).
  - In import/locate-only mode: it is an **import** — announce `import <path>`, increment
    `imported` and `imported_bytes`, and mirror the existing metadata verbatim (no compute).
- **REQ-SUM-4** (no metadata, not re-hashing) — A file with no usable digests that is not
  being hashed (import/locate-only mode) is reported `skip (no metadata) <path>` and counted
  `skipped`.
- **REQ-SUM-5** (mirroring) — When `--database` is set and not `-n`: mirror in **addition**
  to the xattr. Both freshly-hashed and pre-existing metadata are mirrored (so the database
  reflects the whole tree, not just changed files). Mirroring writes one row per
  `(algo, digest)` in the map (today one). Stat columns are written only with `--locate`.
- **REQ-SUM-6** (`--locate` stat capture) — With `--locate`, every visited file is `stat`'d
  and its locate columns written. A file that has usable digests gets a full mirror + stat;
  a file with **no** digests still gets a stat-only `update_stat` (a no-op if it has no row).
  Stat columns include `birthtime` only on macOS.
- **REQ-SUM-7** (`--sum` without `--locate`) — Locate columns stay NULL.
- **REQ-SUM-8** (`--locate` backfill) — A row previously written by a plain `--sum` (locate
  columns NULL) gets those columns filled by a later `--locate` run **without disturbing the
  digest** (the COALESCE contract, REQ-DB-5).
- **REQ-SUM-9** (`--import` copies, never computes) — Prove-by-construction: if an xattr
  holds a *wrong* digest, `--import` mirrors that wrong digest verbatim and never rewrites
  the xattr. `--import` on a file lacking a usable xattr skips it (no row written).
- **REQ-SUM-10** (`-n` composition) — `--database --import -n` previews and writes nothing —
  **not even creating the database file**. In general a database is opened only when there is
  something to write (never under `-n`).

## `--verify`

- **REQ-VERIFY-5** — Read-only. Recompute each file's digest(s) and classify (Part II).
  Never write an xattr or a db row, **especially not on a mismatch** (silently "healing"
  would destroy the evidence of corruption).
- **REQ-VERIFY-6** — A file with no usable xattr (or empty digests) is reported
  `unverifiable <path>`, counted `unverifiable`, and marks the run as having an error
  (contributes to exit 2). This replaces the announcement outright (no read attempted).
- **REQ-VERIFY-7** — A file that is read gets announced (`verify <path>` at `-v`, bare path
  otherwise) *before* the read. Outcomes: `INTACT` prints nothing further (silence = good);
  `CORRUPTION` prints `CORRUPT <path>` and counts `corrupt`; `STALE` prints
  `stale  <path> (modified since hash; restamp needed)` and counts `stale`.
- **REQ-VERIFY-8** — Exit: `1` if any corruption; else `2` if any unverifiable/error; else
  `0`.

## `--remove`

- **REQ-REMOVE-1** — Strip the `user.sumtag` xattr from every walked file. A
  testing/reset utility; no mtime logic, nothing computed.
- **REQ-REMOVE-2** — A file that had the xattr: announce `remove <path>` (bare path without
  `-v`), count `removed`. A file without it: `skip <path> (no metadata)` at `-v` only, count
  `skipped`. Under `-n`: `would remove <path>` for present, `skip … (no metadata)` for
  absent; nothing deleted.
- **REQ-REMOVE-3** — Exit 2 if any `OSError` occurred, else 0.

## `--prune-dirs` / `--prune-all` (database reconciliation)

These walk the **database**, not the filesystem, to delete rows for directories/files that
no longer exist.

- **REQ-PRUNE-1** (unmounted-drive guard, part 1) — Every scan root must exist as a
  directory. If any root is missing, print
  `sumtag: <root>: no such directory (nothing pruned; is the drive mounted?)` and exit **2
  before the database is even opened** (an absent drive must never read as a mass deletion).
- **REQ-PRUNE-2** — Open the database `rw` (or `ro` under `-n`). A **missing** database is an
  error (never created): `sumtag: no such database: <path>`, exit 2.
- **REQ-PRUNE-3** (candidate directories) — For each root, compute its **live** mount +
  rel-prefix (same `statfs`/mount machinery as stamping) and collect from the database the
  distinct directories (dirnames of `files.rel_path`) under that prefix, each with its
  resident file-row count. Overlapping roots must not double-count a directory. A root that
  matches **no** rows draws a warning
  `sumtag: <root>: no database rows under this root (mounted at <mount>); nothing to check`
  and contributes nothing (a drive mounted somewhere unexpected simply matches no rows).
- **REQ-PRUNE-4** (directory check) — For each candidate directory (sorted), `stat` it. It
  is "gone" if it does not exist or is no longer a directory. A surviving directory is a
  `-v`-gated `skip   <path> (exists)`. A gone directory: under `-n`,
  `would prune <path> (<N> file rows)`; live, delete its **resident** rows (dirname
  equality, never recursive) and print `prune  <path> (<N> file rows)`. Count `pruned_dirs`
  and add the rows to `pruned_files`; count `checked_dirs`.
- **REQ-PRUNE-5** (no recursion) — Deletion is directory-resident only. A vanished parent's
  vanished children are discovered by their **own** checks (every directory that held files
  is independently in the candidate list), so a gone directory costs exactly one `stat`.
- **REQ-PRUNE-6** (`--prune-all` per-file pass) — For a **surviving** directory, additionally
  `lstat` each resident file row. A row is stale if its path does not exist or is no longer a
  **regular** file (turned into a dir or symlink). Delete stale rows; announce
  `prune  <path> (file row)` / `would prune <path> (file row)`; count `checked_files` and add
  to `pruned_files`. The directory pass runs first, so a gone directory never costs one
  `lstat` per row.
- **REQ-PRUNE-7** (commits) — Deletes are committed **per directory**, so an interrupted run
  keeps the prunes it completed.
- **REQ-PRUNE-8** (never touches the filesystem) — No file or xattr is ever modified; this is
  maintenance *of* the sink toward filesystem truth. The sink-never-a-source principle holds.
- **REQ-PRUNE-9** (exit codes) — `0` = nothing stale (database already matches); `1` = stale
  found and pruned (or would-prune under `-n`); `2` = errors prevented a complete check.
- **REQ-PRUNE-10** (progress unit) — With `--progress`, a live counter shows directories (or,
  under `--prune-all`, directories + file rows as `paths`) checked out of the total known at
  start (Part VI / CountIndicator).

---

# Part VI — Output contracts

## Status lines

- **REQ-OUT-1** (announcement, the pre-read line) — For any file about to be checksummed, a
  line prints **before** the read. **Without `-v`, it is the bare path and nothing else.**
  With `-v`, it expands to the action form using present/imperative verbs:
  `hash <path> (<reason>)`, `verify <path>`, `import <path>`, or under `-n`
  `would hash <path> (<reason>)`. This bare-path/`-v` split applies to every routine
  per-file announcement (stamp, dry-run, import, and the no-metadata report).
- **REQ-OUT-2** (deviation lines are unconditional) — `CORRUPT <path>`,
  `stale  <path> (modified since hash; restamp needed)`, and `unverifiable <path>` print
  with their labels **regardless of `-v`** (they are alarms, not routine detail). Errors go
  to stderr as `sumtag: <path>: <error>`; the file is skipped and the run continues.
- **REQ-OUT-3** (skips are `-v`-only) — `skip   <path> (<reason>)` for an up-to-date file
  prints only at `-v`, with no bare-path line either. A repeat run over an already-stamped
  archive is therefore **completely silent** by default.
- **REQ-OUT-4** (quiet) — `-q` suppresses all normal (stdout) output, including the summary.
  `-qq` also suppresses stderr errors.

## Run summary block

- **REQ-OUT-5** — Every run ends with an aligned `label: value` block on stdout (so `-q`
  suppresses it). Example:

  ```
  hashed:   42 files, 1.3GiB
  database: /var/db/cb.sqlite
  scanned:  /backup, /data
  ```

- **REQ-OUT-6** (headline) — The first line names what the mode did, with count and
  cumulative byte size (byte figures honor `--si`): `hashed` (stamp), `would hash` (`-n`),
  `imported` (import/locate-only), `verified` (`--verify`), `removed: N stamps` (`--remove`),
  `pruned:`/`would prune:` with dir and file-row counts (prune). The headline **always
  prints, even at zero**. A run that both hashed and imported shows both lines (the zero one
  is dropped). Prune adds a `checked:` line (dirs, plus files under `--prune-all`).
- **REQ-OUT-7** (deviation counts) — `skipped`, `errors`, and `--verify`'s `CORRUPT` /
  `stale` / `unverifiable` tallies print **only when nonzero**.
- **REQ-OUT-8** — `database:` appears when `--database` was given; `scanned:` always closes
  the block, listing roots exactly as given on the command line. Labels are padded so the
  colons align.
- **REQ-OUT-9** (Ctrl-C) — On `KeyboardInterrupt`, the run stops, any live progress bar is
  cleared, `interrupted` is printed, then the identical summary block, then exit 130.
- **REQ-OUT-10** (honest counters) — Counters count only **completed** work: a file whose
  hash was cut off mid-read is not claimed. The interrupted summary is an honest statement of
  how far the run got.

## `--progress` within-file indicator

- **REQ-PROG-1** (trigger) — Triggered by *time*, not size: once a single file's checksum has
  run **> 2 seconds**, a live line appears on stderr, redrawn in place (throttled to a few
  updates/second) and cleared the moment that file's hash completes. A file finishing under
  the threshold shows nothing.
- **REQ-PROG-2** (independent of `-v`) — `--progress` is orthogonal to verbosity. With both,
  the per-file announcement prints first, then the bar appears if the file is slow.
- **REQ-PROG-3** (suppressed off-tty) — Suppressed entirely when stderr is not a terminal.
- **REQ-PROG-4** (line format) — Fields, left to right, fixed widths except the bar:

  ```
  {size:>8}  {rate:>10}  [{bar}] {pct:>3}%  {elapsed:>8}  ETA {eta:<7}
  ```

  - **size** — file total, human-readable (binary by default, SI with `--si`).
  - **rate** — current throughput, same units, `/s`.
  - **bar** — pv-style: `=` fill, `>` leading edge, spaces remainder, in `[...]`; the bar
    absorbs all remaining width.
  - **pct** — integer percent, outside the bar.
  - **elapsed** — `H:MM:SS`.
  - **eta** — literal `ETA ` then a compact duration (`45s`, `5m12s`, `1h05m`), styled
    distinctly from elapsed. `--` (or `?`) when no rate yet.
- **REQ-PROG-5** (resize) — On `SIGWINCH` the width is re-measured on the next redraw and the
  bar re-sized to fill; no other field's layout changes. When width is unknown, or off a
  platform with `SIGWINCH`, fall back to an 80-column budget.
- **REQ-PROG-6** (width clamp) — Every redraw is hard-clamped to the terminal width before
  writing (`\r` + `line[:width]` + erase-to-EOL), so an over-wide line can never wrap and
  strand the bar's first row. The clear runs in a `finally`, so a file whose read fails
  mid-hash still clears the bar before the error prints — in both stamp and verify.
- **REQ-PROG-7** (`human_size`) — Binary units `B/KiB/MiB/GiB/TiB/PiB` (base 1024) by
  default; SI units `B/kB/MB/GB/TB/PB` (base 1000) with `--si`. Bytes (`idx==0`) render as an
  integer with no decimal; otherwise one decimal, **except** a value ≥ 1000 in its unit (the
  1000–1023.9 binary band) drops the decimal to avoid overflowing the fixed field.
- **REQ-PROG-8** (CountIndicator) — `--prune-dirs`/`--prune-all` and `dbmerge` use a
  whole-pass counter instead: `nnn/mmm <unit>  [bar] pct%  elapsed  ETA eta`, `nnn`
  zero-padded to `mmm`'s width; same 2-second trigger, off-tty suppression, and clear rules.
  Unit is `dirs` / `paths` (prune) or `rows` (dbmerge). A normal output line calls
  `interrupt()` first so the two never collide.

## `--prescan` counter prefix

- **REQ-PRESCAN-1** — `--prescan` walks the tree once up front, purely to count the files the
  run will checksum and their total size, then prefixes each hash/verify announcement:

  ```
  nnn/mmm (pp%)  bytes-so-far/bytes-total (pp%)  <the usual announcement>
  ```

- **REQ-PRESCAN-2** — `nnn` is this file's ordinal among files being checksummed, zero-padded
  to `mmm`'s width. `mmm` is the up-front total. `bytes-so-far` is the sum of sizes of files
  **already completed** (so `0B` on the first file); it is per-file, printed once before each
  file's read (not a live in-file counter). Byte figures honor `--si`.
- **REQ-PRESCAN-3** — Each fraction is followed by its whole-number percentage in parens,
  right-padded to three digits (`(  0%)`..`(100%)`), so columns never jitter. A zero total
  reads as `(100%)`.
- **REQ-PRESCAN-4** (what counts) — The counted set mirrors the mode: for the stamp pass, the
  files the re-hash decision (or `--force`) will hash; for `--verify`, every file with a
  usable stored digest (the ones that will be read, i.e. not `unverifiable`). The counting
  pass uses the **same** decision function as the real pass, so the counter lines up. Skips,
  imports, and no-metadata files get no prefix (they were never "the line before summing a
  file").
- **REQ-PRESCAN-5** (directory announcements) — The prescan walk announces each visited
  directory (bare path, or `prescan <path>` at `-v`; suppressed by `-q`), giving the
  otherwise-silent counting pass a sign of life. Pruned directories are not announced.
  Traversal **warnings** the real pass will print (e.g. a root's own marker) are suppressed
  during prescan so nothing is warned twice; errors are likewise swallowed (the real pass
  hits them).
- **REQ-PRESCAN-6** (drift) — The counts are a prediction. If the tree changes between the
  prescan and the real pass, `nnn/mmm` can drift; nothing about hashing, stamping, or exit
  codes depends on it. `--prescan` conflicts with `--remove` (nothing to count).
- **REQ-PRESCAN-7** (persist for `--db-prescan`) — On a `--database` run and **not** under
  `-n`, `--prescan` additionally stores its totals as the database's one-row prescan summary:
  file count, byte total, the normalized scan roots, and the full counting context (`--sum`
  mode, `--force`, `--exclude` patterns, `--no-ignore`) plus a timestamp, replacing any
  previous summary.

## `--db-prescan`

- **REQ-PRESCAN-8** — `--db-prescan` reads `mmm`/bytes-total from the stored summary instead
  of walking the filesystem (seconds instead of an hour on a huge tree). The real pass runs
  exactly as normal; only the two counter totals come from the database.
- **REQ-PRESCAN-9** (display-only) — The stored data never influences the summing pass:
  every per-file mtime decision is made the normal way; nothing is skipped or trusted based
  on stored totals (sink-never-a-source). `nnn` counts what actually happens this run, so it
  may overshoot `mmm` or finish below it; the percentage may pass 100% (the line simply
  widens by a character — harmless in an appended log).
- **REQ-PRESCAN-10** (match-or-error) — The stored summary is used **only** if it answers
  this run's question: the scan roots (compared as sets of normalized absolute paths, so
  `/data` vs `/data/` vs a relative respelling never falsely mismatch) and the full counting
  context (`--sum`, `--force`, `--exclude`, `--no-ignore`) must all equal this run's. The
  check runs **before** the store is opened, so nothing (not even the database file) is
  created on failure. Failures (exit 2):
  - missing summary → `sumtag: <db>: no stored prescan totals; run --prescan --database first`
  - mismatch → `sumtag: <db>: stored prescan totals do not match this run (<what differs>); run --prescan --database to refresh them`, where `<what differs>` is one of `different scan roots`, `different action`, `--force differs`, `--exclude patterns differ`, `--no-ignore differs`.
- **REQ-PRESCAN-11** (announced) — On success, print on stdout (`-q` suppresses; byte figure
  honors `--si`): `using stored prescan totals: <N> files, <size>, from <timestamp>`.
- **REQ-PRESCAN-12** (composition) — Requires `--database`; conflicts with `--prescan` and
  `--remove`; cannot combine with `--verify` (verify conflicts with `--database`). `-n`
  composes: the summary is read (read-only open, never creating the file) and the counters
  display, while `-n` separately keeps `--prescan` from persisting.

---

# Part VII — Companion programs

These ship inside the `sumtag` package with their own `console_scripts` entry points
(`grouper`, `dedupe`, `dbmerge`). They consume the sumtag database and **never compute a
hash**. They are experimental in status but fully specified here. Development equivalents:
`python3 -m sumtag.grouper`, etc.

## grouper — find groups of similar directories

`grouper` reads sumtag's `files`/`mountpoints` tables and owns six derived tables in the
same database file: `dirs`, `dir_files` (the directory index); `dir_pairs` (pairwise
similarity); `groups`, `group_dirs` (the persisted partition); `grouper_meta` (provenance).

### CLI surface

- **REQ-GROUPER-1** — `--database DB` is **required**. With no action flags, grouper prints
  the stored grouping (the report). Actions/flags:
  `--prep` (= `--index` + `--pairs`), `--index`, `--pairs`, `--threshold X`, `--fn NAME`,
  `--jobs N` (default: all CPUs), `--progress`, `--max-df N`, `--min-sim X`,
  `--no-junk-filter`, `--sort {bond,files,size,tree-size}` (default `bond`), `--name`,
  `--ls DIR`, `--compare A B`, `--dupes`, `--top [N]`, `--min N` (default 2), `--clean-db`.
- **REQ-GROUPER-2** (arg validation, all usage errors) — `--threshold` must be in `[0,1]`;
  `--jobs` ≥ 1; `--max-df` is 0 or positive; `--min-sim` in `[0,1]`; `--dupes` and `--top`
  are mutually exclusive; `--top` ≥ 1; `--clean-db` cannot combine with any other action.
- **REQ-GROUPER-3** — A missing database is a hard error: `grouper: no such database: <path>`
  (exit via `sys.exit`). A writable connection (index/pairs/threshold/clean-db) uses a 600 s
  busy timeout; read-only uses 5 s. Errors from the build stages surface as `grouper: <msg>`
  on stderr with exit 1 (raised as `LookupError`).
- **REQ-GROUPER-4** (dispatch order in one invocation) — `--prep` sets index+pairs.
  `--clean-db` runs alone and returns. Otherwise: run `--index`, then `--pairs`, then (if
  `--threshold` given and the stored grouping is not already current) `--threshold`; then
  the first of `--ls` / `--compare` / `--dupes` / `--top` present returns; else if
  `--threshold` was given or nothing was built, print the group report.

### Stage 1: `--index`

- **REQ-GROUPER-5** — Drop-and-rebuild `dirs`/`dir_files` from `files`. `dirs` holds every
  distinct directory that directly contains ≥1 indexed file; `dir_files` maps `files.rowid →
  dir_id` (indexed). Comparisons are direct-children only (a directory's own file listing is
  its signature). `dirs.rel_path` `''` is the mount point itself.
- **REQ-GROUPER-6** (two junk filters, both on by default) — (a) a directory whose
  mount-relative path has **any component starting with `.`** (`.git/hooks`,
  `.build/checkouts/x`, anything under `.Trash-*`) is never indexed; (b) a directory whose
  direct files are **all hidden** (every basename starts with `.`) is dropped. A kept
  directory keeps *all* its files, hidden ones included (the filters decide which
  *directories* exist, not which files count). Each filter reports its dropped count on
  stderr: `grouper: dropped <N> junk directorie(s) (hidden path component)` and
  `grouper: dropped <N> directorie(s) with no visible files`.
- **REQ-GROUPER-7** — `--no-junk-filter` disables **both** filters for the run.
- **REQ-GROUPER-8** (provenance, tombstone-first) — The index regime is recorded in
  `grouper_meta` as an `index` row (with a `no_junk` column). The row is **deleted and
  committed before** the table surgery, and rewritten only on success, so a partial index
  can never wear valid provenance. Old databases have `max_df`/`min_sim`/`no_junk`/
  `name_match` columns added in place if missing.

### Stage 2: `--pairs`

- **REQ-GROUPER-9** — Requires the index (`grouper: directory index missing; run --index
  first` if absent). Compare directories with one named comparison function and store all
  kept `(dir_a<dir_b, similarity, matched)` rows in `dir_pairs`, indexed by the grouping
  walk's order `(similarity DESC, matched DESC, dir_a, dir_b)`.
- **REQ-GROUPER-10** (comparison functions) — Registered by name as `(signature, score)`
  pairs; `signature(files)` is built **once per directory**, `score(sig_a, sig_b)` returns a
  `(similarity, matched)` tuple. `matched` is the count of files in common under that
  function's notion of matching. The three functions:
  - `digest` — `signature` = multiset (`Counter`) of `(algo, digest)`; `score` = multiset
    Jaccard `|A∩B|/|A∪B|` (empty-vs-empty = 1.0); `matched` = intersection size. Renames are
    free (names not in the signature).
  - `name-digest` — signature = multiset of `(basename, algo, digest)`; same Jaccard scorer.
    All-or-nothing per `(name, digest)`; a rename matches nothing.
  - `name-score` (**default**) — signature = dict `basename → (algo, digest)`; score is
    name-anchored partial credit: 1 point per shared basename, +2 more if the `(algo,digest)`
    also matches; `similarity = points / (3 * max(|A|,|B|))`; `matched` = shared-basename
    count. Two empty dirs score 1.0.
- **REQ-GROUPER-11** (nomination invariant) — Every comparison function must score **0** for
  key-disjoint signatures (no shared signature key). This is what makes `--max-df` nomination
  lossless; a new function violating it would silently break nomination.
- **REQ-GROUPER-12** (what is stored) — Only pairs with `similarity > 0` **and**
  `similarity ≥ min_sim` are stored (zero-similarity pairs can never group).
- **REQ-GROUPER-13** (`--max-df` candidate nomination) — Exhaustive all-pairs is used up to
  `_EXHAUSTIVE_MAX = 50_000_000` comparisons; above that (or with explicit `--max-df N`,
  default cap `1000`), switch to nomination: build an inverted index of signature keys and
  score only pairs sharing at least one key present in **2..N** directories. Nominated pairs
  get **exact** scores from full signatures (the cap governs *who* is looked at, never *what*
  a look sees). `--max-df 0` forces exhaustive at any size. Candidate mode is announced:
  `grouper: candidate nomination, max-df <N> (pairs sharing only commoner tokens are not
  scored)`, and the cap is recorded in `grouper_meta`.
- **REQ-GROUPER-14** (`--min-sim` storage floor) — `--min-sim X` keeps only pairs scoring
  ≥ X. The floor governs what is *kept*, not what is *scored*. Recorded in `grouper_meta`; a
  later `--threshold` below the stored floor is refused (REQ-GROUPER-19).
- **REQ-GROUPER-15** (`--name` gate) — With `--name`, two directories whose **basenames
  differ** score 0 unconditionally (filtered before scoring, so kept scores stay exact and
  the nomination invariant holds). Applies to `--pairs` and `--compare`. Recorded in
  `grouper_meta` (`name_match`). The mount root (basename `''`) matches only another root.
- **REQ-GROUPER-16** (parallelism) — `--pairs` fans scoring across `--jobs` worker processes
  (default all CPUs; used only when `jobs > 1` and total ≥ 500_000, else serial). Each worker
  loads its **own** signature table from the database over a read-only connection — nothing
  large is pickled across the spawn pipe — so every worker holds a full copy in RAM (`--jobs`
  is a memory knob as much as speed). Only the parent writes (SQLite single-writer). The
  triangular loop is dealt in interleaved stripes (`jobs*8`). The **stored pair set is
  identical at any `--jobs`**; only insertion order varies, which nothing depends on. The run
  uses WAL journal mode during parallel scoring (restored to DELETE on success; an
  interrupted run leaving WAL is harmless).
- **REQ-GROUPER-17** (incremental commit + tombstone) — Inserts commit incrementally (per
  stripe / ~1M rows), so an interrupted `--pairs` keeps completed inserts. The `pairs`
  provenance row is deleted and committed **before** any table surgery and rewritten only on
  success, so `--threshold` refuses a partial table with `no stored pairs` until a `--pairs`
  run completes.

### Stage 3: `--threshold X` and the report

- **REQ-GROUPER-18** (grouping walk) — Read stored pairs with `similarity ≥ X` in
  `(similarity DESC, matched DESC, dir_a, dir_b)` order and partition: if both dirs already
  placed → skip (no merging, ever); if neither → mint a new group id and place both; if
  exactly one → the other joins its group. Persist to `groups`/`group_dirs`
  (`group_dirs.dir_id` is the PRIMARY KEY, so one-group-per-directory is enforced by the
  schema). No group can have a single member. Deterministic for a given database. Record a
  `groups` provenance row (fn + threshold).
- **REQ-GROUPER-19** (refusals) — `build_groups` raises `LookupError` (→ `grouper: <msg>`,
  exit 1) when: no stored pairs/`dir_pairs` (`no stored pairs; run --pairs first`); a
  requested `--fn` differs from the stored pairs' fn
  (`stored pairs were built with --fn <f>; rerun --pairs --fn <g> …`); the requested
  threshold is below the stored `--min-sim` floor
  (`stored pairs were built with --min-sim <x>; … rerun --pairs with --min-sim <y> or lower …`);
  or the pair table predates the `matched` column
  (`stored pairs predate the matched-file tie-break; rerun --pairs to rebuild them`).
- **REQ-GROUPER-20** (skip-if-current) — `--threshold` skips the rebuild when the stored
  grouping already reflects this threshold and fn **and** the pair table is not newer than
  the grouping (a stale grouping — pairs rebuilt after grouping — does not count as current).
- **REQ-GROUPER-21** (report) — Print `grouping: fn=<fn>  threshold=<t>  built <ts>`, then
  (if newer) the notes `note: pair table is newer than this grouping; rerun --threshold to
  refresh` and `note: directory index is newer than the pair table; rerun --pairs …`. For
  each group in `--sort` order, print
  `group <id>  (<M> directories[, <weight>][, best pair <b>])` followed by member paths
  indented, members alphabetical by path. Close with `<G> group(s), <D> directorie(s)`. A
  missing grouping prints `grouper: no stored grouping; run --pairs, then --threshold X`
  (exit 1).
- **REQ-GROUPER-22** (`--sort`) — `bond` (default): ascending group id (creation order =
  strongest bonds first). `files`/`size`: by each group's total stamped-file count / byte
  size over member directories' **direct children**, descending, group id as tiebreak.
  `tree-size`: like `size` but over each member's **entire subtree** (indexed range scan
  `rel_path >= dir+'/' AND rel_path < dir+'0'`), with a member nested under another member
  of the same group skipped so nothing double-counts; header weight says `in tree`. The stat
  sorts add to the header: `<n> files, <size>[ in tree], avg <size>/dir` (byte total ÷
  member count).
- **REQ-GROUPER-23** (missing sizes) — `size`/`tree-size` read the nullable
  `--locate`-populated `files.size`. If **some** files lack sizes, proceed with a stderr
  note `note: <u> of <n> files have no stored size (a sumtag --locate run fills it); size
  totals undercount`. If **all** do, refuse before printing any group:
  `grouper: no stored file sizes to sort by; run sumtag --locate --database ... first`
  (exit 1).
- **REQ-GROUPER-24** (best-pair) — Each group's header shows `best pair <b>` = the maximum
  stored similarity between any two of its members (present only if `dir_pairs` exists).

### Inspection helpers & cleanup

- **REQ-GROUPER-25** (`--ls DIR`) — Resolve `DIR` (absolute → sumtag's firmlink-aware
  `mount_relative`; else tried as a mount-relative path directly) to a `dirs` row and list
  its indexed files: header `<full path>  (<N> file(s))`, then per file
  `    <algo>:<digest>  <size>  <basename>` (size `?` if NULL), sorted by rel_path. Not
  indexed → `grouper: directory not in index: <path> (run --index?)` (exit 1).
- **REQ-GROUPER-26** (`--compare A B`) — Resolve both, print the similarity to 4 decimals
  (`0.7500`). With `--name`, differently-basenamed dirs print `0.0000` without scoring.
  A dir not in the index → the same not-in-index error (exit 1).
- **REQ-GROUPER-27** (`--dupes` / `--top`) — `--dupes` reports every `(algo, digest)` shared
  by ≥ `--min` files (default 2). `--top [N]` reports the N (default 1) most frequent
  digests, **excluding empty files** (matched by known empty-digest constants per algorithm,
  plus `size <> 0` where size is known). Output per group:
  `\n<algo>:<digest>  x<count>[  (<k> distinct inode(s) -- some are hard links)]` then
  `    [ino <inode>]  <full path>` per file; closes with `<G> group(s), <F> file(s) total`.
  Grouping is by `(algo, digest)` — cross-algorithm identical files do **not** group (the
  documented mixed-algorithm hazard), and the algo is surfaced so it is visible.
- **REQ-GROUPER-28** (`--clean-db`) — Drop all six grouper-owned tables and `VACUUM` (which
  is what returns the space). All-or-nothing (the artifacts are interdependent). Sumtag's own
  tables are never touched. Prints `dropped:   <tables>` and `reclaimed: <before> -> <after>
  bytes`; if none present, `grouper: no grouper tables present; nothing to clean`.
- **REQ-GROUPER-29** (`--progress`) — A live bar on the build stages (`--index`, `--pairs`),
  following sumtag's conventions but with **work-item** units and an a-priori denominator
  (N files for `--index`; N(N−1)/2 comparisons for exhaustive `--pairs`; under nomination,
  directories-completed since the pair count is unknown up front). Redraws are **time-based**
  (a few/second) so the bar keeps ticking while all workers are mid-stripe; `--pairs` shows
  its phases (files loaded, dirs signed, pairs scored). Suppressed off-tty.

## dedupe — delete duplicate files from a cull tree

`dedupe --database DB ACTUAL CULL` deletes every file in CULL whose digest duplicates a file
in the **corresponding** ACTUAL directory (regardless of name); directories emptied by this
are removed on the way up. ACTUAL is never modified. **It deletes files** — the dangerous
companion.

- **REQ-DEDUPE-1** (CLI) — `--database DB` (required), positional `ACTUAL` and `CULL`,
  `--delete` (arm), `--allow-mixed`, `-n`/`--offline`. `-n` with `--delete` is a usage error
  (`--delete conflicts with -n/--offline (a prediction cannot arm)`).
- **REQ-DEDUPE-2** (arming model) — A **bare** run is a full **preview**: every
  `would delete`/`would sweep`/`would rmdir` printed, nothing touched, database opened
  read-only. Deletion requires the explicit `--delete` flag (this deliberately inverts
  sumtag's `-n`: lethality is opt-in). Under `--delete`, deleted files' database rows are
  deleted in the same run, committed **per directory**.
- **REQ-DEDUPE-3** (synchronized flat walk) — Both trees are walked depth-first **in
  lockstep by relative path**; the walk never descends a subdirectory name that is not a real
  directory on **both** sides. Comparison/deletion are flat, one directory pair at a time:
  same relative directory required, same name **not** required. A cull file dies iff a
  same-`(algo,digest)` file sits in the corresponding ACTUAL directory. **All** copies die
  (three cull files matching one witness all go). Reorganized duplicates are out of scope by
  design.
- **REQ-DEDUPE-4** (empty-directory carve-out) — A cull-side subdirectory with **no real
  files anywhere beneath it** (only ignorables, qualifying symlinks, empty dirs) is swept
  even without an ACTUAL counterpart.
- **REQ-DEDUPE-5** (signpost invariants) — What survives in cull is always a signpost
  (unique/unknown/suspicious). The **cull root itself is never removed**, even when empty
  (placeholder + receipt).
- **REQ-DEDUPE-6** (three trust vetoes, zero content I/O) — Stored digests are trusted (no
  re-hashing). A candidate is vetoed unless all hold: (1) the database digest equals the
  file's own xattr digest, on **both** sides; (2) live mtime equals the xattr's `file_mtime`,
  on **both** sides; (3) the cull file's live **size** equals its witness's live size. A
  vetoed cull file is **kept with a warning** (`<path>: <reason>; kept` / `not used as a
  witness` for the actual side). Reasons: `xattr missing or unreadable`, `xattr and database
  disagree`, `modified since stamped`.
- **REQ-DEDUPE-7** (size mismatch = loud) — A digest match with **differing sizes** is a loud
  `<path>: MISMATCH: digest matches <witness> but live sizes differ (<a> vs <b>); kept`,
  counted as an error (exit 2), never a deletion.
- **REQ-DEDUPE-8** (ignorables) — `IGNORABLE_NAMES` (notably `.DS_Store`) is not content: it
  never blocks a directory's removal and is swept **only** when it is the last thing standing
  before the `rmdir`, never earlier.
- **REQ-DEDUPE-9** (symlinks) — Never followed or digest-matched. A symlink is sweepable at
  exit iff it is **relative** (target not starting with `/`) **and** points into the cull
  tree (checked against both abspath and realpath spellings of the root). An **absolute**
  symlink is never deletable wherever it points; any link escaping the cull tree blocks its
  directory and is never deleted.
- **REQ-DEDUPE-10** (`@sumtag-ignore` fence) — A cull directory containing the marker is
  fenced: not descended, nothing inside deleted/swept/removed, and it never empties (blocks
  its parent like any survivor). A marker on the **cull root** is honored with a warning
  (`<cull>: cull root contains @sumtag-ignore; nothing to do`) — the run does nothing.
  Actual-side markers need no handling.
- **REQ-DEDUPE-11** (unknown/invisible files) — Files the database does not know are
  invisible: never matched, never deleted, left blocking their directory ("so be it"),
  counted as `unknown`.
- **REQ-DEDUPE-12** (identity / hard-link safety, at the moment of deletion) — If
  `realpath(cull file) == realpath(witness)`, they are **one directory entry** reached
  through links: refused loudly (`<path>: same file as <w> (one directory entry reached
  through links); kept`, error, exit 2), since deleting "the copy" would delete the only
  name. Same inode+dev with **different** realpaths is a genuine **hard link**: safe to
  delete (the data keeps its ACTUAL-side name), noted with
  `<path>: hard link of <w>; deleting the name is safe …`.
- **REQ-DEDUPE-13** (safety checks, before any deletion) — ACTUAL and CULL must exist, be
  directories, and be **disjoint under realpath** (not equal, neither nested). The database
  must already exist (never created) and must **know both roots**: each root's live mount is
  recorded, with ≥1 file row under it — else
  `<root>: no database rows under this <ACTUAL|CULL> root (mounted at <m>); scan it with
  sumtag first`. A candidate set spanning more than one algorithm is refused without
  `--allow-mixed`: `mixed digest algorithms under these roots (<a>, <b>): … pass
  --allow-mixed to proceed anyway`.
- **REQ-DEDUPE-14** (path discipline) — The walk and row lookups use **abspath** (exactly how
  sumtag records rel_paths), never realpath or a symlinked component (macOS `/var` →
  `/private/var` would match zero rows). `realpath` is reserved for the safety checks. Roots
  must be spelled as sumtag scanned them; a wrong spelling fails the no-rows check with a
  clear error.
- **REQ-DEDUPE-15** (offline `-n`/`--offline`) — Predicts "what **might** be deleted" from the
  database alone — **no filesystem access**, so neither root need be mounted. Roots resolve
  against the **stored** mountpoints (both rel forms `store._relativize` can emit are tried
  lexically per mountpoint — the escaping `../../../var/...` form and the rooted rebase —
  with the database's own rows arbitrating which holds rows; a rowless root falls to its best
  guess and fails the no-rows check). The flat same-relative-directory match runs over rows;
  candidates print as `might delete <path>`. Everything the filesystem would contribute is
  skipped (the three vetoes, realpath/identity checks, fences, sweeps, rmdirs). Two row-only
  checks survive: the recorded-size collision check where `--locate` populated sizes on both
  sides (a recorded-size mismatch on a digest match is the same loud `MISMATCH`, exit 2), and
  a shared-recorded-inode note. The run is announced
  (`offline: predicting from database contents alone (trust vetoes and filesystem checks
  skipped)`), the database opens read-only, and the summary headline is `might delete:`
  (rows without sizes add `(+N of unknown size)` to the byte total). Surviving safety checks:
  root disjointness (on resolved mountpoint+rel identities), the no-rows refusal, and the
  mixed-algorithm refusal.
- **REQ-DEDUPE-16** (summary + exit) — The summary is the house `label: value` block:
  headline `deleted:` / `would delete:` / `might delete:` with count and human size; then
  `swept`/`would sweep`, `removed`/`would remove` (directories), `kept` (with a
  `unique`/`unknown`/`stale` breakdown), `fenced`, `errors` — each only when nonzero — then
  `database:`, `actual:`, `cull:`. Exit codes (house 0/1/2): `0` nothing redundant; `1`
  duplicates found (deleted or would-delete); `2` errors or a safety refusal. Ctrl-C prints
  the same summary and exits **130** (commits are per directory; counters count only
  completed work).

## dbmerge — combine per-volume databases into one

`dbmerge --database TARGET SOURCE...` folds each SOURCE (opened read-only, always) into
TARGET, so cross-volume `dedupe`/`grouper` runs get one database. The schema was always
multi-volume; one-db-per-volume is an operational choice for parallel scanning.

- **REQ-DBMERGE-1** (CLI) — `--database TARGET` (required), `SOURCE...` (1+), `-n`/
  `--dry-run`, `--progress`, `--allow-mixed`.
- **REQ-DBMERGE-2** (replace-per-mountpoint) — For each mountpoint present in a source, the
  target's existing rows for that mountpoint are **deleted** and the source's inserted fresh.
  This makes the merge idempotent and each source authoritative for its mountpoints —
  including an **empty** one (a source mountpoint with zero rows still clears the target's
  rows for it: authoritative emptiness). Target mountpoints **no source names** are left
  untouched (one volume can be re-merged without feeding all).
- **REQ-DBMERGE-3** (collision refusal) — The same mountpoint path recorded in **two sources**
  is an error: `mountpoint <p> is recorded in both <s1> and <s2>; sources must partition by
  mountpoint (the second replacement would delete the first's rows)`. (This also guards the
  "mount path is not a global identity" limitation.)
- **REQ-DBMERGE-4** (what never flows) — Only `mountpoints` and `files` flow. Grouper's
  derived tables are never copied, and any such artifacts already in the target are
  **dropped** with `drop grouper artifacts (stale after a merge; rerun grouper --prep)` (a
  merge that changed `files` invalidated them). The `prescan_summary` row is neither copied
  nor touched (it describes a walk, which the merge does not invalidate). Sources are never
  modified.
- **REQ-DBMERGE-5** (guards, before opening the target for writing) — Every source must exist
  and carry the sumtag tables (`<s>: not a sumtag database (no mountpoints/files tables)` /
  `<s>: no such database`); the target may not also be a source, nor a source given twice
  (checked under realpath: `<s>: the target database cannot also be a source` / `<s>: same
  database as source <first>`); the mountpoint collision (REQ-DBMERGE-3); and a merged corpus
  (sources + surviving target rows) spanning more than one `algo` is refused without
  `--allow-mixed` (`mixed digest algorithms across these databases (<a>, <b>): … pass
  --allow-mixed to proceed anyway`).
- **REQ-DBMERGE-6** (`-n`) — Previews everything (`would merge`/`would replace`/`would drop`,
  per-mountpoint row counts) with **no side effects**: the target is opened read-only if
  present and **not created** if absent.
- **REQ-DBMERGE-7** (progress) — `--progress` shows **one aggregate** rows-merged/rows-total
  bar across all sources (denominator = per-source `COUNT(*)` up front), via the house
  CountIndicator (unit `rows`), all the usual conventions.
- **REQ-DBMERGE-8** (commits + interrupt) — Commits are **per mountpoint**, so an interrupted
  run keeps completed mountpoints; counters count only committed work. Ctrl-C prints the
  normal summary and exits **130**.
- **REQ-DBMERGE-9** (summary + exit) — House block: `merge:` (rows, mountpoints, sources —
  prints even at zero), `replace:`/`drop:` when nonzero, `database:`, `sources:`. Exit codes
  (house 0/1/2, with one wrinkle): `1` (target modified — or would be, under `-n`) is the
  **normal** successful merge, because replacement always rewrites; `0` (nothing to do)
  occurs only for empty sources; `2` = errors or a refusal.

---

# Part VIII — Non-functional requirements

## Language, dependencies, platform

- **REQ-PKG-1** — Python 3.x, standard library preferred throughout. The only required
  third-party dependency is **`xxhash`** (XXH3 is not in the stdlib).
- **REQ-PLAT-1** — Targets macOS and Linux. Only the xattr read/write layer and the
  mount-point detection differ by platform; everything else is platform-neutral.
- **REQ-PLAT-2** (xattr layer) — A byte-oriented, platform-abstracted interface with
  `get(path, name) -> bytes | None` (absent attribute → `None`), `set(path, name, value)`,
  `remove(path, name) -> bool` (whether it was present). Linux uses stdlib
  `os.getxattr`/`os.setxattr`/`os.removexattr` (translating `ENODATA`/`ENOATTR` → absent).
  macOS binds `getxattr`/`setxattr`/`removexattr` from libSystem via `ctypes` (the extra
  `position`/`options` args are 0; `ENOATTR` = 93 → absent) — avoiding a second third-party
  dependency.
- **REQ-PLAT-3** (mount detection) — As in REQ-PATH-2/3: macOS `statfs(2)` via `ctypes`
  (preferring the `statfs$INODE64` symbol where present); Linux/other the `ismount` walk.
- **REQ-PLAT-4** (`birthtime`) — Captured only where `os.stat_result` exposes `st_birthtime`
  (macOS); NULL elsewhere.

## Packaging & invocation

- **REQ-PKG-2** — Installable package (setuptools via `pyproject.toml`), `name = "sumtag"`,
  `dependencies = ["xxhash"]`, with `console_scripts` entry points generating four commands
  on `PATH`: `sumtag`, `grouper`, `dedupe`, `dbmerge` (mapping to `sumtag.cli:main`,
  `sumtag.grouper:main`, `sumtag.dedupe:main`, `sumtag.dbmerge:main`).
- **REQ-PKG-3** — Both `sumtag …` (installed) and `python3 -m sumtag …` (source checkout)
  funnel through the same `main()`; no behavioral drift. `__main__.py` does
  `sys.exit(main())`.
- **REQ-PKG-4** (`main()` contract) — Each program's `main()` reads `sys.argv` (no required
  args), parses, and **returns an `int`** exit code (must `return`, not `print` and fall off
  the end), so exit codes propagate identically whether invoked as the installed command or
  via `-m`.
- **REQ-PKG-5** (man pages) — Each command has a man page under `man/` in three committed,
  never-drifting formats: `.1` (troff source of truth), `.1.txt` (plain text, `MANWIDTH=80
  man ./man/<p>.1 | col -b`), `.1.pdf` (`mandoc -T pdf`). This is a repo-maintenance
  invariant, not runtime behavior, but a reimplementation that ships man pages should keep
  the three in sync.

---

# Part IX — TDD build guide

This section is advisory: a suggested order and a test-harness shape that make the behavior
above testable. It mirrors the reference repo's own approach.

## Red-green build order

Build from the pure core outward, so each layer is testable before the next depends on it:

1. **Pure decision logic (Part II) first.** `should_rehash` and `classify_verify` are I/O-free
   functions over plain values — write their truth-table tests before anything else. Then the
   timestamp formatters (REQ-TIME) and `major_of`, which are similarly pure.
2. **Data-format helpers.** xattr document build/serialize/parse (REQ-SCHEMA-2/6), including
   the soft-parse failure cases (bad UTF-8, non-object, missing key, non-object `digests`).
3. **I/O edges** against real temp files: the xattr layer (`get`/`set`/`remove`, absent →
   `None`), the chunked hasher (compare against a known XXH3-64 vector), and mount detection
   (`mount + rel` recomposes via `samefile`). These need a filesystem but not the CLI.
4. **Traversal (Part III).** Build trees in a tempdir and assert yielded order, marker
   pruning (incl. root-marker warning), `--exclude` globbing, symlink skipping, and the
   directory-announcement callback.
5. **CLI parse + conflict matrix (Part IV).** Table-driven tests: each `parser.error` case
   from the matrix asserts a usage failure; each allowed combination parses. Pure argparse
   tests, no filesystem.
6. **End-to-end mode scenarios (Part V).** Subprocess (or in-process `main([...])`) runs over
   a built corpus, asserting the resulting xattrs, database rows, exit code, and — where it
   matters — stdout/stderr. This is where the scenario harness below pays off.
7. **Output formats (Part VI).** Summary block, status-line `-v` split, prescan prefix, and
   `--db-prescan` match-or-error. Progress output is stderr-tty-only, so test its *pure*
   pieces (`human_size`, bar rendering, field widths) directly rather than through a pty.
8. **Companion tools (Part VII).** Each is a separate program over a seeded database; build
   grouper's pure comparison functions (signatures + scorers, including the nomination
   invariant REQ-GROUPER-11) as unit tests first, then the stage pipelines, then dedupe's
   walk/veto logic, then dbmerge's replace semantics.

## Test-harness design (the proven pattern)

- **Independent oracle.** Read observed state back through a **separate** copy of the schema
  (its own JSON parse and its own XXH3), never the implementation's code. That independence
  is what lets the oracle *catch* a bug rather than mirror it. The oracle exposes convenience
  predicates like `digest_matches_content` (stored digest == freshly computed) and
  `mtime_matches` (stored `file_mtime` == live).
- **Corpus builder with prestamp fixtures.** Declare a starting tree of files (with sizes)
  and, per file, a **prestamp** state: `valid` (correct digest + current mtime),
  `wrong-digest` (a deliberately incorrect digest with a matching mtime — its survival proves
  a skip/import happened), or `stale` (a recorded mtime older than the file — proves a
  re-hash happened). Also declare `@sumtag-ignore` directories.
- **Mutate step.** After stamping but before the run, optionally disturb the tree:
  `_corrupt_silently` (overwrite bytes, then restore the exact `st_mtime_ns` → the
  `CORRUPTION` alarm case) vs `_edit_legitimately` (overwrite bytes, let mtime advance → the
  `STALE` case).
- **Scenario catalog.** Each scenario = starting corpus + optional mutate + `argv` + a
  `check(root, result, checker)` that inspects the real on-disk result and accumulates every
  failure (so one run reports all problems). Substitute a `{db}` token for the scenario's
  database path. Allow `extra_runs` to seed prior state (e.g. a first `--sum --database` run
  before the checked run).

## Starter requirement → test traceability (representative, not exhaustive)

| Requirement | Scenario / test |
|---|---|
| REQ-REHASH-1 (rows 2,5,6) | fresh file stamped; stale file re-hashed; up-to-date file skipped |
| REQ-REHASH-1 (row 1) | `--force` corrects a `wrong-digest` file |
| REQ-VERIFY-1 (row 4) | silent-corruption mutate → `--verify` exit 1 |
| REQ-VERIFY-1 (row 5) | legitimate-edit mutate → `--verify` exit 0 |
| REQ-WALK-2 | `wrong-digest` under an `@sumtag-ignore` dir survives `--sum` |
| REQ-SUM-10 | `--database --import -n` creates no database file, exit 0 |
| REQ-DB-2 | re-scan UPSERTs one row, no duplicate |
| REQ-DB-5 / REQ-SUM-8 | `--sum` leaves stat NULL; later `--locate` backfills, digest intact |
| REQ-SUM-9 | `--import` mirrors a `wrong-digest` verbatim, xattr untouched |
| REQ-PATH-1 | `mount + rel_path` recomposes to the file (`samefile`) |
| REQ-CLI-2 | bare `sumtag /d` (no action) → usage error |
| REQ-GROUPER-18 | deterministic partition; `group_dirs.dir_id` PK prevents double placement |
| REQ-DEDUPE-2 | bare run previews (nothing deleted); `--delete` actually deletes |
| REQ-DBMERGE-2 | re-merge replaces a mountpoint's rows; unnamed target mountpoints untouched |

---

# Appendix A — Canonical names (authoritative)

This appendix is the single source of truth for **exact spelling**: program names, every
option string (and its short form), positional metavars, `choices`, and defaults. A
reimplementation must match these verbatim. (Descriptions are elsewhere; this is names
only.) `type`/`action` are given because they affect parsing (e.g. `count`, `append`,
`nargs`).

### Programs (four `console_scripts`)

| command | entry point |
|---|---|
| `sumtag` | `sumtag.cli:main` |
| `grouper` | `sumtag.grouper:main` |
| `dedupe` | `sumtag.dedupe:main` |
| `dbmerge` | `sumtag.dbmerge:main` |

### `sumtag` (`prog="sumtag"`)

| option / positional | short | action / type | metavar / choices / default |
|---|---|---|---|
| `directories` | — | positional, `nargs="+"` | metavar `DIRECTORY` |
| `--dry-run` | `-n` | `store_true` | |
| `--quiet` | `-q` | `count` | default `0` |
| `--verbose` | `-v` | `count` | default `0` |
| `--progress` | — | `store_true` | |
| `--si` | — | `store_true` | |
| `--force` | `-f` | `store_true` | |
| `--database` | — | str | metavar `VALUE` |
| `--sum` | — | `store_true` | |
| `--import` | — | `store_true` | (dest `do_import`) |
| `--verify` | — | `store_true` | |
| `--remove` | — | `store_true` | |
| `--prune-dirs` | — | `store_true` | (dest `prune_dirs`) |
| `--prune-all` | — | `store_true` | (dest `prune_all`) |
| `--prescan` | — | `store_true` | |
| `--db-prescan` | — | `store_true` | (dest `db_prescan`) |
| `--no-ignore` | — | `store_true` | |
| `--exclude` | — | `append` | metavar `PATTERN`, default `[]` |
| `--locate` | — | `store_true` | |
| `--version` | — | `action="version"` | prints `sumtag <version>` |

### `grouper` (`prog="grouper"`)

| option | action / type | metavar / choices / default |
|---|---|---|
| `--database` | str, **required** | metavar `DB` |
| `--prep` | `store_true` | |
| `--index` | `store_true` | |
| `--pairs` | `store_true` | |
| `--threshold` | `float` | metavar `X` (valid range `0.0`–`1.0`) |
| `--fn` | choice | choices `digest`, `name-digest`, `name-score`; default `name-score` |
| `--jobs` | `int` | metavar `N`; default `os.cpu_count() or 1` (all CPUs) |
| `--progress` | `store_true` | |
| `--max-df` | `int` | metavar `N`; default `None` (auto: exhaustive ≤ ~10k dirs, else cap `1000`); `0` forces exhaustive |
| `--min-sim` | `float` | metavar `X`; default `0.0` |
| `--no-junk-filter` | `store_true` | (dest `no_junk`) |
| `--sort` | choice | choices `bond`, `files`, `size`, `tree-size`; default `bond` |
| `--name` | `store_true` | |
| `--ls` | str | metavar `DIR` |
| `--compare` | `nargs=2` | metavar `DIR_A DIR_B` |
| `--dupes` | `store_true` | |
| `--top` | `int`, `nargs="?"` | `const=1`, metavar `N` |
| `--min` | `int` | metavar `N`; default `2` |
| `--clean-db` | `store_true` | (dest `clean_db`) |

### `dedupe` (`prog="dedupe"`)

| option / positional | short | action | metavar / default |
|---|---|---|---|
| `--database` | — | str, **required** | metavar `DB` |
| `actual` | — | positional | metavar `ACTUAL` |
| `cull` | — | positional | metavar `CULL` |
| `--delete` | — | `store_true` | |
| `--allow-mixed` | — | `store_true` | (dest `allow_mixed`) |
| `--offline` | `-n` | `store_true` | |

### `dbmerge` (`prog="dbmerge"`)

| option / positional | short | action | metavar |
|---|---|---|---|
| `--database` | — | str, **required** | metavar `TARGET` |
| `sources` | — | positional, `nargs="+"` | metavar `SOURCE` |
| `--dry-run` | `-n` | `store_true` | |
| `--progress` | — | `store_true` | |
| `--allow-mixed` | — | `store_true` | (dest `allow_mixed`) |

### Cross-program naming notes

- The three companion programs all name their database `--database` (as does `sumtag`), but
  its **role** differs by program: sumtag's is an optional sink; grouper/dedupe require an
  existing sumtag database to read; dbmerge's `--database` is the write **target** and its
  positional `SOURCE...` are the inputs.
- `-n` means **dry-run** in `sumtag` and `dbmerge`, but **offline prediction** in `dedupe`
  (a deliberate difference — dedupe's bare run is already the preview; see REQ-DEDUPE-15).
- `--allow-mixed` (dedupe, dbmerge) and `--progress` (all but the pure inspection paths)
  carry the same meaning across programs.

---

*End of specification.*
