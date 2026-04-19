"""UI route tests — HTML served, HTMX fragments served, form round-trip."""

import base64
import json
import os
from pathlib import Path

import pytest
import yaml
from fastapi.testclient import TestClient

from andera.api import create_app


@pytest.fixture(autouse=True)
def master_key(monkeypatch):
    monkeypatch.setenv("ANDERA_MASTER_KEY", base64.b64encode(os.urandom(32)).decode())


@pytest.fixture
def client(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    return TestClient(create_app())


def test_root_redirects_to_runs(client):
    r = client.get("/", follow_redirects=False)
    assert r.status_code in (302, 303, 307)
    assert "/ui/runs" in r.headers["location"]


def test_runs_page_renders(client):
    r = client.get("/ui/runs")
    assert r.status_code == 200
    assert "<title>Runs" in r.text
    assert "htmx" in r.text.lower()


def test_runs_fragment_empty(client):
    r = client.get("/ui/runs/fragment")
    assert r.status_code == 200
    assert "No runs yet" in r.text


def test_runs_fragment_shows_completed_run(client, tmp_path):
    run_root = tmp_path / "runs" / "run-ok"
    (run_root / "blobs" / "aa").mkdir(parents=True)
    (run_root / "RUN_MANIFEST.json").write_text(json.dumps({
        "version": 1,
        "run_id": "run-ok",
        "generated_at": "2026-04-18T00:00:00+00:00",
        "task": {"task_id": "t", "task_name": "Test"},
        "totals": {"samples": 2, "passed": 2, "failed": 0, "artifacts": 0},
        "samples": [
            {"sample_id": "s0", "row_index": 0, "verdict": "pass"},
            {"sample_id": "s1", "row_index": 1, "verdict": "pass"},
        ],
        "artifacts": [],
        "audit_root_hash": "f" * 64,
    }))
    r = client.get("/ui/runs/fragment")
    assert r.status_code == 200
    assert "run-ok" in r.text
    assert "completed" in r.text


def test_new_run_page(client):
    r = client.get("/ui/runs/new")
    assert r.status_code == 200
    # NLP textarea + repeat + upload + extract_fields + multi_item.
    assert 'name="prompt"' in r.text
    assert 'name="repeat"' in r.text
    assert 'name="extract_fields"' in r.text
    assert 'name="multi_item"' in r.text
    assert 'type="file"' in r.text
    assert 'multipart/form-data' in r.text


def test_run_detail_404(client):
    r = client.get("/ui/runs/nope")
    assert r.status_code == 404


def test_run_detail_renders(client, tmp_path):
    run_root = tmp_path / "runs" / "run-d"
    (run_root / "blobs" / "aa").mkdir(parents=True)
    (run_root / "RUN_MANIFEST.json").write_text(json.dumps({
        "version": 1,
        "run_id": "run-d",
        "generated_at": "2026-04-18T00:00:00+00:00",
        "task": {"task_id": "t", "task_name": "T"},
        "totals": {"samples": 1, "passed": 1, "failed": 0, "artifacts": 0},
        "samples": [{"sample_id": "s0", "row_index": 0, "verdict": "pass"}],
        "artifacts": [],
        "audit_root_hash": "e" * 64,
    }))
    r = client.get("/ui/runs/run-d")
    assert r.status_code == 200
    assert "run-d" in r.text
    assert "Live events" in r.text
    # WebSocket url embedded for live updates
    assert "/api/events" in r.text


def test_sample_detail_renders(client, tmp_path):
    run_root = tmp_path / "runs" / "run-s"
    (run_root / "blobs" / "aa").mkdir(parents=True)
    (run_root / "RUN_MANIFEST.json").write_text(json.dumps({
        "version": 1,
        "run_id": "run-s",
        "generated_at": "2026-04-18T00:00:00+00:00",
        "task": {"task_id": "t"},
        "totals": {"samples": 1, "passed": 1, "failed": 0, "artifacts": 0},
        "samples": [{
            "sample_id": "s0", "row_index": 0, "verdict": "pass",
            "extracted": {"title": "Hello"},
        }],
        "artifacts": [
            {"sha256": "a" * 64, "name": "step_00.png", "size": 100, "path": "blobs/aa/" + "a" * 64 + ".png"},
        ],
        "audit_root_hash": "c" * 64,
    }))
    r = client.get("/ui/runs/run-s/samples/s0")
    assert r.status_code == 200
    assert "step_00.png" in r.text
    assert "Hello" in r.text
    assert "/api/screencast" in r.text  # live browser section


def test_create_run_form_validates(client):
    """Repeat without an uploaded file re-renders the form with an error."""
    r = client.post(
        "/ui/runs/create",
        data={"prompt": "do something", "repeat": "true"},
    )
    assert r.status_code == 400
    assert "requires an input file" in r.text


def test_create_run_form_happy_path(client, tmp_path, monkeypatch):
    """NLP prompt + CSV upload routes through the JSON API and redirects."""
    from andera.api.routes import runs as runs_route
    from andera.config import load_profile as real_load_profile

    class _FakeWF:
        def __init__(self, *a, **kw):
            self.audit = type("A", (), {"_on_append": None})()
        async def execute(self):
            return type("R", (), {
                "run_id": "fake", "total": 0, "passed": 0, "failed": 0,
                "run_root": "runs/fake",
            })()
    monkeypatch.setattr(runs_route, "RunWorkflow", _FakeWF)

    project_root = Path(__file__).resolve().parents[3]
    monkeypatch.setattr(
        runs_route, "load_profile",
        lambda: real_load_profile(project_root / "config" / "profile.yaml"),
    )

    csv_bytes = b"repo,url\nfacebook/react,https://github.com/facebook/react\n"
    r = client.post(
        "/ui/runs/create",
        data={
            "prompt": "go to each repo and screenshot the closed PRs page",
            "repeat": "true",
            "max_samples": "",
        },
        files={"input_file": ("rows.csv", csv_bytes, "text/csv")},
        follow_redirects=False,
    )
    assert r.status_code == 303
    assert "/ui/runs/" in r.headers["location"]


def test_connections_page(client):
    r = client.get("/ui/connections")
    assert r.status_code == 200
    assert "Connections" in r.text
    assert "No sealed sessions" in r.text  # empty initially
