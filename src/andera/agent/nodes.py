"""Agent nodes — each a pure-ish function `(AgentState) -> partial dict`.

Side effects (LLM calls, browser tool calls) happen through injected
dependencies captured via a factory closure. This keeps every node a
single-arg callable LangGraph can wire up while still honoring the
hexagonal rule: nodes depend on Protocols, not concretions.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

from andera.contracts import ChatModel
from andera.tools.browser import (
    BrowserTools,
    ClickArgs,
    ExtractArgs,
    GotoArgs,
    ScreenshotArgs,
    TypeArgs,
)

from . import prompts
from .classify import classify_task
from .plan_cache import PlanCache, plan_key
from .specialists import system_prompt_for
from .state import AgentState, compact_observations

REFLECT_MAX = 3
# If the verifier disagrees this many times in a row, bail to re-plan
# rather than chewing up reflection attempts on a dead plan.
REPLAN_AFTER_CONSECUTIVE_FAILS = 2


@dataclass
class AgentDeps:
    """Everything a node needs that isn't in AgentState."""
    planner: ChatModel
    navigator: ChatModel
    extractor: ChatModel
    judge: ChatModel
    browser: BrowserTools
    plan_cache: PlanCache | None = None
    classifier: ChatModel | None = None  # Haiku; if None, classifier node is a noop


def _parse_json(text: str) -> Any:
    """Robust JSON parse that tolerates ```json fences some models emit."""
    s = text.strip()
    if s.startswith("```"):
        # strip ``` fences and optional language tag
        s = s.split("```", 2)[1]
        if s.lstrip().startswith("json"):
            s = s.split("\n", 1)[1] if "\n" in s else s
        s = s.strip()
        if s.endswith("```"):
            s = s[: -3].strip()
    return json.loads(s)


def make_classify_node(deps: AgentDeps):
    """Classify task type once; specialists dispatch on this."""

    async def classify(state: AgentState) -> dict:
        # Already classified (e.g., re-entered after replan)? Pass through.
        if state.get("task_type"):
            return {}
        if deps.classifier is None:
            return {"task_type": "unknown"}
        task_type = await classify_task(
            state.get("task_prompt", ""),
            state.get("extract_schema") or {},
            deps.classifier,
        )
        return {"task_type": task_type}

    return classify


def make_plan_node(deps: AgentDeps):
    async def plan(state: AgentState) -> dict:
        task_prompt = state.get("task_prompt", "")
        schema = state.get("extract_schema") or {}
        start_url = state.get("start_url")
        task_type = state.get("task_type") or "unknown"

        # Check cache — identical task+schema+url_pattern -> reuse plan.
        cache_key = plan_key(task_prompt, schema, start_url)
        if deps.plan_cache is not None:
            cached = deps.plan_cache.get(cache_key)
            if cached is not None:
                return {
                    "plan": cached,
                    "step_index": 0,
                    "status": "acting",
                    "reflect_count": 0,
                    "consecutive_fails": 0,
                    "plan_cache_hit": True,
                }

        # Specialist system prompt driven by classified task type. Falls
        # back to the generic planner prompt when classifier was absent.
        system_prompt = system_prompt_for(task_type)
        messages = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": prompts.planner_user(
                task_prompt=task_prompt,
                input_data=state.get("input_data", {}),
                start_url=start_url,
                schema=schema,
            )},
        ]
        out = await deps.planner.complete(messages=messages)
        try:
            plan = _parse_json(out["content"])
            if not isinstance(plan, list):
                raise ValueError("planner did not return a list")
        except Exception as e:
            return {"status": "failed", "error": f"plan parse: {e}"}

        # Warm the cache for future samples of this task.
        if deps.plan_cache is not None:
            try:
                deps.plan_cache.put(cache_key, plan)
            except Exception:
                pass

        return {
            "plan": plan,
            "step_index": 0,
            "status": "acting",
            "reflect_count": 0,
            "consecutive_fails": 0,
            "plan_cache_hit": False,
        }

    return plan


def make_act_node(deps: AgentDeps):
    """Execute the current plan step via browser tools."""

    async def act(state: AgentState) -> dict:
        plan = state.get("plan") or []
        idx = state.get("step_index", 0)
        if idx >= len(plan):
            return {"status": "extracting"}
        step = plan[idx]
        action = step.get("action")
        target = step.get("target", "")
        value = step.get("value", "")

        if action == "goto":
            r = await deps.browser.goto(GotoArgs(url=target))
        elif action == "click":
            r = await deps.browser.click(ClickArgs(selector_or_text=target))
        elif action == "type":
            r = await deps.browser.type(TypeArgs(selector=target, value=value))
        elif action == "screenshot":
            r = await deps.browser.screenshot(ScreenshotArgs(name=target or f"step_{idx:02d}"))
        elif action == "extract":
            r = await deps.browser.extract(ExtractArgs(json_schema=state.get("extract_schema") or {}))
        elif action == "done":
            return {"status": "extracting"}
        else:
            return {"status": "failed", "error": f"unknown action {action!r}"}

        update: dict[str, Any] = {
            "tool_calls": [r.model_dump(mode="json")],
        }
        if r.status == "error":
            update["status"] = "verifying"  # let verifier decide if we reflect or abort
        else:
            update["status"] = "verifying"
        if action == "screenshot" and r.status == "ok":
            art = r.data.get("artifact")
            if art:
                update["evidence"] = [art]
        if action == "extract" and r.status == "ok":
            # observations is non-reducer now; append + compact explicitly.
            current = state.get("observations") or []
            projected = current + [{"kind": "extract", "data": r.data}]
            update["observations"] = compact_observations(projected)
        return update

    return act


def make_observe_node(deps: AgentDeps):
    """Take a fresh DOM snapshot so the verifier can judge the last action.

    Also compacts the observation history when it exceeds the window,
    so long flows (20+ steps) don't overflow LLM context on subsequent
    verifier / extractor calls. `observations` is a plain (non-reducer)
    list so we can replace it with a compacted version here.
    """

    async def observe(state: AgentState) -> dict:
        snap = await deps.browser.snapshot()
        if snap.status == "error":
            return {"status": "failed", "error": snap.error}
        new_entry = {"kind": "snapshot", "data": snap.data}
        projected = (state.get("observations") or []) + [new_entry]
        return {"observations": compact_observations(projected)}

    return observe


def make_verify_node(deps: AgentDeps):
    async def verify(state: AgentState) -> dict:
        tool_calls = state.get("tool_calls") or []
        last = tool_calls[-1] if tool_calls else {"tool_name": "none"}
        obs = state.get("observations") or []
        last_snap = next(
            (o["data"] for o in reversed(obs) if o.get("kind") == "snapshot"),
            {},
        )
        messages = [
            {"role": "system", "content": prompts.VERIFIER_SYSTEM},
            {"role": "user", "content": prompts.verifier_user(last, last_snap)},
        ]
        out = await deps.navigator.complete(messages=messages)
        try:
            v = _parse_json(out["content"])
            ok = bool(v.get("ok"))
        except Exception:
            # be forgiving — if verifier output is garbled, assume ok and advance
            ok = True
        idx = state.get("step_index", 0)
        if ok:
            return {
                "step_index": idx + 1,
                "status": "acting",
                "consecutive_fails": 0,
            }
        # Failed step: decide among reflect, replan, or fail.
        rc = state.get("reflect_count", 0)
        cf = state.get("consecutive_fails", 0) + 1

        if rc + 1 >= REFLECT_MAX:
            return {"status": "failed", "error": "reflection cap reached"}

        # Plan-level failure: if the same step has failed repeatedly,
        # the plan itself is probably wrong. Escalate to re-planning.
        if cf >= REPLAN_AFTER_CONSECUTIVE_FAILS:
            return {
                "status": "replanning",
                "reflect_count": rc + 1,
                "consecutive_fails": 0,
            }

        return {
            "reflect_count": rc + 1,
            "consecutive_fails": cf,
            "status": "acting",
        }

    return verify


def make_extract_node(deps: AgentDeps):
    async def extract(state: AgentState) -> dict:
        schema = state.get("extract_schema") or {}
        if not schema:
            return {"extracted": {}, "status": "judging"}
        messages = [
            {"role": "system", "content": prompts.EXTRACTOR_SYSTEM},
            {"role": "user", "content": prompts.extractor_user(
                state.get("observations") or [], schema,
            )},
        ]
        out = await deps.extractor.complete(messages=messages, schema=schema)
        parsed = out.get("parsed")
        if parsed is None:
            try:
                parsed = _parse_json(out["content"])
            except Exception as e:
                return {"status": "failed", "error": f"extract parse: {e}"}
        return {"extracted": parsed, "status": "judging"}

    return extract


def make_judge_node(deps: AgentDeps):
    async def judge(state: AgentState) -> dict:
        messages = [
            {"role": "system", "content": prompts.JUDGE_SYSTEM},
            {"role": "user", "content": prompts.judge_user(
                state.get("task_prompt", ""),
                state.get("extracted") or {},
                state.get("evidence") or [],
            )},
        ]
        out = await deps.judge.complete(messages=messages)
        try:
            v = _parse_json(out["content"])
            verdict = v.get("verdict", "uncertain")
            reason = v.get("reason", "")
        except Exception:
            verdict, reason = "uncertain", "judge output unparsable"
        return {"verdict": verdict, "verdict_reason": reason, "status": "done"}

    return judge


# --- routers (conditional edge fns) ---

def route_after_plan(state: AgentState) -> str:
    return "failed" if state.get("status") == "failed" else "act"


def route_after_act(state: AgentState) -> str:
    """Short-circuit observe+verify when the plan is exhausted or errored."""
    s = state.get("status")
    if s == "failed":
        return "failed"
    if s == "extracting":
        return "extract"
    return "observe"


def route_after_verify(state: AgentState) -> str:
    s = state.get("status")
    if s == "failed":
        return "failed"
    if s == "replanning":
        return "plan"
    plan = state.get("plan") or []
    if state.get("step_index", 0) >= len(plan):
        return "extract"
    return "act"


def route_after_extract(state: AgentState) -> str:
    return "failed" if state.get("status") == "failed" else "judge"
