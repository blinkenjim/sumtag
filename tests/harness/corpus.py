"""Corpus builder: materialize a precisely-known file tree from a declarative spec.

A :class:`Corpus` describes files (content, size, mtime, and an optional
*prestamp* — the xattr state the file should already be in *before* sumtag
runs) plus directories that should carry an ``@sumtag-ignore`` marker. Content
is seeded-deterministic, so the same spec produces byte-identical files — and
therefore identical digests — on every run.

Prestamps are how the harness sets up the starting states that drive sumtag's
re-hash and verify logic. The xattr is written through the harness's own
:mod:`xattrio`, with the JSON assembled here from :mod:`schema` — never through
sumtag.
"""

from __future__ import annotations

import json
import os
import random
import zlib
from dataclasses import dataclass, field
from pathlib import Path

from . import schema, xattrio

# Marker filename that prunes a directory subtree (CLAUDE.md "Ignore markers").
IGNORE_MARKER = "@sumtag-ignore"


@dataclass
class PreStamp:
    """The xattr state a file should be in before sumtag runs.

    ``kind`` selects a starting condition; the optional fields override the
    derived defaults for finer control.

    Kinds:

    * ``"valid"`` — a correct, up-to-date stamp (digest matches content,
      ``file_mtime`` equals the file's actual mtime). sumtag should **skip** it.
    * ``"stale"`` — a correct digest but a ``file_mtime`` older than the file's
      actual mtime. sumtag should **re-hash**.
    * ``"wrong-digest"`` — a deliberately wrong digest with an up-to-date
      ``file_mtime``. Lets a black-box test observe skip-vs-rehash: if the wrong
      digest survives, the file was skipped; if corrected, it was re-hashed.
    * ``"wrong-version"`` — a lower major version, which should force a re-hash.
    * ``"missing-algo"`` — an empty ``digests`` map (the requested algorithm is
      absent), which should force a re-hash.
    * ``"malformed"`` — non-JSON bytes; sumtag should treat the xattr as absent.
    """

    kind: str = "valid"
    digest: str | None = None          # explicit digest hex (overrides derivation)
    file_mtime_ns: int | None = None   # explicit file_mtime in ns (overrides derivation)
    version: str | None = None         # explicit version string
    raw: bytes | None = None           # malformed: write these raw bytes verbatim


@dataclass
class FileSpec:
    """One file to create, relative to the corpus root."""

    relpath: str
    size: int = 64
    seed: int | None = None        # deterministic content; default derived from relpath
    content: bytes | None = None   # explicit content overrides size/seed
    mtime: float | None = None     # epoch seconds; if None, leaves the write-time mtime
    prestamp: PreStamp | None = None


@dataclass
class Corpus:
    """A whole tree: a set of files plus directories that carry ignore markers."""

    files: list[FileSpec] = field(default_factory=list)
    ignore_dirs: list[str] = field(default_factory=list)


def _content(spec: FileSpec) -> bytes:
    if spec.content is not None:
        return spec.content
    # zlib.crc32 gives a stable seed across processes (unlike the salted hash()).
    seed = spec.seed if spec.seed is not None else zlib.crc32(spec.relpath.encode())
    return random.Random(seed).randbytes(spec.size)


def _stamp_payload(path: Path, data: bytes, ps: PreStamp) -> bytes:
    """Build the JSON xattr value for a prestamp (raw kinds bypass this)."""
    actual_ns = path.stat().st_mtime_ns

    file_mtime_ns = ps.file_mtime_ns
    if file_mtime_ns is None:
        file_mtime_ns = actual_ns
        if ps.kind == "stale":
            file_mtime_ns = actual_ns - 60_000_000_000  # 60s older than the file

    digest = ps.digest
    if digest is None:
        correct = schema.compute_digest(data)
        digest = schema.corrupt_digest(correct) if ps.kind == "wrong-digest" else correct

    digests = {} if ps.kind == "missing-algo" else {schema.ALGO: digest}

    version = ps.version
    if version is None:
        version = "0.0.0" if ps.kind == "wrong-version" else schema.CURRENT_VERSION

    payload = {
        "version": version,
        "digests": digests,
        "file_mtime": schema.iso_utc_ns(file_mtime_ns),
        "hashed_at": schema.iso_utc_ns(actual_ns),
        "run_started_at": schema.iso_utc_ns(actual_ns),
    }
    return json.dumps(payload).encode("utf-8")


def _apply_prestamp(path: Path, data: bytes, ps: PreStamp) -> None:
    if ps.kind == "malformed":
        value = ps.raw if ps.raw is not None else b"not json at all \x00\xff"
    elif ps.raw is not None:
        value = ps.raw
    else:
        value = _stamp_payload(path, data, ps)
    xattrio.set(path, schema.XATTR_NAME, value)


def build(corpus: Corpus, root) -> Path:
    """Materialize ``corpus`` under ``root`` and return ``root`` as a Path.

    Order matters: content is written, then the mtime is set, then the prestamp
    is applied — so a ``"valid"`` prestamp's ``file_mtime`` captures the final
    mtime. (Writing an xattr changes ctime, not mtime, so it does not disturb the
    value just recorded.)
    """
    root = Path(root)

    for d in corpus.ignore_dirs:
        marker_dir = root / d
        marker_dir.mkdir(parents=True, exist_ok=True)
        (marker_dir / IGNORE_MARKER).write_bytes(b"")

    for f in corpus.files:
        p = root / f.relpath
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(_content(f))
        if f.mtime is not None:
            os.utime(p, (f.mtime, f.mtime))
        if f.prestamp is not None:
            _apply_prestamp(p, _content(f), f.prestamp)

    return root
