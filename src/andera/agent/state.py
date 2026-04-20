"""AgentState — the payload flowing through the LangGraph per sample.

Pure TypedDict so LangGraph can introspect + checkpoint it cleanly.
Each node returns a dict with a subset of these keys; LangGraph merges.
"""

from __future__ import annotations

from typing import Annotated, Any, Literal, TypedDict

# Cap for full observations kept verbatim; older ones compacted to abstracts.
OBSERVATION_WINDOW = 5


# LangGraph reducers: list channels that should append, not replace.
def _append(left: list, right: list) -> list:
    return (left or []) + (right or [])


def compact_observations(observations: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Keep the last OBSERVATION_WINDOW snapshot entries intact; summarize
    older snapshots. Extract observations are ALWAYS preserved verbatim —
    the extractor node reads them directly, so compacting them silently
    would drop per-item data on long list_iter flows and produce nulls.
    """
    # Split extracts (keep all) from snapshots/other (window-bounded).
    extracts = [o for o in observations if o.get("kind") == "extract"]
    others = [o for o in observations if o.get("kind") != "extract"]
    if len(others) <= OBSERVATION_WINDOW:
        return extracts + others  # extracts first so they're easy to find
    head = others[: -OBSERVATION_WINDOW]
    tail = others[-OBSERVATION_WINDOW:]
    abstracts: list[dict[str, Any]] = []
    for obs in head:
        kind = obs.get("kind", "obs")
        data = obs.get("data") or {}
        abstracts.append({
            "kind": f"{kind}.abstract",
            "summary": _one_line(kind, data),
        })
    return extracts + abstracts + tail


def _one_line(kind: str, data: dict[str, Any]) -> str:
    if kind == "snapshot":
        return f"snapshot: {data.get('title') or '?'} @ {data.get('url') or '?'}"
    if kind == "extract":
        keys = list((data or {}).keys())[:5]
        return f"extract: fields={keys}"
    return f"{kind}: <{len(data)} keys>"


Status = Literal[
    "pending", "planning", "acting", "verifying", "extracting",
    "persisting", "judging", "done", "failed",
]


class PlanStep(TypedDict, total=False):
    """One step in the agent's plan."""
    action: str       # goto | click | type | screenshot | extract | done
    target: str       # URL | selector | text | schema key
    value: str        # for type actions
    rationale: str


class AgentState(TypedDict, total=False):
    # --- inputs ---
    run_id: str
    sample_id: str
    task_prompt: str                  # NL task from the RunSpec
    input_data: dict[str, Any]        # one row of the input dataset
    start_url: str                    # optional starting URL
    extract_schema: dict[str, Any]    # JSON schema of target fields

    # --- agent working memory ---
    plan: list[PlanStep]              # ordered steps
    step_index: int                   # which plan step we're executing
    # observations: replaced wholesale by observe node (compacted in place).
    # tool_calls + evidence append via reducers (monotonic audit trail).
    observations: list[dict[str, Any]]
    tool_calls: Annotated[list[dict[str, Any]], _append]
    evidence: Annotated[list[dict[str, Any]], _append]  # Artifact dumps
    extracted: dict[str, Any]         # final extracted data
    reflect_count: int                # bounded retries (cap = 3)
    consecutive_fails: int            # verify-failures on the same step
    plan_count: int                   # number of times plan node has run (cap = 3)
    plan_cache_hit: bool              # telemetry
    task_type: str                    # set by classifier in Phase 2.75
    last_tool_error: str              # set when act's tool call errored; verify reads it
    extract_errors: list[str]         # schema validation errors from the last extract attempt
    judge_feedback: str               # judge's reason fed back into extract on retry

    # --- Set-of-Mark visual grounding state ---
    # `last_marks` holds the current page's numbered elements (from the
    # most recent `annotate` action). The `visual_do` handler reads
    # these to match a cached descriptor or to feed the vision LMM.
    # `last_annotated_sha` points to the annotated screenshot blob; the
    # act node reads those bytes when calling vision. Storing sha (not
    # bytes) keeps LangGraph checkpoints small.
    # `resolved_plan` mirrors `plan` with descriptors filled in — after
    # a successful sample we write this back to the PlanCache so every
    # subsequent input row replays without vision.
    last_marks: list[dict[str, Any]]
    last_annotated_sha: str
    resolved_plan: list[dict[str, Any]]

    # --- control + result ---
    status: Status
    verdict: Literal["pass", "fail", "uncertain"]
    verdict_reason: str
    error: str
