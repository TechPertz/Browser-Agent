"""End-to-end graph test with a scripted ChatModel + FakeSession.

No network, no real browser. Proves the state machine wires correctly:
plan -> act (goto, screenshot, extract) -> observe -> verify -> extract -> judge.
"""

import json
from pathlib import Path

import pytest

from andera.agent import run_sample
from andera.agent.nodes import AgentDeps
from andera.contracts import Artifact
from andera.tools.browser import BrowserTools


class ScriptedModel:
    """ChatModel stub that returns canned responses in order per role."""

    def __init__(self, script: list[str]):
        self._script = list(script)

    async def complete(self, messages, schema=None, **kwargs):
        content = self._script.pop(0)
        out = {"role": "assistant", "content": content}
        if schema is not None:
            out["parsed"] = json.loads(content)
        return out


class FakeSession:
    def __init__(self):
        self.urls_visited = []

    async def goto(self, url):
        self.urls_visited.append(url)

    async def click(self, s): ...
    async def type(self, s, v): ...

    async def screenshot(self, name):
        return Artifact(
            sha256="a" * 64, name=f"{name}.png", mime="image/png", size=10,
            path=f"/tmp/{name}.png",
        )

    async def extract(self, schema):
        return {"_probe": "ok"}

    async def snapshot(self):
        return {
            "url": "https://example.com/issue/1",
            "title": "Example Issue",
            "html_len": 500,
            "html_head": "<html><body>Example Issue</body></html>",
        }

    async def close(self): ...


@pytest.mark.asyncio
async def test_graph_runs_plan_to_judge(tmp_path):
    plan_json = json.dumps([
        {"action": "goto", "target": "https://example.com/issue/1"},
        {"action": "screenshot", "target": "issue_page"},
        {"action": "extract", "target": "fields"},
        {"action": "done", "target": "ok"},
    ])
    # Verifier is called once per plan step (3 non-done steps).
    verify_ok = json.dumps({"ok": True, "reason": "looks right"})
    extract_json = json.dumps({"title": "Example Issue", "author": "octocat", "state": "open"})
    judge_json = json.dumps({"verdict": "pass", "reason": "all fields present with evidence"})

    planner = ScriptedModel([plan_json])
    # navigator is used for verify; needs 3 responses
    navigator = ScriptedModel([verify_ok, verify_ok, verify_ok])
    extractor = ScriptedModel([extract_json])
    judge = ScriptedModel([judge_json])

    deps = AgentDeps(
        planner=planner,
        navigator=navigator,
        extractor=extractor,
        judge=judge,
        browser=BrowserTools(FakeSession()),
    )

    initial = {
        "run_id": "r1",
        "sample_id": "s1",
        "task_prompt": "extract issue fields",
        "input_data": {"url": "https://example.com/issue/1"},
        "start_url": "https://example.com/issue/1",
        "extract_schema": {
            "type": "object",
            "required": ["title", "author", "state"],
            "properties": {
                "title": {"type": "string"},
                "author": {"type": "string"},
                "state": {"type": "string"},
            },
        },
        "status": "pending",
    }

    final = await run_sample(
        deps=deps,
        initial_state=initial,
        checkpoint_db=tmp_path / "ckpt.db",
        thread_id="s1",
    )

    assert final["status"] == "done"
    assert final["verdict"] == "pass"
    assert final["extracted"] == {"title": "Example Issue", "author": "octocat", "state": "open"}
    assert len(final["evidence"]) == 1  # one screenshot


@pytest.mark.asyncio
async def test_graph_fails_fast_on_bad_plan(tmp_path):
    # Planner returns garbage -> plan node sets status=failed -> route to END
    planner = ScriptedModel(["not json at all"])
    navigator = ScriptedModel([])
    extractor = ScriptedModel([])
    judge = ScriptedModel([])
    deps = AgentDeps(planner=planner, navigator=navigator, extractor=extractor, judge=judge,
                     browser=BrowserTools(FakeSession()))
    final = await run_sample(
        deps=deps,
        initial_state={"run_id": "r1", "sample_id": "s2", "task_prompt": "x", "status": "pending",
                       "extract_schema": {"type": "object"}},
        checkpoint_db=tmp_path / "ckpt2.db",
        thread_id="s2",
    )
    assert final["status"] == "failed"
    assert "plan parse" in final.get("error", "")
