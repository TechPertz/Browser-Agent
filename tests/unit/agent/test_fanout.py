"""Fan-out extraction: one sample -> N output items.

Covers the Option C plumbing: array schemas route through a distinct
prompt/validate/default path in the extract node. Uses a scripted
extractor model so we can assert the LLM's output shape end-to-end.
"""

import json

import pytest

from andera.agent.nodes import (
    AgentDeps,
    _is_array_schema,
    _schema_errors,
    make_extract_node,
)


class _ScriptedModel:
    """Returns the next canned response; records the schema argument."""

    def __init__(self, script):
        self._script = list(script)
        self.schemas_seen = []

    async def complete(self, messages, schema=None, **kwargs):
        self.schemas_seen.append(schema)
        content = self._script.pop(0)
        return {"role": "assistant", "content": content}


def _deps(extractor):
    return AgentDeps(
        planner=None, navigator=None, extractor=extractor, judge=None,
        browser=None,
    )


def test_is_array_schema_detection():
    assert _is_array_schema({"type": "array", "items": {"type": "object"}})
    assert _is_array_schema({"items": {"type": "object"}})  # no explicit type
    assert not _is_array_schema({"type": "object"})
    assert not _is_array_schema({})


def test_schema_errors_array_walks_each_item():
    schema = {
        "type": "array",
        "items": {
            "type": "object",
            "properties": {"name": {"type": "string"}},
            "required": ["name"],
        },
    }
    errs = _schema_errors([{"name": "ok"}, {"not_name": "oops"}], schema)
    # Exactly one error, prefixed with the failing item index.
    assert len(errs) == 1
    assert errs[0].startswith("[1]")


def test_schema_errors_array_rejects_non_list():
    schema = {"type": "array", "items": {"type": "object"}}
    errs = _schema_errors({"a": 1}, schema)
    assert errs and "expected array" in errs[0]


@pytest.mark.asyncio
async def test_extract_node_array_mode_returns_list():
    schema = {
        "type": "array",
        "items": {
            "type": "object",
            "properties": {
                "pr_number": {"type": "integer"},
                "author": {"type": "string"},
            },
            "required": ["pr_number", "author"],
        },
    }
    extractor = _ScriptedModel([json.dumps([
        {"pr_number": 1, "author": "octocat"},
        {"pr_number": 2, "author": "pavelfeldman"},
    ])])
    node = make_extract_node(_deps(extractor))
    state = {
        "extract_schema": schema,
        "observations": [],
        "evidence": [],
    }
    out = await node(state)
    # Array mode MUST skip strict-JSON-mode schema (array top-level not
    # universally supported by providers). Assert None was passed.
    assert extractor.schemas_seen == [None]
    assert isinstance(out["extracted"], list)
    assert len(out["extracted"]) == 2
    assert out["extracted"][0]["author"] == "octocat"
    assert out["extract_errors"] == []


@pytest.mark.asyncio
async def test_extract_node_unwraps_items_wrapper():
    """Haiku sometimes emits {items: [...]} despite an 'array' instruction.
    The node should unwrap that to the bare list so downstream code sees
    a uniform shape."""
    schema = {"type": "array", "items": {"type": "object"}}
    extractor = _ScriptedModel([json.dumps({"items": [{"k": 1}, {"k": 2}]})])
    node = make_extract_node(_deps(extractor))
    out = await node({"extract_schema": schema, "observations": []})
    assert out["extracted"] == [{"k": 1}, {"k": 2}]


@pytest.mark.asyncio
async def test_extract_node_object_mode_passes_schema_through():
    """Object schemas continue to use strict JSON mode for reliability."""
    schema = {
        "type": "object",
        "properties": {"title": {"type": "string"}},
        "required": ["title"],
    }
    extractor = _ScriptedModel([json.dumps({"title": "hello"})])
    node = make_extract_node(_deps(extractor))
    out = await node({"extract_schema": schema, "observations": []})
    assert extractor.schemas_seen == [schema]
    assert out["extracted"] == {"title": "hello"}


def test_csv_rebuild_fanout(tmp_path):
    """One sample with a list of items produces N CSV rows + item_index."""
    from andera.orchestrator.runner import RunWorkflow

    # Build a minimal workflow just to exercise _rebuild_csv_from_jsonl.
    run_root = tmp_path / "runs" / "run-fanout"
    run_root.mkdir(parents=True)
    samples = run_root / "samples.jsonl"
    samples.write_text("\n".join([
        json.dumps({
            "sample_id": "s-00", "row_index": 0, "verdict": "pass",
            "extracted": [
                {"author": "octocat", "date": "2024-01-01"},
                {"author": "pavelfeldman", "date": "2024-02-02"},
            ],
            "evidence_count": 2,
        }),
        json.dumps({
            "sample_id": "s-01", "row_index": 1, "verdict": "pass",
            "extracted": [{"author": "torvalds", "date": "2024-03-03"}],
            "evidence_count": 1,
        }),
    ]) + "\n")

    # Instantiate just enough of the workflow for the CSV writer.
    wf = RunWorkflow.__new__(RunWorkflow)
    wf.run_root = run_root
    wf.samples_jsonl = samples

    csv_path = run_root / "output.csv"
    wf._rebuild_csv_from_jsonl(csv_path)

    lines = csv_path.read_text().strip().splitlines()
    # Header + 3 data rows (2 items from s-00 + 1 from s-01).
    assert lines[0] == "sample_id,row_index,item_index,verdict,author,date"
    assert len(lines) == 4
    assert "s-00,0,0,pass,octocat,2024-01-01" in lines
    assert "s-00,0,1,pass,pavelfeldman,2024-02-02" in lines
    assert "s-01,1,0,pass,torvalds,2024-03-03" in lines


def test_csv_rebuild_single_item_backcompat(tmp_path):
    """Samples with dict (non-list) extracted still produce one row each,
    no item_index column — preserves the shape every existing task uses."""
    from andera.orchestrator.runner import RunWorkflow

    run_root = tmp_path / "runs" / "run-single"
    run_root.mkdir(parents=True)
    samples = run_root / "samples.jsonl"
    samples.write_text("\n".join([
        json.dumps({
            "sample_id": "s-00", "row_index": 0, "verdict": "pass",
            "extracted": {"title": "T1", "author": "A1", "state": "open"},
            "evidence_count": 1,
        }),
        json.dumps({
            "sample_id": "s-01", "row_index": 1, "verdict": "pass",
            "extracted": {"title": "T2", "author": "A2", "state": "closed"},
            "evidence_count": 1,
        }),
    ]) + "\n")

    wf = RunWorkflow.__new__(RunWorkflow)
    wf.run_root = run_root
    wf.samples_jsonl = samples

    csv_path = run_root / "output.csv"
    wf._rebuild_csv_from_jsonl(csv_path)

    lines = csv_path.read_text().strip().splitlines()
    assert lines[0] == "sample_id,row_index,verdict,title,author,state"
    assert len(lines) == 3  # header + 2 rows


def test_csv_rebuild_mixed_fanout_and_single(tmp_path):
    """If ANY sample uses fan-out, the whole CSV gets the item_index
    column so consumers see a uniform shape."""
    from andera.orchestrator.runner import RunWorkflow

    run_root = tmp_path / "runs" / "run-mixed"
    run_root.mkdir(parents=True)
    samples = run_root / "samples.jsonl"
    samples.write_text("\n".join([
        json.dumps({
            "sample_id": "s-00", "row_index": 0, "verdict": "pass",
            "extracted": {"k": "single"},
        }),
        json.dumps({
            "sample_id": "s-01", "row_index": 1, "verdict": "pass",
            "extracted": [{"k": "multi1"}, {"k": "multi2"}],
        }),
    ]) + "\n")

    wf = RunWorkflow.__new__(RunWorkflow)
    wf.run_root = run_root
    wf.samples_jsonl = samples

    csv_path = run_root / "output.csv"
    wf._rebuild_csv_from_jsonl(csv_path)

    lines = csv_path.read_text().strip().splitlines()
    assert lines[0] == "sample_id,row_index,item_index,verdict,k"
    # 1 row from the single + 2 from the fan-out = 3 data rows.
    assert len(lines) == 4
