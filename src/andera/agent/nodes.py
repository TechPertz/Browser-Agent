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
from .state import AgentState

REFLECT_MAX = 3


@dataclass
class AgentDeps:
    """Everything a node needs that isn't in AgentState."""
    planner: ChatModel
    navigator: ChatModel
    extractor: ChatModel
    judge: ChatModel
    browser: BrowserTools


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


def make_plan_node(deps: AgentDeps):
    async def plan(state: AgentState) -> dict:
        messages = [
            {"role": "system", "content": prompts.PLANNER_SYSTEM},
            {"role": "user", "content": prompts.planner_user(
                task_prompt=state.get("task_prompt", ""),
                input_data=state.get("input_data", {}),
                start_url=state.get("start_url"),
                schema=state.get("extract_schema", {}),
            )},
        ]
        out = await deps.planner.complete(messages=messages)
        try:
            plan = _parse_json(out["content"])
            if not isinstance(plan, list):
                raise ValueError("planner did not return a list")
        except Exception as e:
            return {"status": "failed", "error": f"plan parse: {e}"}
        return {"plan": plan, "step_index": 0, "status": "acting", "reflect_count": 0}

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
            update["observations"] = [{"kind": "extract", "data": r.data}]
        return update

    return act


def make_observe_node(deps: AgentDeps):
    """Take a fresh DOM snapshot so the verifier can judge the last action."""

    async def observe(state: AgentState) -> dict:
        snap = await deps.browser.snapshot()
        if snap.status == "error":
            return {"status": "failed", "error": snap.error}
        return {"observations": [{"kind": "snapshot", "data": snap.data}]}

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
            return {"step_index": idx + 1, "status": "acting"}
        # failed step: either reflect or give up
        rc = state.get("reflect_count", 0)
        if rc + 1 >= REFLECT_MAX:
            return {"status": "failed", "error": "reflection cap reached"}
        return {"reflect_count": rc + 1, "status": "acting"}

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
    plan = state.get("plan") or []
    if state.get("step_index", 0) >= len(plan):
        return "extract"
    return "act"


def route_after_extract(state: AgentState) -> str:
    return "failed" if state.get("status") == "failed" else "judge"
