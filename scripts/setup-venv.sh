#!/usr/bin/env bash
# (Re)build the project virtualenv for THIS machine and keep it out of Dropbox.
#
# The repo lives in a Dropbox-synced tree, and a venv is machine-specific:
# interpreter symlinks and compiled wheels built on one OS are garbage on
# another, so a synced .venv is broken everywhere except the machine that
# made it. This script rebuilds .venv with the local python3 and marks it
# with Dropbox's `com.dropbox.ignored` xattr so it never syncs.
#
# Run it once per machine, from anywhere:
#     scripts/setup-venv.sh
# and again any time .venv shows up broken (which means the ignore xattr
# was lost -- this script reapplies it).
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

# Mark the venv Dropbox-ignored *before* installing anything, so the sync
# client never starts uploading site-packages. On macOS the attribute name
# is used as-is; on Linux it must live in the user. namespace.
case "$(uname)" in
    Darwin) xattr -w com.dropbox.ignored 1 .venv ;;
    *)      .venv/bin/python -c \
              'import os; os.setxattr(".venv", "user.com.dropbox.ignored", b"1")' ;;
esac

"${pip_cmd[@]}" install -e .

echo "setup-venv: OK -- $(.venv/bin/python --version), .venv marked Dropbox-ignored"
