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
    """Keep the last OBSERVATION_WINDOW entries intact; summarize older ones.

    Older observations are replaced with a 1-line abstract so total
    context size stays bounded on long flows. Summarization is
    deliberately cheap (no LLM call) — we capture kind + url/title/
    action so the verifier has a breadcrumb trail.
    """
    if len(observations) <= OBSERVATION_WINDOW:
        return observations
    head = observations[: -OBSERVATION_WINDOW]
    tail = observations[-OBSERVATION_WINDOW:]
    abstracts: list[dict[str, Any]] = []
    for obs in head:
        kind = obs.get("kind", "obs")
        data = obs.get("data") or {}
        abstracts.append({
            "kind": f"{kind}.abstract",
            "summary": _one_line(kind, data),
        })
    return abstracts + tail


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
    plan_cache_hit: bool              # telemetry
    task_type: str                    # set by classifier in Phase 2.75

    # --- control + result ---
    status: Status
    verdict: Literal["pass", "fail", "uncertain"]
    verdict_reason: str
    error: str
