"""Agent nodes — each a pure-ish function `(AgentState) -> partial dict`.

Side effects (LLM calls, browser tool calls) happen through injected
dependencies captured via a factory closure. This keeps every node a
single-arg callable LangGraph can wire up while still honoring the
hexagonal rule: nodes depend on Protocols, not concretions.
"""

from __future__ import annotations

import base64
import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from andera.contracts import ChatModel
from andera.tools.browser import (
    AnnotateArgs,
    BrowserTools,
    ClickArgs,
    ClickMarkArgs,
    ExtractArgs,
    GotoArgs,
    ScreenshotArgs,
    ScrollArgs,
    ScrollToArgs,
    SearchArgs,
    TypeArgs,
    TypeMarkArgs,
    VisitEachLinkArgs,
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
# Hard cap on plan attempts per sample. Without this, verify→replan→plan
# resets reflect_count and the graph can loop until LangGraph's recursion
# cap fires. 3 = one initial plan + two replans.
PLAN_MAX = 3

# Actions that don't mutate the page. The verifier can't tell from a
# DOM snapshot whether these succeeded (nothing visible changed). If
# the tool returned status=ok, trust it and advance. Re-verifying via
# LLM just triggers "I can't tell if it changed → ok=false" loops that
# burn through the reflection budget without doing anything useful.
NON_DOM_CHANGING_ACTIONS = {
    "screenshot", "screenshot_all", "visit_each_link",
    "scroll", "scroll_to", "extract", "search",
    # `annotate` draws an overlay (pointerEvents: none) — the DOM that
    # matters hasn't changed. visual_do and click_mark / type_mark ARE
    # DOM-changing via the resulting click/type, so they go through
    # LLM verify like a normal click.
    "annotate",
    # goto_search_result DOES change the page — but the change is a
    # navigation, which is handled like `goto`. Exclude from fast-path.
}

# Allowed action names. Mirrored into PLAN_RESPONSE_SCHEMA so the
# planner LLM is physically forced (via response_format=json_schema)
# to pick from this enum — no more invented actions like
# "goto_first_linkedin_in_result" or plans with bare-string steps.
ALLOWED_ACTIONS = sorted({
    "goto", "click", "type",
    "screenshot", "screenshot_all",
    "scroll", "scroll_to",
    "visit_each_link",
    "search", "goto_search_result",
    # Set-of-Mark visual grounding. `visual_do` is the preferred primitive
    # for any click/type whose target depends on page content — the act
    # node annotates + resolves via vision (cache miss) or descriptor
    # match (cache hit). click_mark/type_mark exist for completeness if
    # the planner wants to pin a mark_id directly (rarely needed).
    "visual_do", "annotate", "click_mark", "type_mark",
    "extract", "done",
})

# Response schema the planner MUST satisfy. Wrapped in a top-level
# object because some providers (incl. Anthropic through LiteLLM)
# don't support top-level array outputs in strict JSON mode.
# Planner schema. Lists every field an action might emit so strict JSON
# mode actually surfaces them — Anthropic will ONLY generate keys that
# appear in `properties`. Without `url`/`selector`/`query` here, every
# step came out as `{"action": "goto"}` with no target and failed
# instantly. Do NOT add `additionalProperties: false` — that combo is
# what made Anthropic hang for 60s+ in earlier iterations.
PLAN_RESPONSE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "steps": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "action": {"type": "string", "enum": ALLOWED_ACTIONS},
                    "url": {"type": "string"},
                    "target": {"type": "string"},
                    "selector": {"type": "string"},
                    "text": {"type": "string"},
                    "value": {"type": "string"},
                    "name": {"type": "string"},
                    "path": {"type": "string"},
                    "mode": {"type": "string"},
                    "folder": {"type": "string"},
                    "query": {"type": "string"},
                    "url_pattern": {"type": "string"},
                    "url_filter": {"type": "string"},
                    "name_template": {"type": "string"},
                    "limit": {"type": "integer"},
                    "index": {"type": "integer"},
                    # Set-of-Mark fields.
                    "intent": {"type": "string"},   # visual_do: NL target
                    "mark_id": {"type": "integer"}, # click_mark / type_mark
                    "rationale": {"type": "string"},
                },
                "required": ["action"],
            },
        }
    },
    "required": ["steps"],
    "title": "AgentPlan",
}


# Vision resolver schema. Intentionally permissive on `descriptor` — vision
# picks WHICHEVER structural property identifies the element (href regex for
# links, name regex for branded CTAs, placeholder for search inputs,
# viewport_region as last resort). Only `role` is required because every
# mark has one.
VISION_RESPONSE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "mark_id": {"type": "integer"},
        "descriptor": {
            "type": "object",
            "properties": {
                "role": {"type": "string"},
                "href_pattern": {"type": "string"},
                "name_pattern": {"type": "string"},
                "placeholder_pattern": {"type": "string"},
                "viewport_region": {"type": "string"},
            },
            "required": ["role"],
        },
        "rationale": {"type": "string"},
    },
    "required": ["mark_id", "descriptor"],
    "title": "VisualResolve",
}


def _filter_by_descriptor(
    marks: list[dict[str, Any]], desc: dict[str, Any],
) -> list[dict[str, Any]]:
    """Return marks matching every non-empty descriptor field. Order
    preserved (DOM order), so `ordinal` indexing is deterministic."""
    out: list[dict[str, Any]] = []
    role = desc.get("role") or ""
    hrefp = desc.get("href_pattern") or ""
    namep = desc.get("name_pattern") or ""
    placep = desc.get("placeholder_pattern") or ""
    region = desc.get("viewport_region") or ""
    for m in marks:
        if role and m.get("role") != role:
            continue
        if hrefp:
            try:
                if not re.search(hrefp, m.get("href") or ""):
                    continue
            except re.error:
                # A bad regex from vision shouldn't poison replay — treat
                # as no-filter-on-this-field. The next-sample descriptor
                # rewrite will replace it with a valid pattern.
                pass
        if namep:
            try:
                if not re.search(namep, m.get("name") or ""):
                    continue
            except re.error:
                pass
        if placep:
            try:
                if not re.search(placep, m.get("placeholder") or ""):
                    continue
            except re.error:
                pass
        if region and (m.get("viewport_region") or "") != region:
            continue
        out.append(m)
    return out


def _match_descriptor(
    desc: dict[str, Any] | None, marks: list[dict[str, Any]],
) -> dict[str, Any] | None:
    """Resolve a cached descriptor to a concrete mark on the current page.

    Returns None when the descriptor has no candidates (page changed
    shape, element missing, A/B variant). Callers fall back to vision
    and rewrite the cache entry.
    """
    if not desc:
        return None
    candidates = _filter_by_descriptor(marks, desc)
    if not candidates:
        return None
    ordinal = int(desc.get("ordinal", 0))
    if 0 <= ordinal < len(candidates):
        return candidates[ordinal]
    return None


async def _vision_resolve(
    intent: str,
    image_path: str | Path,
    marks: list[dict[str, Any]],
    vision_model: ChatModel,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Ask the vision LMM to pick a mark + emit a structural descriptor.

    Returns (chosen_mark, descriptor_hint_from_vision).

    The image is sent as an Anthropic-style content block. LiteLLM
    translates this shape to whatever the underlying provider needs —
    no adapter changes required.
    """
    img_bytes = Path(image_path).read_bytes()
    img_b64 = base64.b64encode(img_bytes).decode("ascii")
    # Trim the marks list sent as text. The model also sees the image;
    # duplicating 80 marks as JSON doubles input tokens for diminishing
    # returns. 40 marks is plenty for a typical viewport.
    marks_for_prompt = marks[:40]
    user_blocks = [
        {
            "type": "text",
            "text": (
                f"Intent: {intent}\n\n"
                f"Marks visible on page (JSON):\n"
                f"{json.dumps(marks_for_prompt, ensure_ascii=False)}"
            ),
        },
        {
            "type": "image",
            "source": {
                "type": "base64",
                "media_type": "image/png",
                "data": img_b64,
            },
        },
    ]
    messages = [
        {"role": "system", "content": prompts.VISION_NAVIGATOR_SYSTEM},
        {"role": "user", "content": user_blocks},
    ]
    out = await vision_model.complete(
        messages=messages, schema=VISION_RESPONSE_SCHEMA,
    )
    parsed = out.get("parsed")
    if not isinstance(parsed, dict):
        parsed = _parse_json(out.get("content") or "{}")
    mark_id = int(parsed.get("mark_id", -1))
    by_id = {int(m["mark_id"]): m for m in marks}
    mark = by_id.get(mark_id)
    if mark is None:
        # Vision hallucinated an id. Best we can do is the first mark
        # matching the proposed descriptor's role, or the 0th overall.
        desc = parsed.get("descriptor") or {}
        candidates = _filter_by_descriptor(marks, desc) if desc else marks
        mark = candidates[0] if candidates else marks[0]
    descriptor = parsed.get("descriptor") or {"role": mark.get("role", "")}
    return mark, descriptor


def _descriptor_for(
    mark: dict[str, Any],
    marks: list[dict[str, Any]],
    vision_hint: dict[str, Any] | None,
) -> dict[str, Any]:
    """Merge vision's proposed descriptor with ordinal disambiguation.

    Vision supplies semantic intent (href_pattern / name_pattern /
    viewport_region). We compute `ordinal` among marks that match the
    non-ordinal fields so replay is deterministic when multiple
    candidates satisfy the pattern.
    """
    hint = dict(vision_hint or {})
    desc: dict[str, Any] = {"role": mark.get("role") or hint.get("role") or ""}
    for k in ("href_pattern", "name_pattern", "placeholder_pattern", "viewport_region"):
        v = hint.get(k)
        if v:
            desc[k] = v
    # Resolve ordinal AMONG marks that match the other fields. If vision
    # didn't give us any structural hint, fall back to ordinal-of-role
    # only (less precise, but still deterministic for homogeneous pages).
    candidates = _filter_by_descriptor(marks, desc)
    try:
        desc["ordinal"] = candidates.index(mark)
    except ValueError:
        # mark didn't match its own descriptor — happens when vision's
        # pattern is too strict. Fall back to ordinal by role only.
        desc["ordinal"] = 0
        same_role = [m for m in marks if m.get("role") == desc["role"]]
        if mark in same_role:
            desc["ordinal"] = same_role.index(mark)
    return desc


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
    # Multimodal model for Set-of-Mark vision resolution. None disables
    # visual_do — act node raises a tool error if the planner emits one
    # without vision wired. Keeps the legacy path alive for tasks that
    # don't need visual grounding.
    vision: ChatModel | None = None


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
    """Classify task type once; specialists dispatch on this.

    Classification is a pure function of (task_prompt, schema). Every
    sample in a run has the same task_prompt + schema, so we cache the
    result in a closure dict — N samples produce 1 Haiku call instead
    of N calls. Saves 20-50 s on a 1000-sample run.
    """
    from .classify import classify_cache_key

    _memo: dict[str, str] = {}

    async def classify(state: AgentState) -> dict:
        if state.get("task_type"):
            return {}
        if deps.classifier is None:
            return {"task_type": "unknown"}
        key = classify_cache_key(
            state.get("task_prompt", ""),
            state.get("extract_schema") or {},
        )
        if key in _memo:
            return {"task_type": _memo[key]}
        task_type = await classify_task(
            state.get("task_prompt", ""),
            state.get("extract_schema") or {},
            deps.classifier,
        )
        _memo[key] = task_type
        return {"task_type": task_type}

    return classify


def make_plan_node(deps: AgentDeps):
    async def plan(state: AgentState) -> dict:
        task_prompt = state.get("task_prompt", "")
        schema = state.get("extract_schema") or {}
        start_url = state.get("start_url")
        task_type = state.get("task_type") or "unknown"

        plan_count = state.get("plan_count", 0) + 1
        if plan_count > PLAN_MAX:
            return {
                "status": "failed",
                "error": f"plan cap reached ({PLAN_MAX} attempts)",
                "plan_count": plan_count,
            }

        # Check cache — identical task+schema+url_pattern -> reuse plan.
        # Only honored on the FIRST attempt; if the first plan failed, the
        # cached plan is presumed broken for this sample and we re-plan.
        cache_key = plan_key(task_prompt, schema, start_url)
        if plan_count == 1 and deps.plan_cache is not None:
            cached = deps.plan_cache.get(cache_key)
            if cached is not None:
                return {
                    "plan": cached,
                    "step_index": 0,
                    "status": "acting",
                    "reflect_count": 0,
                    "consecutive_fails": 0,
                    "plan_count": plan_count,
                    "plan_cache_hit": True,
                }

        # Specialist system prompt driven by classified task type. Falls
        # back to the generic planner prompt when classifier was absent.
        system_prompt = system_prompt_for(task_type)
        # Feed the planner the most recent snapshot (from preflight goto
        # OR from a previous act+observe on replan). It needs to see the
        # actual page to write concrete click targets.
        observations = state.get("observations") or []
        latest_snapshot = next(
            (o.get("data") for o in reversed(observations) if o.get("kind") == "snapshot"),
            None,
        )
        messages = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": prompts.planner_user(
                task_prompt=task_prompt,
                input_data=state.get("input_data", {}),
                start_url=start_url,
                schema=schema,
                current_snapshot=latest_snapshot,
            )},
        ]
        # Strict JSON schema so the planner can't emit bare-string steps,
        # invented action names, or placeholder URLs. We unwrap the
        # top-level {steps: [...]} envelope into a plain list for the
        # rest of the graph.
        # The wide try covers both (a) the adapter's auto-json-parse
        # raising on non-JSON content AND (b) our own shape-check fails.
        try:
            out = await deps.planner.complete(
                messages=messages, schema=PLAN_RESPONSE_SCHEMA,
            )
            parsed = out.get("parsed")
            if parsed is None:
                parsed = _parse_json(out["content"])
        except Exception as e:
            return {"status": "failed", "error": f"plan parse: {e}",
                    "plan_count": plan_count}
        # Support both shapes: {"steps": [...]} (strict mode) and a raw
        # list (legacy fallback if the provider ignored the schema).
        if isinstance(parsed, dict) and "steps" in parsed:
            plan = parsed["steps"]
        elif isinstance(parsed, list):
            plan = parsed
        else:
            return {"status": "failed", "plan_count": plan_count,
                    "error": f"plan parse: expected array or {{steps:[]}}, "
                             f"got {type(parsed).__name__}"}
        if not isinstance(plan, list) or not plan:
            return {"status": "failed", "plan_count": plan_count,
                    "error": "plan parse: empty or not a list"}

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
            "plan_count": plan_count,
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
        # Defensive: Opus sometimes drifts from the schema and emits plan
        # steps as bare strings or nested lists. Don't crash — surface a
        # tool error so verify → replan kicks in with a valid plan.
        if not isinstance(step, dict):
            return {
                "status": "verifying",
                "last_tool_error": (
                    f"plan step {idx} is not a JSON object (got "
                    f"{type(step).__name__}: {str(step)[:80]}). Replan with "
                    "each step as an object like "
                    '{"action": "...", ...}'
                ),
                "consecutive_fails": state.get("consecutive_fails", 0) + 1,
                "tool_calls": [{
                    "tool_name": "agent.bad_step",
                    "status": "error",
                    "error": f"non-dict step at idx {idx}",
                }],
            }
        action = step.get("action")
        # Planners (Opus) naturally emit action-specific fields: url for
        # goto, selector+text for click, name for screenshot. Accept those
        # as first-class AND fall back to the unified `target` key.
        target = (
            step.get("target")
            or step.get("url")
            or step.get("selector")
            or step.get("text")
            or step.get("name")
            or step.get("path")
            or ""
        )
        value = step.get("value", "")

        if action == "goto":
            r = await deps.browser.goto(GotoArgs(url=target))
        elif action == "click":
            r = await deps.browser.click(ClickArgs(selector_or_text=target))
        elif action == "type":
            r = await deps.browser.type(TypeArgs(selector=target, value=value))
        elif action == "screenshot":
            mode = step.get("mode") or "viewport"
            folder = step.get("folder") or step.get("directory") or step.get("subfolder")
            name = target or f"step_{idx:02d}"
            # Defensive parse: planner sometimes emits "folder/name.png"
            # as a single name field even though we asked for separate
            # `folder` + `name`. Promote the prefix to folder when
            # folder wasn't explicitly set. Sanitization happens in the
            # store; this just preserves intent.
            if folder is None and "/" in name:
                folder, name = name.split("/", 1)
            r = await deps.browser.screenshot(
                ScreenshotArgs(name=name, mode=mode, folder=folder),
            )
        elif action == "screenshot_all":
            folder = step.get("folder") or step.get("directory") or step.get("subfolder")
            name = target or f"step_{idx:02d}_all"
            if folder is None and "/" in name:
                folder, name = name.split("/", 1)
            r = await deps.browser.screenshot_all(
                ScreenshotArgs(name=name, folder=folder),
            )
        elif action == "search":
            query = step.get("query") or step.get("q") or target
            r = await deps.browser.search(SearchArgs(
                query=query,
                limit=int(step.get("limit", 5)),
            ))
        elif action == "goto_search_result":
            # Pull the most-recent search results from observations and
            # pick one matching the filter. Lets the planner compose
            # search -> goto without referencing future values statically.
            url_filter = (
                step.get("url_filter") or step.get("filter")
                or step.get("target") or ""
            )
            idx_in_results = int(step.get("index", 0))
            observations = state.get("observations") or []
            results: list[dict[str, Any]] = []
            for obs in reversed(observations):
                data = obs.get("data") or {}
                search = data.get("search")
                if isinstance(search, dict) and search.get("results"):
                    results = search["results"]
                    break
            candidates = (
                [x for x in results
                 if isinstance(x, dict) and url_filter in (x.get("url") or "")]
                if url_filter else
                [x for x in results if isinstance(x, dict)]
            )
            if idx_in_results < len(candidates):
                chosen = candidates[idx_in_results].get("url") or ""
                r = await deps.browser.goto(GotoArgs(url=chosen))
            else:
                # No matching search result — surface a synthetic tool
                # error so verify/replan can re-plan with a different
                # strategy (maybe broaden the filter or try next result).
                import uuid as _uuid

                from andera.contracts import ToolResult
                r = ToolResult(
                    call_id=_uuid.uuid4().hex[:12],
                    tool_name="agent.goto_search_result",
                    status="error",
                    data={"url_filter": url_filter, "index": idx_in_results,
                          "available_results": len(results)},
                    error=(
                        f"no search result matched url_filter={url_filter!r} "
                        f"at index={idx_in_results} "
                        f"(total results: {len(results)})"
                    ),
                )
        elif action == "scroll":
            r = await deps.browser.scroll(ScrollArgs(amount=(target or value or "down")))
        elif action == "scroll_to":
            r = await deps.browser.scroll_to(ScrollToArgs(target=target))
        elif action == "visit_each_link":
            folder = step.get("folder") or step.get("directory") or step.get("subfolder")
            name_tpl = (
                step.get("name_template") or step.get("name")
                or step.get("target") or "item_{i:02d}"
            )
            if folder is None and "/" in name_tpl:
                folder, name_tpl = name_tpl.split("/", 1)
            r = await deps.browser.visit_each_link(VisitEachLinkArgs(
                url_pattern=step.get("url_pattern") or step.get("pattern") or "/",
                limit=int(step.get("limit", 10)),
                name_template=name_tpl,
                folder=folder,
            ))
        elif action == "visual_do":
            # SET-OF-MARK primary primitive. Annotate → resolve → act.
            #
            # The resolver tries `step["resolved"]` (a cached structural
            # descriptor) first. On miss (cache-free first sample, or
            # descriptor no longer matches because the page shape
            # changed), it falls back to the vision LMM and rewrites
            # step["resolved"] so the plan cache persists a generic
            # descriptor that will work on other input rows.
            intent = step.get("intent") or step.get("target") or ""
            typed_value = step.get("value") or ""
            ann = await deps.browser.annotate(AnnotateArgs(name=f"step_{idx:02d}_annotated"))
            if ann.status != "ok":
                r = ann
            else:
                marks = ann.data.get("marks") or []
                art = ann.data.get("artifact") or {}
                resolved = step.get("resolved")
                chosen = _match_descriptor(resolved, marks)
                if chosen is None:
                    if deps.vision is None:
                        from andera.contracts import ToolResult
                        r = ToolResult(
                            call_id=ann.call_id,
                            tool_name="agent.visual_do",
                            status="error",
                            data={"intent": intent, "reason": "no_vision_model"},
                            error=(
                                "visual_do requires a vision model (profile "
                                "models.vision) but none is configured, and "
                                "no cached descriptor matched the current "
                                "page. Configure vision or rewrite the plan "
                                "to use text-grounded click/type."
                            ),
                        )
                    else:
                        try:
                            chosen, vhint = await _vision_resolve(
                                intent=intent,
                                image_path=art.get("path") or "",
                                marks=marks,
                                vision_model=deps.vision,
                            )
                            # Write the generalized descriptor INTO the plan
                            # step so that when the plan is cached at the end
                            # of the sample, subsequent rows replay deterministically.
                            step["resolved"] = _descriptor_for(chosen, marks, vhint)
                        except Exception as e:
                            from andera.contracts import ToolResult
                            r = ToolResult(
                                call_id=ann.call_id,
                                tool_name="agent.visual_do",
                                status="error",
                                data={"intent": intent, "marks_count": len(marks)},
                                error=f"vision_resolve failed: {e}",
                            )
                            chosen = None
                if chosen is not None:
                    mark_id = int(chosen.get("mark_id", -1))
                    if typed_value:
                        r = await deps.browser.type_mark(
                            TypeMarkArgs(mark_id=mark_id, value=typed_value),
                        )
                    else:
                        r = await deps.browser.click_mark(
                            ClickMarkArgs(mark_id=mark_id),
                        )
        elif action == "annotate":
            r = await deps.browser.annotate(AnnotateArgs(name=target or f"step_{idx:02d}_annotated"))
        elif action == "click_mark":
            mark_id = int(step.get("mark_id", step.get("index", -1)))
            r = await deps.browser.click_mark(ClickMarkArgs(mark_id=mark_id))
        elif action == "type_mark":
            mark_id = int(step.get("mark_id", step.get("index", -1)))
            r = await deps.browser.type_mark(
                TypeMarkArgs(mark_id=mark_id, value=value),
            )
        elif action == "extract":
            r = await deps.browser.extract(ExtractArgs(json_schema=state.get("extract_schema") or {}))
        elif action == "done":
            return {"status": "extracting"}
        else:
            return {"status": "failed", "error": f"unknown action {action!r}"}

        update: dict[str, Any] = {
            "tool_calls": [r.model_dump(mode="json")],
        }
        # Tool error is an unambiguous failure signal — do not leave it to
        # the LLM verifier to notice. Bump consecutive_fails directly and
        # still pass through observe/verify for evidence continuity.
        if r.status == "error":
            update["status"] = "verifying"
            update["consecutive_fails"] = state.get("consecutive_fails", 0) + 1
            update["last_tool_error"] = r.error
        else:
            update["status"] = "verifying"
        if action == "screenshot" and r.status == "ok":
            art = r.data.get("artifact")
            if art:
                update["evidence"] = [art]
        if action == "screenshot_all" and r.status == "ok":
            arts = r.data.get("artifacts") or []
            if arts:
                update["evidence"] = arts
        if action == "visit_each_link" and r.status == "ok":
            arts = r.data.get("artifacts") or []
            if arts:
                update["evidence"] = arts
            visited = r.data.get("visited") or []
            if visited:
                current = state.get("observations") or []
                projected = current + [{"kind": "extract", "data": {"visited": visited}}]
                update["observations"] = compact_observations(projected)
        if action == "search" and r.status == "ok":
            # Search results (title/url/snippet list) land as an extract
            # observation so subsequent plan steps + the final extractor
            # can see them. Especially important for "goto the top result"
            # style flows where the planner needs the URL from the results.
            current = state.get("observations") or []
            projected = current + [{"kind": "extract", "data": {"search": r.data}}]
            update["observations"] = compact_observations(projected)
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
        plan = state.get("plan") or []
        idx = state.get("step_index", 0)
        current_step = plan[idx] if idx < len(plan) else {}
        action = current_step.get("action")
        # Fast-path: non-DOM-changing actions that reported tool-OK.
        # No LLM verification — just advance. (Tool errors still fall
        # through to the `last_tool_error` branch below and fail cleanly.)
        if (
            action in NON_DOM_CHANGING_ACTIONS
            and not state.get("last_tool_error")
        ):
            return {
                "step_index": idx + 1,
                "status": "acting",
                "consecutive_fails": 0,
                "last_tool_error": None,
            }
        # Tool-layer errors are already definitive. Skip the LLM call.
        if state.get("last_tool_error"):
            ok = False
            _tool_error_reason = state.get("last_tool_error")
        else:
            _tool_error_reason = None
            tool_calls = state.get("tool_calls") or []
            last = tool_calls[-1] if tool_calls else {"tool_name": "none"}
            obs = state.get("observations") or []
            last_snap = next(
                (o["data"] for o in reversed(obs) if o.get("kind") == "snapshot"),
                {},
            )
            plan = state.get("plan") or []
            idx = state.get("step_index", 0)
            current_step = plan[idx] if idx < len(plan) else {}
            messages = [
                {"role": "system", "content": prompts.VERIFIER_SYSTEM},
                {"role": "user", "content": prompts.verifier_user(
                    task_prompt=state.get("task_prompt", ""),
                    current_step=current_step,
                    last_action=last,
                    snapshot=last_snap,
                )},
            ]
            out = await deps.navigator.complete(messages=messages)
            try:
                v = _parse_json(out["content"])
                ok = bool(v.get("ok"))
            except Exception:
                # SAFETY: garbled verifier output is NOT a pass signal.
                # Treat as not-ok and let the reflection budget handle it.
                ok = False
        idx = state.get("step_index", 0)
        if ok:
            return {
                "step_index": idx + 1,
                "status": "acting",
                "consecutive_fails": 0,
                "last_tool_error": None,
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


EXTRACT_RETRY_MAX = 2  # total attempts = 1 + EXTRACT_RETRY_MAX


def _is_array_schema(schema: dict[str, Any]) -> bool:
    """True when the caller asked for N-items-per-sample (fan-out)."""
    return schema.get("type") == "array" or "items" in schema


def _schema_errors(parsed: Any, schema: dict[str, Any]) -> list[str]:
    """Return human-readable schema validation errors, [] if valid.

    For array schemas, walks each element against `schema.items` and
    prefixes errors with `[i].` so the retry prompt can cite the bad
    item precisely.
    """
    if _is_array_schema(schema):
        item_schema = schema.get("items") or {}
        if not isinstance(parsed, list):
            return [f"expected array, got {type(parsed).__name__}"]
        errs: list[str] = []
        for i, item in enumerate(parsed):
            for msg in _schema_errors_obj(item, item_schema):
                errs.append(f"[{i}].{msg}")
        return errs
    return _schema_errors_obj(parsed, schema)


def _schema_errors_obj(parsed: Any, schema: dict[str, Any]) -> list[str]:
    """Single-object schema check (the original behavior)."""
    try:
        from jsonschema import Draft202012Validator
    except ImportError:
        if not isinstance(parsed, dict):
            return [f"expected object, got {type(parsed).__name__}"]
        missing = set(schema.get("required") or []) - set(parsed.keys())
        return [f"missing required field: {k}" for k in missing]
    if not isinstance(parsed, dict):
        return [f"expected object, got {type(parsed).__name__}"]
    errs = []
    for e in Draft202012Validator(schema).iter_errors(parsed):
        path = ".".join(str(p) for p in e.absolute_path) or "(root)"
        errs.append(f"{path}: {e.message}")
    return errs


def make_extract_node(deps: AgentDeps):
    async def extract(state: AgentState) -> dict:
        schema = state.get("extract_schema") or {}
        if not schema:
            # Action-oriented task (no structured extraction): nothing to
            # extract. Surface a small summary for the judge so it has
            # something to reason about instead of an empty dict.
            evidence = state.get("evidence") or []
            return {
                "extracted": {"evidence_count": len(evidence)},
                "status": "judging",
            }

        observations = state.get("observations") or []
        judge_feedback = state.get("judge_feedback")
        parsed: Any = None
        errors: list[str] = []
        is_array = _is_array_schema(schema)

        # Retry loop: initial attempt + EXTRACT_RETRY_MAX re-asks on
        # schema-invalid output, each time showing the prior attempt
        # and the specific validation errors.
        for attempt in range(EXTRACT_RETRY_MAX + 1):
            user_msg = prompts.extractor_user(
                observations,
                schema,
                judge_feedback=judge_feedback,
                prior_extraction=parsed if attempt > 0 else None,
                validation_errors=errors if attempt > 0 else None,
            )
            messages = [
                {"role": "system", "content": prompts.EXTRACTOR_SYSTEM},
                {"role": "user", "content": user_msg},
            ]
            # Array schemas skip strict JSON-mode — many providers only
            # allow top-level object for structured output. Prompt +
            # _parse_json round-trip is robust enough; the schema walk
            # below still enforces item structure on the retry loop.
            pass_schema = None if is_array else schema
            out = await deps.extractor.complete(messages=messages, schema=pass_schema)
            parsed = out.get("parsed")
            if parsed is None:
                try:
                    parsed = _parse_json(out["content"])
                except Exception as e:
                    errors = [f"JSON parse: {e}"]
                    continue
            # Unwrap `{items: [...]}` if the LLM produced a wrapped object
            # despite being asked for a bare array — common Haiku pattern.
            if is_array and isinstance(parsed, dict) and "items" in parsed and isinstance(parsed["items"], list):
                parsed = parsed["items"]
            errors = _schema_errors(parsed, schema)
            if not errors:
                break

        # Default-empty on total failure: [] for arrays, {} for objects.
        if parsed is None or (is_array and not isinstance(parsed, list)):
            parsed = [] if is_array else {}

        return {
            "extracted": parsed,
            "extract_errors": errors,
            "status": "judging",
            # Clear feedback so a re-entry from judge doesn't loop on stale text.
            "judge_feedback": None,
        }

    return extract


def make_judge_node(deps: AgentDeps):
    async def judge(state: AgentState) -> dict:
        schema = state.get("extract_schema") or {}
        evidence = state.get("evidence") or []
        # Action-oriented task: no schema to validate, so the verdict is
        # "did the plan complete + is there evidence?" Let the judge LLM
        # see the task + the evidence list and decide.
        messages = [
            {
                "role": "system",
                "content": prompts.JUDGE_SYSTEM if schema
                else prompts.JUDGE_SYSTEM_ACTION,
            },
            {"role": "user", "content": prompts.judge_user(
                state.get("task_prompt", ""),
                state.get("extracted") or {},
                evidence,
            )},
        ]
        out = await deps.judge.complete(messages=messages)
        try:
            v = _parse_json(out["content"])
            verdict = v.get("verdict", "uncertain")
            reason = v.get("reason", "")
        except Exception:
            verdict, reason = "uncertain", "judge output unparsable"

        # On fail/uncertain, route back to extract once with the judge's
        # reason so the extractor can correct itself from the already-
        # captured evidence. Bounded by reflect_count so we never loop.
        if verdict in ("fail", "uncertain"):
            rc = state.get("reflect_count", 0)
            if rc < REFLECT_MAX:
                return {
                    "verdict": verdict,
                    "verdict_reason": reason,
                    "judge_feedback": reason or "verdict was " + verdict,
                    "reflect_count": rc + 1,
                    "status": "extracting",  # route_after_judge sees this
                }

        # Sample passed — persist the resolved plan so subsequent input
        # rows of the same task replay deterministically. The `plan` in
        # state was mutated in-place by visual_do handlers to carry
        # `resolved` descriptors on each step.
        if verdict == "pass" and deps.plan_cache is not None:
            try:
                final_plan = state.get("plan") or []
                has_resolved = any(
                    isinstance(s, dict) and s.get("resolved") for s in final_plan
                )
                # Only overwrite the cache if we actually learned something
                # visual — skip for text-grounded tasks that never went
                # through visual_do.
                if has_resolved:
                    cache_key = plan_key(
                        state.get("task_prompt", ""),
                        state.get("extract_schema") or {},
                        state.get("start_url"),
                    )
                    deps.plan_cache.put(cache_key, final_plan)
            except Exception:
                # Never fail a sample because cache write hiccuped.
                pass

        return {"verdict": verdict, "verdict_reason": reason, "status": "done"}

    return judge


def route_after_judge(state: AgentState) -> str:
    """If judge requested a retry, go back to extract; else END."""
    if state.get("status") == "extracting":
        return "extract"
    return "end"


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
