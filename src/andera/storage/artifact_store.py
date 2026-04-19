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


def _safe_path_component(s: str) -> str:
    """Sanitize a user/LLM-supplied name so it can't escape the run root.

    Strips path separators, replaces non-[alnum._-] with '_', caps length.
    Never empty — returns '_' if input sanitizes to nothing.
    """
    s = s.replace("/", "_").replace("\\", "_").replace("..", "_")
    out = "".join(c if (c.isalnum() or c in "._-") else "_" for c in s)
    out = out.strip("._") or "_"
    return out[:120]


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
        # Human-readable subfolder placement (hardlink to the blob).
        # Blob remains source of truth for sha tracking; subfolder copy
        # is for humans browsing per-sample evidence directories.
        subfolder = tags.get("subfolder")
        if subfolder:
            safe_sub = _safe_path_component(str(subfolder))
            safe_name = _safe_path_component(str(name))
            human_dir = self.root / safe_sub
            human_dir.mkdir(parents=True, exist_ok=True)
            human_path = human_dir / safe_name
            # Collision handling: if a file with this name already exists,
            # it may be the same content (hardlinks share an inode — skip)
            # OR different content (planner reused a target across scrolls
            # — disambiguate with a short sha so we don't silently lose the
            # second screenshot in the human-readable folder).
            if human_path.exists():
                try:
                    same = human_path.stat().st_ino == dest.stat().st_ino
                except OSError:
                    same = False
                if not same:
                    stem, dot, ext = safe_name.rpartition(".")
                    if dot:
                        safe_name = f"{stem}-{sha[:8]}.{ext}"
                    else:
                        safe_name = f"{safe_name}-{sha[:8]}"
                    human_path = human_dir / safe_name
            if not human_path.exists():
                try:
                    human_path.hardlink_to(dest)
                except (OSError, NotImplementedError):
                    human_path.write_bytes(content)
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
