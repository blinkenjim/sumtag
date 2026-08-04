"""Stream a file's contents through a digest algorithm.

Reads in chunks rather than slurping the whole file, so arbitrarily large
archive members hash in bounded memory. The optional ``progress`` callback is
invoked with cumulative bytes read, to drive a within-file indicator (--progress).

Which algorithm runs is a single switch: :data:`sumtag.schema.ALGO`. Everything
downstream (the xattr's ``digests`` key, the re-hash decision, the DB ``algo``
column) already reads that same constant, so flipping it here is enough --
no other module needs to change. A real ``--digest`` selector is future work;
today this is a code-level switch, not a CLI flag.
"""

from __future__ import annotations

import hashlib
from typing import Callable

import xxhash

from . import schema

_CHUNK = 1 << 20  # 1 MiB

#: Digest algorithm name -> zero-arg constructor returning a hashlib-shaped
#: object (``.update(bytes)`` / ``.hexdigest()``). Add new algorithms here.
_HASHERS: dict[str, Callable[[], object]] = {
    "xxh3": xxhash.xxh3_64,
    "md5": hashlib.md5,
}


def hash_file(path, algo: str | None = None,
              progress: Callable[[int], None] | None = None) -> str:
    """Return the lowercase-hex digest of the file at ``path``.

    ``algo`` defaults to :data:`sumtag.schema.ALGO`, looked up at call time
    (not as a default-argument value) so changing that constant takes effect
    without needing to re-import this module.
    """
    # Resolve the algorithm at CALL time (never as a default-argument value)
    # so flipping schema.ALGO redirects everything downstream immediately;
    # an unknown name is a ValueError, not a KeyError leak.
    algo = algo if algo is not None else schema.ALGO
    try:
        factory = _HASHERS[algo]
    except KeyError:
        raise ValueError(f"unsupported digest algorithm: {algo!r}") from None
    # Fixed-size chunked reads: bounded memory whatever the file size. The
    # progress callback sees the cumulative byte count after each chunk --
    # exactly what a within-file indicator needs.
    h = factory()
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
