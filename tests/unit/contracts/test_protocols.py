from pathlib import Path

from andera.contracts import Artifact, ArtifactStore, BrowserSession, ChatModel, TaskQueue


class _StubBrowser:
    async def goto(self, url): ...
    async def click(self, s): ...
    async def type(self, s, v): ...
    async def screenshot(self, n): ...
    async def extract(self, s): ...
    async def snapshot(self): ...
    async def close(self): ...


class _StubQueue:
    async def enqueue(self, item): return "id"
    async def dequeue(self): return None
    async def ack(self, item_id): ...
    async def nack(self, item_id, reason): ...
    async def dead_letter(self, item_id): ...


class _StubStore:
    async def put(self, content, name, mime, **tags): ...
    async def get(self, sha): return b""
    def local_path(self, artifact: Artifact) -> Path: return Path("/tmp")


class _StubModel:
    async def complete(self, messages, schema=None, **kwargs): return {"role": "assistant"}


def test_browser_session_protocol():
    assert isinstance(_StubBrowser(), BrowserSession)


def test_task_queue_protocol():
    assert isinstance(_StubQueue(), TaskQueue)


def test_artifact_store_protocol():
    assert isinstance(_StubStore(), ArtifactStore)


def test_chat_model_protocol():
    assert isinstance(_StubModel(), ChatModel)
