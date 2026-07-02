"""Format scenario outcomes and summarize pass/fail."""

from __future__ import annotations

import sys

from .runner import Outcome

_GREEN = "\033[32m"
_RED = "\033[31m"
_DIM = "\033[2m"
_RESET = "\033[0m"


def _color(text: str, code: str, use_color: bool) -> str:
    return f"{code}{text}{_RESET}" if use_color else text


def report(outcomes: list[Outcome], *, verbose: bool = False, use_color: bool | None = None) -> int:
    """Print per-scenario results and a summary. Return an exit code (0 = all pass)."""
    if use_color is None:
        use_color = sys.stdout.isatty()

    passed = sum(o.passed for o in outcomes)
    failed = len(outcomes) - passed

    for o in outcomes:
        tag = _color("PASS", _GREEN, use_color) if o.passed else _color("FAIL", _RED, use_color)
        print(f"[{tag}] {o.name}")
        if verbose and o.passed:
            print(_color(f"       {o.description}", _DIM, use_color))
        if not o.passed:
            print(_color(f"       {o.description}", _DIM, use_color))
            if o.error:
                print(f"       harness error: {o.error}")
            for f in o.failures:
                print(f"       - {f}")
            if verbose and o.result is not None:
                print(_color(f"       exit={o.result.exit_code}", _DIM, use_color))
                if o.result.stderr.strip():
                    print(_color(f"       stderr: {o.result.stderr.strip()}", _DIM, use_color))

    print()
    summary = f"{passed} passed, {failed} failed, {len(outcomes)} total"
    print(_color(summary, _GREEN if failed == 0 else _RED, use_color))
    return 0 if failed == 0 else 1
