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


async def test_subfolder_hardlink_placed(store, tmp_path):
    """Human-readable subfolder copy lands next to the blob."""
    art = await store.put(b"png-bytes", "pr_01.png", subfolder="facebook-react")
    human = tmp_path / "facebook-react" / "pr_01.png"
    assert human.exists()
    assert human.read_bytes() == b"png-bytes"
    # Same inode as the blob -> hardlink succeeded (not a copy).
    blob = tmp_path / "blobs" / art.sha256[:2] / f"{art.sha256}.png"
    assert human.stat().st_ino == blob.stat().st_ino


async def test_subfolder_same_content_idempotent(store, tmp_path):
    """Calling put twice with same bytes + same folder+name is a no-op."""
    await store.put(b"same-bytes", "pr_01.png", subfolder="microsoft-vscode")
    await store.put(b"same-bytes", "pr_01.png", subfolder="microsoft-vscode")
    human_dir = tmp_path / "microsoft-vscode"
    # Only one file — the second call detected same-inode and skipped.
    files = list(human_dir.iterdir())
    assert len(files) == 1


async def test_subfolder_different_content_same_name_disambiguated(store, tmp_path):
    """Two screenshots with the same target but different content both land.

    Before the fix, the second hardlink was silently skipped because
    `human_path.exists()` returned True. That lost evidence in the
    user-visible folder.
    """
    a1 = await store.put(b"first-shot", "closed_prs.png", subfolder="repo")
    a2 = await store.put(b"second-shot", "closed_prs.png", subfolder="repo")
    assert a1.sha256 != a2.sha256
    human_dir = tmp_path / "repo"
    names = {p.name for p in human_dir.iterdir()}
    # Both present: the original name + a sha-disambiguated sibling.
    assert len(names) == 2
    assert "closed_prs.png" in names
    assert any(
        n != "closed_prs.png" and n.startswith("closed_prs-") and n.endswith(".png")
        for n in names
    )


async def test_subfolder_sanitizes_unsafe_names(store, tmp_path):
    """Subfolder + name are sanitized — LLM can't escape the run root."""
    await store.put(b"x", "../../../etc/passwd", subfolder="../../evil")
    # Nothing outside tmp_path was created.
    assert not (tmp_path.parent / "etc").exists()
    assert not (tmp_path.parent / "evil").exists()
