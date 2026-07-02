# Bugs

Known defects in sumtag. See TODO.md for feature work and deferred design decisions.

## Open

None currently.

## Closed

- **`--dry-run --database X --import` (without `--sum`) previewed the wrong action** (found and fixed 2026-07-02, commit `83e6412`). A fresh file with no xattr was previewed as `would hash ... (no usable metadata)`, but a real run of the same command correctly left it alone (`skip (no metadata)`) — `--import`'s refusal to compute unless `--sum`/`--force` says otherwise. Root cause: `_stamp()`'s `use_standard_decision` was computed from whether the database connection happened to be open, which is always `False` under `--dry-run` regardless of which action flags were given. Fixed by basing it on `args.sum or not args.database` directly.

## Not a bug

- **Sumtag hashes/stamps its own `--database` sqlite file when it lives inside a scanned tree.** Initially flagged as a possible bug (2026-07-01): the db file gets treated like any other file, hashed and mirrored into itself, then re-hashed on every subsequent run since it keeps changing after being stamped. No crash or data loss. Confirmed 2026-07-02: this is correct, intended behavior — sumtag has no reason to special-case its own database file, and doing so would add complexity without fixing any actual instability. Do not re-flag this.
