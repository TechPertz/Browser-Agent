import hashlib

import pytest

from andera.contracts import ArtifactStore
from andera.storage import FilesystemArtifactStore


@pytest.fixture
def store(tmp_path):
    return FilesystemArtifactStore(tmp_path)


def test_satisfies_protocol(store):
    assert isinstance(store, ArtifactStore)


async def test_put_returns_content_addressed_artifact(store):
    content = b"hello world"
    art = await store.put(content, "hello.txt")
    assert art.sha256 == hashlib.sha256(content).hexdigest()
    assert art.size == len(content)
    assert art.mime == "text/plain"
    assert art.path.endswith(".txt")


async def test_dedup_same_content(store):
    a1 = await store.put(b"same", "a.txt")
    a2 = await store.put(b"same", "b.txt")
    assert a1.sha256 == a2.sha256
    assert a1.path == a2.path  # same on-disk file


async def test_get_roundtrip(store):
    art = await store.put(b"payload", "x.bin", mime="application/octet-stream")
    assert await store.get(art.sha256) == b"payload"


async def test_get_missing_raises(store):
    with pytest.raises(FileNotFoundError):
        await store.get("0" * 64)


async def test_tags_persisted_on_artifact(store):
    art = await store.put(b"data", "x.png", sample_id="s1", run_id="r1")
    assert art.sample_id == "s1"
    assert art.run_id == "r1"


async def test_local_path(store):
    art = await store.put(b"content", "foo.json")
    p = store.local_path(art)
    assert p.exists()
    assert p.read_bytes() == b"content"
