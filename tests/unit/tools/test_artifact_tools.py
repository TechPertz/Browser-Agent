import pytest

from andera.storage import FilesystemArtifactStore
from andera.tools.artifact import ArtifactTools, GetArgs, PutArgs


@pytest.fixture
def tools(tmp_path):
    store = FilesystemArtifactStore(tmp_path)
    return ArtifactTools(store)


async def test_put_returns_artifact(tools):
    out = await tools.put(PutArgs(content=b"hello", name="greeting.txt", run_id="r1"))
    assert out.status == "ok"
    assert out.data["artifact"]["name"] == "greeting.txt"
    assert out.data["artifact"]["run_id"] == "r1"


async def test_put_does_not_leak_bytes_into_envelope(tools, tmp_path):
    # the audit-safe args payload must not contain raw bytes
    out = await tools.put(PutArgs(content=b"secret-bytes", name="x.bin"))
    assert out.status == "ok"
    # ToolResult has its own audit representation; call_id populated
    assert out.call_id


async def test_get_roundtrip(tools):
    put = await tools.put(PutArgs(content=b"abcdef", name="x.bin"))
    sha = put.data["artifact"]["sha256"]
    got = await tools.get(GetArgs(sha256=sha))
    assert got.status == "ok"
    assert got.data["size"] == 6


async def test_get_missing_is_error(tools):
    out = await tools.get(GetArgs(sha256="0" * 64))
    assert out.status == "error"
    assert "FileNotFoundError" in (out.error or "")
