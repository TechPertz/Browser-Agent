"""POST /api/runs, GET /api/runs, GET /api/runs/{id}.

Kicks off a RunWorkflow as an asyncio background task; the caller gets
back a run_id immediately and polls / subscribes for progress.
"""

from __future__ import annotations

import asyncio
import uuid
from pathlib import Path
from typing import Any

import yaml
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from andera.config import load_profile
from andera.orchestrator import load_inputs
from andera.orchestrator.runner import RunWorkflow

from ..registry import RunRecord, get_registry
from ..ws import get_bus

router = APIRouter()


class CreateRunRequest(BaseModel):
    task_path: str
    input_path: str
    max_samples: int | None = None
    run_id: str | None = None


@router.post("/api/runs")
async def create_run(req: CreateRunRequest) -> dict[str, Any]:
    task_path = Path(req.task_path)
    input_path = Path(req.input_path)
    if not task_path.exists():
        raise HTTPException(400, f"task not found: {task_path}")
    if not input_path.exists():
        raise HTTPException(400, f"input not found: {input_path}")

    with task_path.open() as f:
        task_spec = yaml.safe_load(f)

    try:
        rows = load_inputs(input_path)
    except Exception as e:
        raise HTTPException(400, f"input load failed: {e}") from e

    profile = load_profile()
    run_id = req.run_id or f"run-{uuid.uuid4().hex[:8]}"
    wf = RunWorkflow(
        profile=profile,
        task=task_spec,
        input_rows=rows,
        run_id=run_id,
        max_samples=req.max_samples,
    )
    # Wire the audit log to the event bus so subscribers see progress.
    wf.audit._on_append = get_bus().publish  # type: ignore[attr-defined]

    rec = RunRecord(
        run_id=run_id,
        task_id=task_spec.get("task_id"),
        status="queued",
        total=len(rows[: req.max_samples] if req.max_samples else rows),
        task=task_spec,
    )
    get_registry().register(rec)

    async def _drive() -> None:
        rec.status = "running"
        try:
            result = await wf.execute()
            rec.status = "completed"
            rec.total = result.total
            rec.passed = result.passed
            rec.failed = result.failed
            rec.run_root = str(result.run_root)
        except Exception as e:
            rec.status = "failed"
            rec.error = f"{type(e).__name__}: {e}"

    rec.task_fut = asyncio.create_task(_drive())
    return {"run_id": run_id, "status": "queued"}


@router.get("/api/runs")
async def list_runs() -> dict[str, Any]:
    return {"runs": get_registry().list()}


@router.get("/api/runs/{run_id}")
async def get_run(run_id: str) -> dict[str, Any]:
    rec = get_registry().get(run_id)
    if rec is None:
        raise HTTPException(404, f"run not found: {run_id}")
    out = rec.public_dict()
    out["samples_count"] = len(rec.samples)
    return out
