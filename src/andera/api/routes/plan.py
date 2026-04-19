"""POST /api/plan — NL task description -> task spec dict."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from andera.config import load_profile
from andera.models import Role, get_model
from andera.planner import plan_task_from_nl

router = APIRouter()


class PlanRequest(BaseModel):
    nl: str
    input_schema: dict[str, Any] | None = None


@router.post("/api/plan")
async def create_plan(req: PlanRequest) -> dict[str, Any]:
    if not req.nl.strip():
        raise HTTPException(400, "nl must be non-empty")
    profile = load_profile()
    model = get_model(Role.PLANNER, profile)
    try:
        spec = await plan_task_from_nl(
            nl=req.nl, planner_model=model, input_schema=req.input_schema,
        )
    except ValueError as e:
        raise HTTPException(422, str(e)) from e
    return {"task": spec}
