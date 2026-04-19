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


def _schema_from_fields(
    fields: str | None, multi_item: bool,
) -> dict[str, Any]:
    """Synthesize an extract schema from a comma-separated field list.

    - None / empty        -> {} (action-oriented, agent just captures evidence)
    - "a, b, c"           -> object schema with those three required strings
    - "a, b" + multi_item -> array schema whose items use the same object shape

    Field names are stripped + deduped while preserving order. Types are
    all string — richer typing would need a separate UI and is out of
    scope for this form. The judge + extractor handle null values for
    fields that aren't visible in evidence.
    """
    if not fields or not fields.strip():
        return {}
    seen: set[str] = set()
    names: list[str] = []
    for raw in fields.split(","):
        name = raw.strip()
        if name and name not in seen:
            seen.add(name)
            names.append(name)
    if not names:
        return {}
    item_schema = {
        "type": "object",
        "properties": {n: {"type": ["string", "null"]} for n in names},
        "required": names,
    }
    if multi_item:
        return {"type": "array", "items": item_schema}
    return item_schema


class CreateRunRequest(BaseModel):
    """Flexible create-run payload.

    Two ways to describe the task:
      - task_path: path to a task YAML on disk (legacy / CLI parity)
      - prompt:    NLP task string; synthesized into an inline task dict
                   with empty extract_schema (action-oriented flow)

    Two ways to feed rows:
      - input_path: load CSV / JSONL / JSON / XLSX
      - no input_path + repeat=False: single sample with empty row
    repeat=True without an input file is rejected (nothing to iterate).
    """

    task_path: str | None = None
    prompt: str | None = None
    input_path: str | None = None
    repeat: bool = False
    max_samples: int | None = None
    run_id: str | None = None
    # Optional structured-extraction hint for NLP tasks. Comma-separated
    # list of field names the agent should pull out (e.g. "author, date,
    # school"). If unset, the task is action-oriented.
    extract_fields: str | None = None
    # If True, extracted output becomes a list (one row per item) instead
    # of a single object. Enables fan-out tasks like "10 PRs per repo".
    multi_item: bool = False


@router.post("/api/runs")
async def create_run(req: CreateRunRequest) -> dict[str, Any]:
    if req.task_path:
        task_path = Path(req.task_path)
        if not task_path.exists():
            raise HTTPException(400, f"task not found: {task_path}")
        with task_path.open() as f:
            task_spec = yaml.safe_load(f) or {}
    elif req.prompt:
        task_spec = {
            "task_id": f"adhoc-{uuid.uuid4().hex[:6]}",
            "task_name": "Ad-hoc task",
            "prompt": req.prompt,
            # Schema depends on whether the caller listed fields to
            # extract. Empty -> action-oriented (screenshot flow).
            "extract_schema": _schema_from_fields(
                req.extract_fields, req.multi_item,
            ),
        }
    else:
        raise HTTPException(400, "either `task_path` or `prompt` is required")

    if req.input_path:
        input_path = Path(req.input_path)
        if not input_path.exists():
            raise HTTPException(400, f"input not found: {input_path}")
        try:
            rows = load_inputs(input_path)
        except Exception as e:
            raise HTTPException(400, f"input load failed: {e}") from e
    else:
        if req.repeat:
            raise HTTPException(400, "repeat=true requires an input file")
        rows = [{}]

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
        workflow=wf,
    )
    get_registry().register(rec)

    async def _drive() -> None:
        rec.status = "running"
        try:
            result = await wf.execute()
            # In distributed mode, execute() returns immediately after
            # enqueuing. Hand off to the API's finalizer loop, which
            # will call wf.finalize() once the queue drains.
            if profile.queue.distributed:
                rec.awaits_finalization = True
                rec.run_root = str(result.run_root)
                # rec.status stays "running" until finalizer flips it.
                return
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
