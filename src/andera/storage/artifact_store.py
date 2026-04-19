"""Content-addressed artifact store.

Filenames on disk are the content sha256 — immutable, deduplicated,
tamper-evident. Callers pass a human-readable `name` that we persist in
the DB row but not on disk (so the same image stored twice occupies one
file). Evidence folders are built via symlinks or path aliases above
this layer.
"""

from __future__ import annotations

import hashlib
import mimetypes
from pathlib import Path
from typing import Any

from andera.contracts import Artifact


def sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def guess_mime(name: str, fallback: str = "application/octet-stream") -> str:
    mime, _ = mimetypes.guess_type(name)
    return mime or fallback


class FilesystemArtifactStore:
    """Implements the `ArtifactStore` Protocol against the local filesystem.

    Layout:
        {root}/blobs/{sha[0:2]}/{sha}{ext}

    The 2-char shard keeps per-directory file counts manageable at 100k+
    artifacts. Extension is inferred from `name` so downstream viewers
    open the file correctly.
    """

    def __init__(self, root: str | Path) -> None:
        self.root = Path(root)
        self.blobs = self.root / "blobs"
        self.blobs.mkdir(parents=True, exist_ok=True)

    def _shard_path(self, sha: str, ext: str) -> Path:
        return self.blobs / sha[:2] / f"{sha}{ext}"

    async def put(
        self, content: bytes, name: str, mime: str | None = None, **tags: Any
    ) -> Artifact:
        sha = sha256_hex(content)
        ext = Path(name).suffix
        dest = self._shard_path(sha, ext)
        if not dest.exists():
            dest.parent.mkdir(parents=True, exist_ok=True)
            tmp = dest.with_suffix(dest.suffix + ".tmp")
            tmp.write_bytes(content)
            tmp.rename(dest)  # atomic on POSIX
        return Artifact(
            sha256=sha,
            name=name,
            mime=mime or guess_mime(name),
            size=len(content),
            path=str(dest),
            sample_id=tags.get("sample_id"),
            run_id=tags.get("run_id"),
        )

    async def get(self, sha: str) -> bytes:
        # scan shard directory (we don't know the ext from sha alone)
        shard = self.blobs / sha[:2]
        if not shard.exists():
            raise FileNotFoundError(f"artifact not found: {sha}")
        for f in shard.iterdir():
            if f.name.startswith(sha):
                return f.read_bytes()
        raise FileNotFoundError(f"artifact not found: {sha}")

    def local_path(self, artifact: Artifact) -> Path:
        return Path(artifact.path)
