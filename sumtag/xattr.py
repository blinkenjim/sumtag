"""Platform-abstracted extended-attribute access.

The one part of sumtag that must straddle two OS APIs (CLAUDE.md "Platform
targets"):

* **Linux** uses the stdlib :func:`os.getxattr` / :func:`os.setxattr` (Python
  3.3+), which wrap the syscalls directly.
* **macOS** has no stdlib xattr support, so this module binds ``getxattr`` /
  ``setxattr`` from libSystem via :mod:`ctypes` — avoiding a second third-party
  dependency. macOS's calls carry the extra ``position`` and ``options``
  parameters (both zero here) that the Linux calls lack.

The public surface is byte-oriented and identical on both platforms: an absent
attribute reads back as ``None``.
"""

from __future__ import annotations

import os
import sys

_IS_MACOS = sys.platform == "darwin"

if _IS_MACOS:
    import ctypes
    import ctypes.util

    _libc = ctypes.CDLL(ctypes.util.find_library("c"), use_errno=True)

    # ssize_t getxattr(path, name, void *value, size_t size, u_int32_t pos, int opts)
    _libc.getxattr.restype = ctypes.c_ssize_t
    _libc.getxattr.argtypes = [
        ctypes.c_char_p, ctypes.c_char_p, ctypes.c_void_p,
        ctypes.c_size_t, ctypes.c_uint32, ctypes.c_int,
    ]
    # int setxattr(path, name, const void *value, size_t size, u_int32_t pos, int opts)
    _libc.setxattr.restype = ctypes.c_int
    _libc.setxattr.argtypes = [
        ctypes.c_char_p, ctypes.c_char_p, ctypes.c_char_p,
        ctypes.c_size_t, ctypes.c_uint32, ctypes.c_int,
    ]

    _ENOATTR = 93  # macOS errno for "attribute not found"

    def _raise_errno(path: str) -> None:
        err = ctypes.get_errno()
        raise OSError(err, os.strerror(err), path)

    def get(path, name: str) -> bytes | None:
        p, n = os.fsencode(path), name.encode()
        size = _libc.getxattr(p, n, None, 0, 0, 0)
        if size < 0:
            if ctypes.get_errno() == _ENOATTR:
                return None
            _raise_errno(str(path))
        buf = ctypes.create_string_buffer(size)
        got = _libc.getxattr(p, n, buf, size, 0, 0)
        if got < 0:
            _raise_errno(str(path))
        return buf.raw[:got]

    def set(path, name: str, value: bytes) -> None:  # noqa: A001 - mirrors os.setxattr
        p, n = os.fsencode(path), name.encode()
        if _libc.setxattr(p, n, value, len(value), 0, 0) < 0:
            _raise_errno(str(path))

else:  # Linux and other os.*xattr platforms

    import errno

    _ENOATTR = {getattr(errno, "ENODATA", None), getattr(errno, "ENOATTR", None)} - {None}

    def get(path, name: str) -> bytes | None:
        try:
            return os.getxattr(path, name)
        except OSError as e:
            if e.errno in _ENOATTR:
                return None
            raise

    def set(path, name: str, value: bytes) -> None:  # noqa: A001 - mirrors os.setxattr
        os.setxattr(path, name, value)
