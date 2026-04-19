"""WebSocket /api/screencast route tests — filtered frame relay."""

import base64
import os

import pytest
from fastapi.testclient import TestClient

from andera.api import create_app


@pytest.fixture(autouse=True)
def master_key(monkeypatch):
    monkeypatch.setenv("ANDERA_MASTER_KEY", base64.b64encode(os.urandom(32)).decode())


@pytest.fixture
def client(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    return TestClient(create_app())


def test_missing_sample_id_closes_ws(client):
    from starlette.websockets import WebSocketDisconnect
    with pytest.raises(WebSocketDisconnect):
        with client.websocket_connect("/api/screencast") as ws:
            ws.receive_text()


def test_forwards_only_matching_sample_id(client):
    from andera.api.ws import get_bus

    with client.websocket_connect("/api/screencast?sample_id=target") as ws:
        # Publish a frame for a different sample -> must NOT reach us
        get_bus().publish({"kind": "screencast.frame", "sample_id": "other", "data": "XXXX"})
        # Non-frame event -> must NOT reach us
        get_bus().publish({"kind": "sample.started", "sample_id": "target", "payload": {}})
        # Matching frame -> this we want
        get_bus().publish({"kind": "screencast.frame", "sample_id": "target", "data": "AAAA"})
        data = ws.receive_text()
        assert data == "AAAA"
