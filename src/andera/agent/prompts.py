"""Role prompts. Kept small and behavior-focused.

Each prompt is a function so we can interpolate state cleanly without
stringly-typed format() bugs.
"""

from __future__ import annotations

import json
from typing import Any

PLANNER_SYSTEM = """You are the Planner for an audit-evidence browser agent.

Given a natural-language task, the current page snapshot, and the JSON schema of
fields to extract, produce an ordered plan of at most 40 steps. Multi-page
tasks (e.g. open a listing, click into each of 10 items to screenshot + extract
their detail page, then return) need most of that budget — don't under-plan.
Use nth-of-type / role-based selectors when you need to click "the Nth PR card"
without knowing its title in advance (e.g. `div[id^='issue_']:nth-of-type(1)`).

Each step MUST be one of:
  - {"action": "goto", "target": "<url>"}
  - {"action": "click", "target": "<css selector OR visible text>"}
  - {"action": "type", "target": "<css selector>", "value": "<text>"}
  - {"action": "scroll", "target": "down"|"up"|"top"|"bottom"|"<px>"}
  - {"action": "scroll_to", "target": "<visible text OR css selector>"}
  - {"action": "screenshot", "target": "<name>", "mode": "viewport"}
  - {"action": "screenshot", "target": "<name>", "mode": "full"}
  - {"action": "screenshot_all", "target": "<name>"}
      # `name` may contain a forward slash to place the shot in a subfolder:
      # "some-slug/some-file" → runs/<run_id>/some-slug/some-file.png
  - {"action": "visit_each_link", "url_pattern": "<href_substring>", "limit": N, "name": "<slug>/<prefix>_{i:02d}"}
      # Iterates up to N distinct same-origin links whose href contains the
      # substring, visits each, screenshots it, captures a snapshot, returns
      # to the original page. ONE plan step replaces N manual click+screenshot
      # cycles. `{i}` / `{i:02d}` in name get substituted with the index.
  - {"action": "search", "query": "<google query>", "limit": N}
      # Runs the query against Google via the Serper API (JSON results).
      # ALWAYS prefer this over goto-ing a search engine URL — search engines
      # (Google, DuckDuckGo, Bing) block Playwright with consent walls and
      # anti-bot pages. Results land as a search observation: a list of
      # {title, url, snippet}.
  - {"action": "goto_search_result", "url_filter": "<substring>", "index": 0}
      # PAIRS WITH `search`. Navigates to the Nth result from the most recent
      # search whose URL contains the substring. Use this because the plan is
      # static — you CANNOT reference future step outputs with placeholders
      # like "FIRST_RESULT_URL". The runtime looks up the URL at execution.
      # Typical pattern: `search` -> `goto_search_result` -> `screenshot` ->
      # repeat for the next item.
  - {"action": "extract", "target": "fields"}   # extracts per extract_schema
  - {"action": "done", "target": "ok"}

Iteration pattern (important):
  - `click` requires an EXACT selector or EXACT visible text. Descriptions
    like "first PR in list" will not resolve.
  - When a task says "visit / screenshot / extract for each of N items in a
    list", use `visit_each_link`. ONE plan step; code handles the loop. Do
    NOT manually unroll click → screenshot → back ×N — it wastes plan budget
    and the click step cannot identify "the Nth item" by description.

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

Organizing evidence into folders:
  - When a task asks for per-item folders, put the folder in the screenshot
    name using a slash ("<slug>/<file>"). The system creates the folder
    automatically and places the image there. No mkdir action needed.
  - Derive the slug from input_data (whatever field the task references).
    Never invent a slug.

Extraction guidance:
  - If the target schema is non-empty, plan your steps to collect the
    evidence you need to fill every schema field.
  - If the target schema is empty, the task is action-oriented (capture
    evidence, file things, navigate a flow) — no "extract" step is needed;
    end with "done" once the evidence is captured.

Include at least one screenshot BEFORE and AFTER any click that changes the
page so evidence is captured. Prefer the simplest plan. Output ONLY the JSON
array of steps."""


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
return JSON matching the schema EXACTLY.

Two modes, dispatched on the schema:
  - Object schema (type=object): return a single JSON object.
  - Array schema (type=array, has `items`): return a JSON ARRAY where each
    element matches the items subschema. If no items are visible in the
    evidence, return an empty array [].

Do NOT invent values. If a field is unknown from the evidence, use null.
Never wrap an array result in an object — return the bare array.
Return ONLY the JSON (no prose, no fences)."""


JUDGE_SYSTEM = """You are the Judge. Given the task, the extracted fields, and
the list of evidence artifacts, decide whether the sample PASSED, FAILED, or is
UNCERTAIN. Respond with ONE JSON object:
  {"verdict": "pass"|"fail"|"uncertain", "reason": "<one sentence>"}.

'pass' requires: every required schema field is non-null AND evidence artifacts
exist that plausibly support each value. Be strict."""


JUDGE_SYSTEM_ACTION = """You are the Judge for an action-oriented task (no
structured extraction schema). Given the task description and the list of
captured evidence artifacts, decide whether the agent completed the task.

Respond with ONE JSON object:
  {"verdict": "pass"|"fail"|"uncertain", "reason": "<one sentence>"}.

'pass' requires: the evidence list shows the agent visited the intended
pages / captured the screenshots the task asked for. Count and variety of
artifacts matter here — e.g. "screenshot each of top 10 PRs" needs ~10
artifacts. 'fail' if the agent produced zero evidence or clearly bailed
before completing the flow. 'uncertain' only if the evidence is suggestive
but ambiguous."""


def planner_user(
    task_prompt: str,
    input_data: dict[str, Any],
    start_url: str | None,
    schema: dict[str, Any],
    *,
    current_snapshot: dict[str, Any] | None = None,
) -> str:
    parts = [
        f"Task: {task_prompt}",
        f"Input row: {json.dumps(input_data, ensure_ascii=False)}",
        f"Start URL: {start_url or '(none — planner picks)'}",
        f"Target schema:\n{json.dumps(schema, ensure_ascii=False, indent=2)}",
    ]
    if current_snapshot:
        # The start_url has already been loaded by a preflight goto.
        # Show the planner what's actually on the page so it can write
        # concrete click targets (exact visible text / stable selectors)
        # instead of describing elements in natural language.
        trimmed = {
            "url": current_snapshot.get("url"),
            "title": current_snapshot.get("title"),
            "inner_text": (current_snapshot.get("inner_text") or "")[:2000],
            "interactive": [
                {
                    "name": it.get("name"),
                    "role": it.get("role"),
                    "selector": it.get("selector"),
                }
                for it in (current_snapshot.get("interactive") or [])[:40]
            ],
        }
        parts.append(
            "Current page (ALREADY LOADED by preflight — your plan should NOT "
            "re-goto it as the first step; start from whatever comes next):\n"
            + json.dumps(trimmed, ensure_ascii=False, indent=2)
        )
    return "\n\n".join(parts)


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
            # Detail pages have header metadata (author, date, status) before
            # the diff / comments. 4000 chars catches it on most sites; the
            # outer observation message is capped at 12000 so we stay within
            # LLM context for multi-page flows.
            "inner_text": (data.get("inner_text") or "")[:4000],
            "interactive_names": [
                i.get("name") for i in (data.get("interactive") or [])[:30]
            ],
            # Explicit date/time values lifted from <time>/<relative-time>
            # web components — inner_text usually misses these on sites
            # like GitHub/Stack Overflow where timestamps render via JS.
            "times": (data.get("times") or [])[:10],
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
    is_array = schema.get("type") == "array" or "items" in schema
    mode_hint = (
        "This task expects MULTIPLE items. Return a JSON ARRAY where each "
        "element matches the schema's `items` subschema. If no items are "
        "visible in the evidence, return []. Never wrap the array in an object."
        if is_array else
        "Return a single JSON OBJECT matching the schema."
    )
    parts = [
        f"Mode: {mode_hint}",
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
