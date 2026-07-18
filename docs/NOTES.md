# Notes

## How to run the grouper

It's a standalone script (not part of the installed package), driven entirely by a sumtag SQLite database — `--database` is required on every invocation. The intended pipeline is two steps: prep, then group-and-report:

```bash
# Stages 1+2: build the directory index, then compare every directory
# to every other and store all nonzero pairs
python3 grouper.py --database foo.sqlite --prep

# Stage 3: build + persist the grouping at a similarity threshold, then
# report it. If the stored grouping is already current for this threshold,
# the rebuild is skipped and it just reports.
python3 grouper.py --database foo.sqlite --threshold 0.7

# Report the stored grouping again without touching anything
python3 grouper.py --database foo.sqlite
```

`--index` and `--pairs` remain available to run stages 1 and 2 individually, and stages combine in one invocation: `python3 grouper.py --database foo.sqlite --prep --threshold 0.7`. Since all nonzero pairs are stored, re-running with a different `--threshold` is cheap — it regroups without redoing the N² comparison.

There are also inspection helpers:

```bash
python3 grouper.py --database foo.sqlite --ls DIR          # list a directory's indexed files
python3 grouper.py --database foo.sqlite --compare A B     # similarity (0.0–1.0) of two dirs
python3 grouper.py --database foo.sqlite --dupes [--min N] # duplicate-files report
python3 grouper.py --database foo.sqlite --top [N]         # N (default 1) most frequent
                                                           #   checksums, excluding empty files
```

`--fn` selects the comparison function for `--pairs`/`--compare` (there's a default; the choices come from its `COMPARISONS` table). The database needs to have been populated by sumtag first (e.g. `sumtag --database foo.sqlite --sum <dir>`).
