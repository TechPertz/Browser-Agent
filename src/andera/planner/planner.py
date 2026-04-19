"""Natural-language -> task spec.

Turns "For each Linear URL, capture a screenshot and extract ticket
number + assignee" into a dict matching the task YAML schema. The
result is validated by the same Pydantic we use for loaded YAMLs so
the output is runnable without a human touching it.
"""

from __future__ import annotations

import json
from typing import Any

from andera.agent.classify import _VALID as VALID_TASK_TYPES
from andera.contracts import ChatModel

PLANNER_SYSTEM = """You convert a natural-language task description into a
structured Andera task spec. Respond with a SINGLE JSON object with:

  task_id:     short kebab-case slug, derived from the task description
  task_name:   one-line human title
  task_type:   one of [extract, form_fill, list_iter, navigate]
  prompt:      the full task description (preserve user's intent verbatim
               where helpful; add clarifying detail about screenshotting
               before/after each click that changes the page)
  extract_schema:
    type: "object"
    required: [list of required keys]
    properties:
      <key>: { type: "string" | "integer" | [type, "null"] }

Rules:
- task_type MUST be one of the four allowed values.
- extract_schema MUST have at least one required field.
- If the input schema mentions columns, mirror them as properties.
- Respond ONLY with the JSON object. No prose.
"""


def _user_prompt(nl: str, input_schema: dict[str, Any] | None) -> str:
    cols = (input_schema or {}).get("columns") or []
    return (
        f"Natural-language task:\n{nl.strip()}\n\n"
        f"Input columns: {json.dumps(cols)}"
    )


def _parse_json(text: str) -> Any:
    s = text.strip()
    if s.startswith("```"):
        s = s.split("```", 2)[1]
        if s.lstrip().startswith("json"):
            s = s.split("\n", 1)[1] if "\n" in s else s
        s = s.strip()
        if s.endswith("```"):
            s = s[:-3].strip()
    return json.loads(s)


def _validate(spec: dict[str, Any]) -> None:
    required = {"task_id", "task_name", "task_type", "prompt", "extract_schema"}
    missing = required - set(spec.keys())
    if missing:
        raise ValueError(f"planner output missing fields: {sorted(missing)}")
    if spec["task_type"] not in VALID_TASK_TYPES:
        raise ValueError(
            f"task_type must be one of {sorted(VALID_TASK_TYPES)}, got {spec['task_type']!r}"
        )
    schema = spec.get("extract_schema") or {}
    if schema.get("type") != "object":
        raise ValueError("extract_schema.type must be 'object'")
    if not schema.get("required"):
        raise ValueError("extract_schema must declare at least one required field")


async def plan_task_from_nl(
    *,
    nl: str,
    planner_model: ChatModel,
    input_schema: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Call the planner model, parse, validate, return the task dict."""
    out = await planner_model.complete(
        messages=[
            {"role": "system", "content": PLANNER_SYSTEM},
            {"role": "user", "content": _user_prompt(nl, input_schema)},
        ],
        temperature=0.1,
    )
    spec = _parse_json(out.get("content") or "")
    if not isinstance(spec, dict):
        raise ValueError("planner did not return a JSON object")
    _validate(spec)
    return spec
