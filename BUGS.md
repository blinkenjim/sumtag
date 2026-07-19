# Bugs

Known defects in sumtag. See TODO.md for feature work and deferred design decisions.

## Open

None currently.

## Closed

- **`--progress` bar fragments stranded onscreen amid the per-file path output** (reported and fixed 2026-07-17). The bar line is budgeted to exactly fill the terminal, but the fixed field widths can overflow: binary `human_size` renders 9 characters for values in the 1000–1023.9 band of a unit (`1010.0MiB`), overflowing the 8-wide size and 10-wide rate fields (`1010.0MiB/s` is 11), and Python's format specs pad but never truncate. The over-width line wrapped onto a second terminal row, after which `\r` + erase-line only ever reached the continuation row — the first row (most of the bar) was stranded and scrolled away among the announcements. Intermittent because it needed a file size or throughput rate in that ~2.3% band (e.g. hashing at ~1.05–1.07 GB/s reads as `1010.0MiB/s`). Fixed by hard-clamping every redraw to the terminal width in one shared `_write_line` (which also covers terminals narrower than the 52-char fixed budget). Two adjacent leaks fixed in the same change: a read failing mid-hash skipped `ind.finish()`, printing the error appended to the stranded bar (now cleared in a `finally`, both the stamp and verify passes); and a terminal narrowed mid-file could still strand a rewrapped row (the terminal rewraps already-drawn text — unfixable after the fact, but the clamp prevents all future redraws from wrapping). Addendum (2026-07-19, fixed independently after a fresh report — DVD VOBs sit at ~1023.99MiB, so a VOB-heavy corpus hits the band on every VOB, not ~2.3% of the time): `human_size` now drops the decimal on a four-digit mantissa (`1024MiB`, 7 chars), so every size and rate fits its fixed field and a normal-width redraw never reaches the clamp — which the clamp alone couldn't give: it truncated the over-width line's tail, chopping the ETA field. The clamp remains the guarantee for narrow terminals.

- **`--dry-run --database X --import` (without `--sum`) previewed the wrong action** (found and fixed 2026-07-02, commit `83e6412`). A fresh file with no xattr was previewed as `would hash ... (no usable metadata)`, but a real run of the same command correctly left it alone (`skip (no metadata)`) — `--import`'s refusal to compute unless `--sum`/`--force` says otherwise. Root cause: `_stamp()`'s `use_standard_decision` was computed from whether the database connection happened to be open, which is always `False` under `--dry-run` regardless of which action flags were given. Fixed by basing it on `args.sum or not args.database` directly.

## Not a bug

- **Sumtag hashes/stamps its own `--database` sqlite file when it lives inside a scanned tree.** Initially flagged as a possible bug (2026-07-01): the db file gets treated like any other file, hashed and mirrored into itself, then re-hashed on every subsequent run since it keeps changing after being stamped. No crash or data loss. Confirmed 2026-07-02: this is correct, intended behavior — sumtag has no reason to special-case its own database file, and doing so would add complexity without fixing any actual instability. Do not re-flag this.
