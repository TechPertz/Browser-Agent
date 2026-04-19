import json

import pytest

from andera.planner import plan_task_from_nl


class Scripted:
    def __init__(self, content):
        self._c = content

    async def complete(self, messages, schema=None, **kwargs):
        return {"role": "assistant", "content": self._c}


GOOD_SPEC = {
    "task_id": "linear-tickets",
    "task_name": "Extract Linear ticket metadata",
    "task_type": "extract",
    "prompt": "For each Linear URL, screenshot and extract fields.",
    "extract_schema": {
        "type": "object",
        "required": ["ticket_no"],
        "properties": {
            "ticket_no": {"type": "string"},
            "assignee": {"type": ["string", "null"]},
        },
    },
}


@pytest.mark.asyncio
async def test_happy_path_returns_valid_spec():
    model = Scripted(json.dumps(GOOD_SPEC))
    out = await plan_task_from_nl(
        nl="Extract Linear ticket metadata",
        planner_model=model,
        input_schema={"columns": ["url"]},
    )
    assert out["task_id"] == "linear-tickets"
    assert out["task_type"] == "extract"


@pytest.mark.asyncio
async def test_fenced_json_tolerated():
    model = Scripted(f"```json\n{json.dumps(GOOD_SPEC)}\n```")
    out = await plan_task_from_nl(nl="x", planner_model=model)
    assert out["task_type"] == "extract"


@pytest.mark.asyncio
async def test_invalid_task_type_rejected():
    bad = {**GOOD_SPEC, "task_type": "banana"}
    model = Scripted(json.dumps(bad))
    with pytest.raises(ValueError):
        await plan_task_from_nl(nl="x", planner_model=model)


@pytest.mark.asyncio
async def test_missing_required_field_rejected():
    bad = {**GOOD_SPEC}
    bad.pop("task_type")
    model = Scripted(json.dumps(bad))
    with pytest.raises(ValueError):
        await plan_task_from_nl(nl="x", planner_model=model)


@pytest.mark.asyncio
async def test_schema_without_required_rejected():
    bad = dict(GOOD_SPEC)
    bad["extract_schema"] = {"type": "object", "properties": {"x": {"type": "string"}}}
    model = Scripted(json.dumps(bad))
    with pytest.raises(ValueError):
        await plan_task_from_nl(nl="x", planner_model=model)


@pytest.mark.asyncio
async def test_non_object_output_rejected():
    model = Scripted(json.dumps(["not", "an", "object"]))
    with pytest.raises(ValueError):
        await plan_task_from_nl(nl="x", planner_model=model)
