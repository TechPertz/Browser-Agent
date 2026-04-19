"""AgentState — the payload flowing through the LangGraph per sample.

Pure TypedDict so LangGraph can introspect + checkpoint it cleanly.
Each node returns a dict with a subset of these keys; LangGraph merges.
"""

from __future__ import annotations

from typing import Annotated, Any, Literal, TypedDict

# LangGraph reducers: list channels that should append, not replace.
def _append(left: list, right: list) -> list:
    return (left or []) + (right or [])


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
    observations: Annotated[list[dict[str, Any]], _append]
    tool_calls: Annotated[list[dict[str, Any]], _append]
    evidence: Annotated[list[dict[str, Any]], _append]  # Artifact dumps
    extracted: dict[str, Any]         # final extracted data
    reflect_count: int                # bounded retries (cap = 3)

    # --- control + result ---
    status: Status
    verdict: Literal["pass", "fail", "uncertain"]
    verdict_reason: str
    error: str
