"""GET /api/evidence/{sha}. Serves raw artifact bytes by sha256."""

from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, HTTPException
from fastapi.responses import Response

router = APIRouter()


def _find_blob(sha: str) -> Path | None:
    """Search across all run roots under `runs/` for a blob with this sha.

    Content-addressing means the blob could live in any run's blob dir.
    For Phase 5a we scan; later phases can index in SQLite.
    """
    runs_root = Path("runs")
    if not runs_root.exists():
        return None
    for run_dir in runs_root.iterdir():
        shard = run_dir / "blobs" / sha[:2]
        if not shard.exists():
            continue
        for p in shard.iterdir():
            if p.name.startswith(sha):
                return p
    return None


def _mime_from_ext(ext: str) -> str:
    mapping = {
        ".png": "image/png",
        ".jpg": "image/jpeg",
        ".jpeg": "image/jpeg",
        ".json": "application/json",
        ".txt": "text/plain",
        ".html": "text/html",
        ".pdf": "application/pdf",
    }
    return mapping.get(ext.lower(), "application/octet-stream")


@router.get("/api/evidence/{sha}")
async def get_evidence(sha: str) -> Response:
    if len(sha) != 64 or not all(c in "0123456789abcdef" for c in sha.lower()):
        raise HTTPException(400, "invalid sha")
    p = _find_blob(sha)
    if p is None:
        raise HTTPException(404, f"evidence not found: {sha}")
    return Response(
        content=p.read_bytes(),
        media_type=_mime_from_ext(p.suffix),
        headers={"Cache-Control": "public, max-age=31536000, immutable"},
    )
