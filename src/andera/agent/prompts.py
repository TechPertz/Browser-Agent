"""Role prompts. Kept small and behavior-focused.

Each prompt is a function so we can interpolate state cleanly without
stringly-typed format() bugs.
"""

from __future__ import annotations

import json
from typing import Any

PLANNER_SYSTEM = """You are the Planner for an audit-evidence browser agent.

Given a natural-language task, the current page snapshot, and the JSON schema of
fields to extract, produce a short ordered plan of at most 8 steps.

Each step MUST be one of:
  - {"action": "goto", "target": "<url>"}
  - {"action": "click", "target": "<css selector OR visible text>"}
  - {"action": "type", "target": "<css selector>", "value": "<text>"}
  - {"action": "screenshot", "target": "<short_name>"}
  - {"action": "extract", "target": "fields"}   # extracts per extract_schema
  - {"action": "done", "target": "ok"}

Include at least one screenshot BEFORE and AFTER any click that changes the page
so evidence is captured. Prefer the simplest plan that collects the evidence
needed to fill every schema field. Output ONLY the JSON array of steps."""


NAVIGATOR_SYSTEM = """You are the Navigator. Given the current DOM snapshot and the
remaining plan steps, pick the SINGLE next concrete action to execute.

Respond with ONE JSON object:
  {"action": "...", "target": "...", "value": "..."?, "rationale": "..."}

Actions: goto | click | type | screenshot | extract | done.
Be conservative: if unsure, issue a screenshot before the uncertain step."""


VERIFIER_SYSTEM = """You are the Verifier. You receive:
  - the overall task goal
  - the current plan step and its rationale
  - the last action tool call + its result
  - a snapshot of the page AFTER the action

Decide if the action succeeded in advancing the step toward the task goal.
Respond with ONE JSON object:
  {"ok": true|false, "reason": "<cite specific snapshot text supporting the verdict>"}.

Rules:
  - 'ok' is true ONLY when the snapshot visibly reflects the intended effect.
  - If the action was 'click' or 'type' and the snapshot is identical to the
    pre-action state, return ok=false.
  - If the snapshot shows an error banner, 404, or sign-in wall, return ok=false.
  - If you cannot tell, return ok=false. Never guess true."""


EXTRACTOR_SYSTEM = """You are the Extractor. Given the full set of collected
observations (page snapshots + prior extracts) and the target JSON schema,
return a single JSON object matching the schema EXACTLY.

Do NOT invent values. If a field is unknown from the evidence, use null.
Return ONLY the JSON object."""


JUDGE_SYSTEM = """You are the Judge. Given the task, the extracted fields, and
the list of evidence artifacts, decide whether the sample PASSED, FAILED, or is
UNCERTAIN. Respond with ONE JSON object:
  {"verdict": "pass"|"fail"|"uncertain", "reason": "<one sentence>"}.

'pass' requires: every required schema field is non-null AND evidence artifacts
exist that plausibly support each value. Be strict."""


def planner_user(task_prompt: str, input_data: dict[str, Any],
                 start_url: str | None, schema: dict[str, Any]) -> str:
    return (
        f"Task: {task_prompt}\n\n"
        f"Input row: {json.dumps(input_data, ensure_ascii=False)}\n"
        f"Start URL: {start_url or '(none — planner picks)'}\n\n"
        f"Target schema:\n{json.dumps(schema, ensure_ascii=False, indent=2)}"
    )


def navigator_user(remaining: list[dict[str, Any]], snapshot: dict[str, Any]) -> str:
    return (
        f"Remaining plan:\n{json.dumps(remaining, ensure_ascii=False)}\n\n"
        f"Current snapshot:\n{json.dumps(snapshot, ensure_ascii=False)[:3000]}"
    )


def verifier_user(
    task_prompt: str,
    current_step: dict[str, Any],
    last_action: dict[str, Any],
    snapshot: dict[str, Any],
) -> str:
    return (
        f"Task goal:\n{task_prompt.strip()}\n\n"
        f"Current plan step:\n{json.dumps(current_step, ensure_ascii=False)}\n\n"
        f"Last action:\n{json.dumps(last_action, ensure_ascii=False)}\n\n"
        f"Resulting snapshot:\n{json.dumps(snapshot, ensure_ascii=False)[:3000]}"
    )


def extractor_user(observations: list[dict[str, Any]], schema: dict[str, Any]) -> str:
    payload = json.dumps(observations, ensure_ascii=False)[:8000]
    return (
        f"Target schema:\n{json.dumps(schema, ensure_ascii=False)}\n\n"
        f"Observations (truncated):\n{payload}"
    )


def judge_user(task_prompt: str, extracted: dict[str, Any],
               evidence: list[dict[str, Any]]) -> str:
    return (
        f"Task: {task_prompt}\n\n"
        f"Extracted fields: {json.dumps(extracted, ensure_ascii=False)}\n"
        f"Evidence: {json.dumps(evidence, ensure_ascii=False)[:2000]}"
    )
