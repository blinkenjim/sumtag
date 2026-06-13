"""Stream a file's contents through XXH3-64.

Reads in chunks rather than slurping the whole file, so arbitrarily large
archive members hash in bounded memory. The optional ``progress`` callback is
invoked with cumulative bytes read, to drive a within-file indicator (--progress).
"""

from __future__ import annotations

from typing import Callable

import xxhash

_CHUNK = 1 << 20  # 1 MiB


def hash_file(path, progress: Callable[[int], None] | None = None) -> str:
    """Return the lowercase-hex XXH3-64 digest of the file at ``path``."""
    h = xxhash.xxh3_64()
    read = 0
    with open(path, "rb") as f:
        while True:
            chunk = f.read(_CHUNK)
            if not chunk:
                break
            h.update(chunk)
            if progress is not None:
                read += len(chunk)
                progress(read)
    return h.hexdigest()
