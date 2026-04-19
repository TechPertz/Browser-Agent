"""LangGraph assembly.

Wires the nodes into the state machine:

    START -> plan -> act -> observe -> verify -> (act | extract | failed)
    extract -> judge -> END
    failed -> END

Reflection loop is implicit: verifier sets status='acting' without
advancing step_index when the last action failed, and increments
reflect_count. When reflect_count >= REFLECT_MAX, status -> failed.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver
from langgraph.graph import END, START, StateGraph

from .nodes import (
    AgentDeps,
    make_act_node,
    make_classify_node,
    make_extract_node,
    make_judge_node,
    make_observe_node,
    make_plan_node,
    make_verify_node,
    route_after_act,
    route_after_extract,
    route_after_judge,
    route_after_plan,
    route_after_verify,
)
from .state import AgentState


def build_graph(deps: AgentDeps):
    """Return an uncompiled StateGraph. Caller supplies a checkpointer."""
    g = StateGraph(AgentState)
    g.add_node("classify", make_classify_node(deps))
    g.add_node("plan", make_plan_node(deps))
    g.add_node("act", make_act_node(deps))
    g.add_node("observe", make_observe_node(deps))
    g.add_node("verify", make_verify_node(deps))
    g.add_node("extract", make_extract_node(deps))
    g.add_node("judge", make_judge_node(deps))

    g.add_edge(START, "classify")
    g.add_edge("classify", "plan")
    g.add_conditional_edges("plan", route_after_plan, {"act": "act", "failed": END})
    g.add_conditional_edges(
        "act",
        route_after_act,
        {"observe": "observe", "extract": "extract", "failed": END},
    )
    g.add_edge("observe", "verify")
    g.add_conditional_edges(
        "verify",
        route_after_verify,
        {"act": "act", "extract": "extract", "plan": "plan", "failed": END},
    )
    g.add_conditional_edges(
        "extract",
        route_after_extract,
        {"judge": "judge", "failed": END},
    )
    g.add_conditional_edges(
        "judge",
        route_after_judge,
        {"extract": "extract", "end": END},
    )
    return g


async def run_sample(
    *,
    deps: AgentDeps,
    initial_state: AgentState,
    checkpoint_db: str | Path = "data/checkpoints.db",
    thread_id: str | None = None,
    recursion_limit: int = 40,
    compiled_graph: Any = None,
) -> dict[str, Any]:
    """Execute one sample end-to-end. Returns final state.

    Compatibility shim: legacy callers (tests, scripts) can invoke
    without a precompiled graph; the orchestrator prefers
    `invoke_compiled` below so the graph isn't rebuilt per sample.
    """
    Path(checkpoint_db).parent.mkdir(parents=True, exist_ok=True)
    if compiled_graph is not None:
        config = {
            "configurable": {
                "thread_id": thread_id or initial_state.get("sample_id", "t"),
            },
            "recursion_limit": recursion_limit,
        }
        return await compiled_graph.ainvoke(initial_state, config=config)

    async with AsyncSqliteSaver.from_conn_string(str(checkpoint_db)) as saver:
        graph = build_graph(deps).compile(checkpointer=saver)
        config = {
            "configurable": {
                "thread_id": thread_id or initial_state.get("sample_id", "t"),
            },
            "recursion_limit": recursion_limit,
        }
        final = await graph.ainvoke(initial_state, config=config)
        return final


async def invoke_compiled(
    compiled_graph: Any,
    *,
    initial_state: AgentState,
    thread_id: str,
    recursion_limit: int = 40,
) -> dict[str, Any]:
    """Fast path: caller has compiled the graph once and holds the saver
    context open across many samples."""
    config = {
        "configurable": {"thread_id": thread_id},
        "recursion_limit": recursion_limit,
    }
    return await compiled_graph.ainvoke(initial_state, config=config)
