"""Execute scenarios against the real sumtag command and collect outcomes.

Each scenario gets a fresh temporary tree: build the corpus, optionally mutate
it, run sumtag as a **subprocess** (the honest black box — real argv, real exit
code), then hand the result to the scenario's check. sumtag is invoked through
the harness's own interpreter as ``python -m sumtag`` by default, so it always
runs in the same environment the harness runs in.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

from .scenarios import Checker, RunResult, Scenario

# Default: the interpreter running the harness, as `python -m sumtag`. Guarantees
# sumtag resolves to the same (editable) install as the harness's environment.
DEFAULT_SUMTAG_CMD = [sys.executable, "-m", "sumtag"]


@dataclass
class Outcome:
    name: str
    description: str
    passed: bool
    failures: list[str] = field(default_factory=list)
    result: RunResult | None = None
    error: str | None = None  # harness-level failure (build/run blew up)


def run_scenario(
    scenario: Scenario,
    *,
    sumtag_cmd: list[str],
    workdir: Path,
    keep: bool,
) -> Outcome:
    tree = Path(tempfile.mkdtemp(prefix=f"sumtag-{scenario.name}-", dir=str(workdir)))
    # A scenario can refer to a database with the {db} token; it lives beside the
    # tree (not inside it, so it is never itself scanned). The check derives the
    # same path as str(root) + ".sqlite".
    db_path = str(tree) + ".sqlite"

    def _sub(argv: list[str]) -> list[str]:
        return [a.replace("{db}", db_path) for a in argv]

    try:
        from .corpus import build

        build(scenario.corpus, tree)
        if scenario.mutate is not None:
            scenario.mutate(tree)

        # Optional pre-runs (e.g. to set up a prior database state) before the
        # single run whose result is checked.
        for pre in scenario.extra_runs:
            subprocess.run([*sumtag_cmd, *_sub(pre), str(tree)],
                           capture_output=True, text=True, cwd=str(tree))
        if scenario.late_mutate is not None:
            scenario.late_mutate(tree)

        proc = subprocess.run(
            [*sumtag_cmd, *_sub(scenario.argv), str(tree)],
            capture_output=True,
            text=True,
            cwd=str(tree),
        )
        result = RunResult(proc.returncode, proc.stdout, proc.stderr)

        checker = Checker()
        scenario.check(tree, result, checker)
        return Outcome(
            name=scenario.name,
            description=scenario.description,
            passed=not checker.failures,
            failures=checker.failures,
            result=result,
        )
    except Exception as e:  # noqa: BLE001 - report any harness-level blowup as a failure
        return Outcome(
            name=scenario.name,
            description=scenario.description,
            passed=False,
            error=f"{type(e).__name__}: {e}",
        )
    finally:
        if keep:
            print(f"  (kept tree: {tree})", file=sys.stderr)
        else:
            shutil.rmtree(tree, ignore_errors=True)
            try:
                os.remove(db_path)
            except OSError:
                pass


def run_all(
    scenarios: list[Scenario],
    *,
    sumtag_cmd: list[str] | None = None,
    workdir: Path | None = None,
    keep: bool = False,
) -> list[Outcome]:
    sumtag_cmd = sumtag_cmd or DEFAULT_SUMTAG_CMD
    workdir = Path(workdir) if workdir else Path(tempfile.gettempdir())
    return [
        run_scenario(s, sumtag_cmd=sumtag_cmd, workdir=workdir, keep=keep)
        for s in scenarios
    ]
