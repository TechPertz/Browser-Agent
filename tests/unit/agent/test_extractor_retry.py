"""Extractor retry on schema-invalid output + judge feedback loop."""

import json

import pytest

from andera.agent import run_sample
from andera.agent.nodes import AgentDeps
from andera.contracts import Artifact
from andera.tools.browser import BrowserTools


class ScriptedModel:
    def __init__(self, script: list[str]):
        self._script = list(script)
        self.calls: list[dict] = []

    async def complete(self, messages, schema=None, **kwargs):
        self.calls.append({"messages": messages, "schema": schema})
        content = self._script.pop(0)
        out = {"role": "assistant", "content": content}
        if schema is not None:
            out["parsed"] = json.loads(content)
        return out


class FakeSession:
    async def goto(self, url): ...
    async def click(self, s): ...
    async def type(self, s, v): ...
    async def screenshot(self, name):
        return Artifact(sha256="a" * 64, name=f"{name}.png", mime="image/png", size=10, path="/tmp/x.png")
    async def extract(self, schema): return {}
    async def snapshot(self):
        return {"url": "https://x/1", "title": "T", "inner_text": "body", "interactive": []}
    async def close(self): ...


SCHEMA = {
    "type": "object",
    "required": ["title", "author"],
    "properties": {
        "title": {"type": "string"},
        "author": {"type": "string"},
    },
}


@pytest.mark.asyncio
async def test_extractor_retries_on_schema_invalid(tmp_path):
    """First extract call returns invalid JSON -> extractor retries with
    validation errors embedded -> second call returns valid output."""
    plan = json.dumps([
        {"action": "goto", "target": "https://x/1"},
        {"action": "done", "target": "ok"},
    ])
    verify_ok = json.dumps({"ok": True, "reason": "loaded"})
    # First extract is missing 'author' (required) — schema-invalid.
    bad_extract = json.dumps({"title": "Hello"})
    # Second extract is valid after being shown the errors.
    good_extract = json.dumps({"title": "Hello", "author": "octocat"})
    judge_pass = json.dumps({"verdict": "pass", "reason": "both fields present"})

    planner = ScriptedModel([plan])
    # classifier + verify
    navigator = ScriptedModel([verify_ok])
    extractor = ScriptedModel([bad_extract, good_extract])
    judge = ScriptedModel([judge_pass])
    classifier = ScriptedModel([json.dumps({"task_type": "extract"})])

    deps = AgentDeps(
        planner=planner, navigator=navigator,
        extractor=extractor, judge=judge, classifier=classifier,
        browser=BrowserTools(FakeSession()),
    )
    initial = {
        "run_id": "r", "sample_id": "s-ret", "task_prompt": "get fields",
        "input_data": {}, "start_url": "https://x/1",
        "extract_schema": SCHEMA, "status": "pending",
    }
    final = await run_sample(
        deps=deps, initial_state=initial,
        checkpoint_db=tmp_path / "c.db", thread_id="s-ret",
    )
    # Retry happened: extractor called twice.
    assert len(extractor.calls) == 2
    assert final["extracted"] == {"title": "Hello", "author": "octocat"}
    assert final["verdict"] == "pass"
    # Second call includes validation errors in the user message
    second_user = extractor.calls[1]["messages"][-1]["content"]
    assert "validation errors" in second_user.lower()


@pytest.mark.asyncio
async def test_judge_fail_loops_back_to_extract(tmp_path):
    """Judge says 'fail' -> route back to extract once with judge feedback,
    then re-judge. Bounded by reflect_count so we never loop forever."""
    plan = json.dumps([
        {"action": "goto", "target": "https://x/1"},
        {"action": "done", "target": "ok"},
    ])
    verify_ok = json.dumps({"ok": True, "reason": "loaded"})
    # First extract returns wrong data; second corrects after judge feedback.
    first_extract = json.dumps({"title": "Wrong", "author": "unknown"})
    corrected_extract = json.dumps({"title": "Right", "author": "octocat"})
    judge_fail = json.dumps({"verdict": "fail", "reason": "title does not match evidence"})
    judge_pass = json.dumps({"verdict": "pass", "reason": "corrected"})

    planner = ScriptedModel([plan])
    navigator = ScriptedModel([verify_ok])
    extractor = ScriptedModel([first_extract, corrected_extract])
    judge = ScriptedModel([judge_fail, judge_pass])
    classifier = ScriptedModel([json.dumps({"task_type": "extract"})])

    deps = AgentDeps(
        planner=planner, navigator=navigator,
        extractor=extractor, judge=judge, classifier=classifier,
        browser=BrowserTools(FakeSession()),
    )
    initial = {
        "run_id": "r", "sample_id": "s-jfb", "task_prompt": "get fields",
        "input_data": {}, "start_url": "https://x/1",
        "extract_schema": SCHEMA, "status": "pending",
    }
    final = await run_sample(
        deps=deps, initial_state=initial,
        checkpoint_db=tmp_path / "c.db", thread_id="s-jfb",
    )
    # Judge ran twice; extractor ran twice.
    assert len(judge.calls) == 2
    assert len(extractor.calls) == 2
    # Final verdict is pass.
    assert final["verdict"] == "pass"
    assert final["extracted"] == {"title": "Right", "author": "octocat"}
    # Second extract call received judge feedback
    second_user = extractor.calls[1]["messages"][-1]["content"]
    assert "judge feedback" in second_user.lower()
    assert "title does not match" in second_user.lower()


@pytest.mark.asyncio
async def test_garbled_verifier_does_not_silently_pass(tmp_path):
    """Garbled verifier output must NOT be treated as ok=True.

    This is the regression test for the P0 accuracy bug where the old
    verifier defaulted to ok=True on parse failure.
    """
    plan = json.dumps([
        {"action": "goto", "target": "https://x/1"},
        {"action": "goto", "target": "https://x/2"},
        {"action": "done", "target": "ok"},
    ])
    # Two garbled verifier responses — must be treated as not-ok, triggering
    # reflection + eventually replan. We then cap replanning too.
    garbled = "this is not json lol"
    replanner_noise = "also not json"
    planner = ScriptedModel([plan, replanner_noise])
    navigator = ScriptedModel([garbled, garbled, garbled])
    extractor = ScriptedModel([])
    judge = ScriptedModel([])
    classifier = ScriptedModel([json.dumps({"task_type": "extract"})])

    deps = AgentDeps(
        planner=planner, navigator=navigator,
        extractor=extractor, judge=judge, classifier=classifier,
        browser=BrowserTools(FakeSession()),
    )
    initial = {
        "run_id": "r", "sample_id": "s-grb", "task_prompt": "x",
        "input_data": {}, "start_url": "https://x/1",
        "extract_schema": SCHEMA, "status": "pending",
    }
    final = await run_sample(
        deps=deps, initial_state=initial,
        checkpoint_db=tmp_path / "c.db", thread_id="s-grb",
    )
    # The run must NOT have succeeded silently. Old buggy behavior would
    # have advanced through all 3 plan steps and extracted {}.
    assert final.get("verdict") != "pass"
