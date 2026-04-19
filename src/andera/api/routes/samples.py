"""GET /api/runs/{id}/samples, GET /api/samples/{id}."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from fastapi import APIRouter, HTTPException

from ..registry import get_registry

router = APIRouter()


@router.get("/api/runs/{run_id}/samples")
async def list_samples(run_id: str) -> dict[str, Any]:
    rec = get_registry().get(run_id)
    if rec is None:
        raise HTTPException(404, f"run not found: {run_id}")
    return {"run_id": run_id, "samples": rec.samples}


@router.get("/api/runs/{run_id}/samples/{sample_id}")
async def get_sample(run_id: str, sample_id: str) -> dict[str, Any]:
    rec = get_registry().get(run_id)
    if rec is None:
        raise HTTPException(404, f"run not found: {run_id}")
    match = next((s for s in rec.samples if s.get("sample_id") == sample_id), None)
    if match is None:
        raise HTTPException(404, f"sample not found: {sample_id}")
    # Try to enrich from disk manifest if available
    mpath = Path(rec.run_root or ("runs/" + run_id)) / "RUN_MANIFEST.json"
    if mpath.exists():
        try:
            m = json.loads(mpath.read_text())
            for s in m.get("samples", []):
                if s.get("sample_id") == sample_id:
                    return {**match, **s}
        except Exception:
            pass
    return match
