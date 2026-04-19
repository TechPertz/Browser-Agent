"""FastAPI route tests — TestClient, no network, no Chromium."""

import base64
import json
import os
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from andera.api import create_app


@pytest.fixture(autouse=True)
def master_key(monkeypatch):
    monkeypatch.setenv("ANDERA_MASTER_KEY", base64.b64encode(os.urandom(32)).decode())


@pytest.fixture
def client(monkeypatch, tmp_path):
    # Isolate runs + credentials to tmp
    monkeypatch.chdir(tmp_path)
    return TestClient(create_app())


def test_health(client):
    r = client.get("/api/health")
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True
    # Always returns ok=true; queue_backend / metadata_backend are
    # best-effort (present when profile loads; profile_error populated
    # when it doesn't — e.g. test fixture chdir'd away from config/).
    assert "anthropic_configured" in body


def test_evidence_bad_sha_rejected(client):
    r = client.get("/api/evidence/not-a-sha")
    assert r.status_code == 400


def test_evidence_missing_returns_404(client):
    r = client.get("/api/evidence/" + "a" * 64)
    assert r.status_code == 404


def test_evidence_served_from_disk(client, tmp_path):
    # Seed a blob inside runs/<id>/blobs/
    sha = "b" * 64
    shard = tmp_path / "runs" / "run-1" / "blobs" / sha[:2]
    shard.mkdir(parents=True)
    (shard / f"{sha}.png").write_bytes(b"\x89PNG\r\n\x1a\n" + b"fake-data")
    r = client.get(f"/api/evidence/{sha}")
    assert r.status_code == 200
    assert r.headers["content-type"] == "image/png"
    assert r.content.startswith(b"\x89PNG")


def test_list_runs_hydrates_from_manifest(client, tmp_path):
    """A run with a RUN_MANIFEST.json on disk shows up even if not in-memory."""
    run_root = tmp_path / "runs" / "run-done"
    (run_root / "blobs" / "aa").mkdir(parents=True)
    manifest = {
        "version": 1,
        "run_id": "run-done",
        "generated_at": "2026-04-18T00:00:00+00:00",
        "task": {"task_id": "t1", "task_name": "Done", "prompt": "x"},
        "totals": {"samples": 3, "passed": 2, "failed": 1, "artifacts": 0},
        "samples": [
            {"sample_id": "s0", "row_index": 0, "verdict": "pass"},
            {"sample_id": "s1", "row_index": 1, "verdict": "pass"},
            {"sample_id": "s2", "row_index": 2, "verdict": "fail"},
        ],
        "artifacts": [],
        "audit_root_hash": "d" * 64,
    }
    (run_root / "RUN_MANIFEST.json").write_text(json.dumps(manifest))

    r = client.get("/api/runs")
    assert r.status_code == 200
    ids = [x["run_id"] for x in r.json()["runs"]]
    assert "run-done" in ids

    r = client.get("/api/runs/run-done")
    assert r.status_code == 200
    body = r.json()
    assert body["passed"] == 2
    assert body["failed"] == 1

    r = client.get("/api/runs/run-done/samples")
    assert r.status_code == 200
    assert len(r.json()["samples"]) == 3


def test_run_not_found_404(client):
    r = client.get("/api/runs/missing-run")
    assert r.status_code == 404


def test_connections_empty_when_no_sealed_state(client):
    r = client.get("/api/connections")
    assert r.status_code == 200
    assert r.json() == {"hosts": []}


def test_create_run_rejects_missing_files(client, tmp_path):
    r = client.post("/api/runs", json={
        "task_path": str(tmp_path / "nope.yaml"),
        "input_path": str(tmp_path / "nope.csv"),
    })
    assert r.status_code == 400


def test_events_websocket_handshake(client):
    """WS accepts, sends ws.ready, then forwards published events."""
    from andera.api.ws import get_bus
    with client.websocket_connect("/api/events?run_id=abc") as ws:
        hello = json.loads(ws.receive_text())
        assert hello["kind"] == "ws.ready"
        assert hello["run_id"] == "abc"
        get_bus().publish({"kind": "sample.started", "run_id": "abc", "payload": {}})
        event = json.loads(ws.receive_text())
        assert event["kind"] == "sample.started"
        assert event["run_id"] == "abc"


def test_events_scoped_by_run_id(client):
    from andera.api.ws import get_bus
    # subscriber on run=a should NOT see events for run=b
    with client.websocket_connect("/api/events?run_id=a") as ws_a:
        json.loads(ws_a.receive_text())  # consume ready
        get_bus().publish({"kind": "x", "run_id": "b", "payload": {}})
        get_bus().publish({"kind": "y", "run_id": "a", "payload": {}})
        event = json.loads(ws_a.receive_text())
        assert event["run_id"] == "a"
        assert event["kind"] == "y"
