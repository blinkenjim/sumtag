"""Independent extended-attribute access for the harness.

This is the harness's *own* xattr layer, deliberately separate from sumtag's:

* **Linux** uses :func:`os.getxattr` / :func:`os.setxattr` — thin stdlib wrappers
  straight onto the kernel syscalls. There is essentially no sumtag logic there
  to share a bug with, so reusing the stdlib calls is already independent.
* **macOS** has no stdlib xattr support, so *sumtag* hand-rolls ctypes bindings
  to libSystem — real code that could be buggy (buffer sizing, length handling).
  To stay independent of it, the harness shells out to the built-in ``xattr``
  command instead, exchanging values as hex (``-x``) to avoid any text-encoding
  pitfalls with arbitrary bytes.

The public surface is byte-oriented and uniform across platforms:
:func:`get`, :func:`set`, :func:`remove`, :func:`list_names`, :func:`supported`.
"""

from __future__ import annotations

import errno
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

_IS_MACOS = sys.platform == "darwin"

# errno values that mean "attribute not present". On Linux ENODATA is the one;
# ENOATTR is a BSD/macOS spelling that may not exist in the errno module.
_ENOATTR = {getattr(errno, "ENODATA", None), getattr(errno, "ENOATTR", None)} - {None}


class XattrUnavailable(RuntimeError):
    """Raised when the platform's xattr tooling is missing or unusable."""


# --- macOS: shell out to the built-in `xattr` CLI -------------------------


def _mac_xattr(*args: str) -> subprocess.CompletedProcess:
    if shutil.which("xattr") is None:
        raise XattrUnavailable("the macOS `xattr` command was not found on PATH")
    return subprocess.run(
        ["xattr", *args], capture_output=True, text=True
    )


def _mac_get(path: Path, name: str) -> bytes | None:
    r = _mac_xattr("-px", name, str(path))
    if r.returncode != 0:
        if "No such xattr" in r.stderr:
            return None
        raise XattrUnavailable(f"xattr -px failed: {r.stderr.strip()}")
    # `xattr -px` prints hex possibly grouped with spaces/newlines.
    return bytes.fromhex("".join(r.stdout.split()))


def _mac_set(path: Path, name: str, value: bytes) -> None:
    r = _mac_xattr("-wx", name, value.hex(), str(path))
    if r.returncode != 0:
        raise XattrUnavailable(f"xattr -wx failed: {r.stderr.strip()}")


def _mac_remove(path: Path, name: str) -> None:
    r = _mac_xattr("-d", name, str(path))
    if r.returncode != 0 and "No such xattr" not in r.stderr:
        raise XattrUnavailable(f"xattr -d failed: {r.stderr.strip()}")


def _mac_list(path: Path) -> list[str]:
    r = _mac_xattr(str(path))
    if r.returncode != 0:
        raise XattrUnavailable(f"xattr failed: {r.stderr.strip()}")
    return [line for line in r.stdout.splitlines() if line.strip()]


# --- Linux: stdlib os.*xattr ----------------------------------------------


def _linux_get(path: Path, name: str) -> bytes | None:
    try:
        return os.getxattr(path, name)
    except OSError as e:
        if e.errno in _ENOATTR:
            return None
        raise


def _linux_set(path: Path, name: str, value: bytes) -> None:
    os.setxattr(path, name, value)


def _linux_remove(path: Path, name: str) -> None:
    try:
        os.removexattr(path, name)
    except OSError as e:
        if e.errno not in _ENOATTR:
            raise


def _linux_list(path: Path) -> list[str]:
    return list(os.listxattr(path))


# --- public, platform-dispatched API --------------------------------------


def get(path, name: str) -> bytes | None:
    """Return the raw xattr value, or ``None`` if the attribute is absent."""
    path = Path(path)
    return _mac_get(path, name) if _IS_MACOS else _linux_get(path, name)


def set(path, name: str, value: bytes) -> None:  # noqa: A001 - mirrors os.setxattr
    """Write ``value`` (raw bytes) to the named xattr on ``path``."""
    path = Path(path)
    (_mac_set if _IS_MACOS else _linux_set)(path, name, value)


def remove(path, name: str) -> None:
    """Remove the named xattr; a no-op if it is already absent."""
    path = Path(path)
    (_mac_remove if _IS_MACOS else _linux_remove)(path, name)


def list_names(path) -> list[str]:
    """Return the names of all xattrs on ``path``."""
    path = Path(path)
    return _mac_list(path) if _IS_MACOS else _linux_list(path)


def supported(directory) -> bool:
    """Best-effort probe of whether ``directory``'s filesystem supports xattrs.

    Many temp filesystems (some CI tmpfs mounts) reject xattrs with ENOTSUP. A
    scenario runner can call this to skip cleanly rather than fail spuriously.
    """
    try:
        with tempfile.NamedTemporaryFile(dir=str(directory)) as tf:
            set(tf.name, "user.sumtag-probe", b"1")
            ok = get(tf.name, "user.sumtag-probe") == b"1"
            remove(tf.name, "user.sumtag-probe")
            return ok
    except (OSError, XattrUnavailable):
        return False
