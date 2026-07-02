#!/usr/bin/env bash
# (Re)build the project virtualenv for THIS machine.
#
# A venv is machine-specific -- interpreter symlinks and compiled wheels
# built on one OS don't work on another -- so each machine (or checkout)
# keeps its own .venv, which .gitignore keeps out of version control. This
# script rebuilds it with the local python3 and does the editable install.
#
# Run it once per machine, from anywhere:
#     scripts/setup-venv.sh
set -euo pipefail

cd "$(dirname "$0")/.."

rm -rf .venv

# Debian/Ubuntu ship python3 without ensurepip unless python3-venv is
# installed; fall back to a pip-less venv driven by the system pip, which
# can target another interpreter via --python (pip 22.3+).
if python3 -m venv .venv >/dev/null 2>&1; then
    pip_cmd=(.venv/bin/pip)
    # ensurepip bundles whatever pip shipped with the interpreter, which can
    # predate PEP 660 (editable installs of pyproject.toml-only projects need
    # pip >= 21.3; macOS python3 3.9 bundles 21.2). Upgrade before installing.
    "${pip_cmd[@]}" install --quiet --upgrade pip
else
    echo "setup-venv: ensurepip unavailable; using system pip3 --python" >&2
    rm -rf .venv
    python3 -m venv --without-pip .venv
    pip_cmd=(pip3 --python .venv/bin/python)
fi

"${pip_cmd[@]}" install -e .

echo "setup-venv: OK -- $(.venv/bin/python --version)"
