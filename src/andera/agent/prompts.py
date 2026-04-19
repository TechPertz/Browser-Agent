"""Role prompts. Kept small and behavior-focused.

Each prompt is a function so we can interpolate state cleanly without
stringly-typed format() bugs.
"""

from __future__ import annotations

import json
from typing import Any

PLANNER_SYSTEM = """You are the Planner for an audit-evidence browser agent.

Given a natural-language task, the current page snapshot, and the JSON schema of
fields to extract, produce a short ordered plan of at most 10 steps.

Each step MUST be one of:
  - {"action": "goto", "target": "<url>"}
  - {"action": "click", "target": "<css selector OR visible text>"}
  - {"action": "type", "target": "<css selector>", "value": "<text>"}
  - {"action": "scroll", "target": "down"|"up"|"top"|"bottom"|"<px>"}
  - {"action": "scroll_to", "target": "<visible text OR css selector>"}
  - {"action": "screenshot", "target": "<short_name>", "mode": "viewport"}
  - {"action": "screenshot", "target": "<short_name>", "mode": "full"}
  - {"action": "screenshot_all", "target": "<short_name>"}
  - {"action": "extract", "target": "fields"}   # extracts per extract_schema
  - {"action": "done", "target": "ok"}

Screenshot guidance (important):
  - DEFAULT to mode="viewport" — it's smaller and faster. Use it for UI state
    checkpoints ("form before submit", "confirmation visible").
  - Use mode="full" ONLY when the task wording implies proof of an entire page
    (e.g. "full-page screenshot", "capture the whole report").
  - Use "screenshot_all" when the task requires walking a long page AND
    extracting content from multiple sections. Code deterministically scrolls
    top-to-bottom in viewport chunks; you do NOT need to emit scroll steps
    yourself. Do not combine screenshot_all with extra scroll steps.
  - For targeted reveal of a specific element, use scroll_to(text="…") THEN
    a viewport screenshot. This is cheaper than screenshot_all.

Include at least one screenshot BEFORE and AFTER any click that changes the
page so evidence is captured. Prefer the simplest plan that collects the
evidence needed to fill every schema field. Output ONLY the JSON array of steps."""


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


def _project_observation(obs: dict[str, Any]) -> dict[str, Any]:
    """Shrink an observation to just the fields an extractor benefits from.

    Keeps extract entries fully (that's per-item data). For snapshots,
    keep url+title+trimmed text + interactive element *names* only.
    """
    kind = obs.get("kind")
    data = obs.get("data") or {}
    if kind == "extract":
        return {"kind": "extract", "data": data}
    if kind and kind.endswith(".abstract"):
        return {"kind": kind, "summary": obs.get("summary", "")}
    if kind == "snapshot":
        return {
            "kind": "snapshot",
            "url": data.get("url"),
            "title": data.get("title"),
            "inner_text": (data.get("inner_text") or "")[:2000],
            "interactive_names": [
                i.get("name") for i in (data.get("interactive") or [])[:30]
            ],
        }
    return {"kind": kind, "data": data}


def extractor_user(
    observations: list[dict[str, Any]],
    schema: dict[str, Any],
    *,
    judge_feedback: str | None = None,
    prior_extraction: dict[str, Any] | None = None,
    validation_errors: list[str] | None = None,
) -> str:
    """Build the extractor user message.

    Projects observations to a bounded, structure-preserving form so
    we never slice a JSON blob mid-object. Appends judge feedback and
    validation errors for retry cycles.
    """
    projected = [_project_observation(o) for o in observations]
    # Send tail-first: freshest observations first so truncation drops
    # least-recent context instead of most-recent (which the extractor
    # usually needs).
    projected = list(reversed(projected))
    parts = [
        f"Target schema:\n{json.dumps(schema, ensure_ascii=False)}",
        f"Observations (most-recent first):\n{json.dumps(projected, ensure_ascii=False)[:12000]}",
    ]
    if prior_extraction is not None:
        parts.append(
            "Your previous extraction (refine, do NOT restart):\n"
            f"{json.dumps(prior_extraction, ensure_ascii=False)}"
        )
    if validation_errors:
        parts.append(
            "Schema validation errors to fix:\n- "
            + "\n- ".join(validation_errors)
        )
    if judge_feedback:
        parts.append(f"Judge feedback to address:\n{judge_feedback}")
    return "\n\n".join(parts)


def judge_user(task_prompt: str, extracted: dict[str, Any],
               evidence: list[dict[str, Any]]) -> str:
    return (
        f"Task: {task_prompt}\n\n"
        f"Extracted fields: {json.dumps(extracted, ensure_ascii=False)}\n"
        f"Evidence: {json.dumps(evidence, ensure_ascii=False)[:2000]}"
    )
