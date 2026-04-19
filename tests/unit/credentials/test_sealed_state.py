import base64
import os

import pytest

from andera.credentials.storage_state import (
    SealedStateStore,
    derive_key_from_env,
    seal,
    unseal,
)


@pytest.fixture(autouse=True)
def master_key(monkeypatch):
    key = base64.b64encode(os.urandom(32)).decode("ascii")
    monkeypatch.setenv("ANDERA_MASTER_KEY", key)
    return key


def test_seal_unseal_roundtrip():
    key = derive_key_from_env()
    sealed = seal(b"top-secret", key)
    assert unseal(sealed, key) == b"top-secret"


def test_different_keys_cannot_unseal(monkeypatch):
    k1 = derive_key_from_env()
    sealed = seal(b"data", k1)
    # flip master key
    monkeypatch.setenv("ANDERA_MASTER_KEY", base64.b64encode(os.urandom(32)).decode("ascii"))
    k2 = derive_key_from_env()
    with pytest.raises(Exception):
        unseal(sealed, k2)


def test_missing_master_key_raises(monkeypatch):
    monkeypatch.delenv("ANDERA_MASTER_KEY", raising=False)
    with pytest.raises(RuntimeError):
        derive_key_from_env()


def test_store_save_and_load(tmp_path):
    store = SealedStateStore(tmp_path)
    state = {"cookies": [{"name": "session", "value": "abc123"}], "origins": []}
    path = store.save("github", state)
    assert path.exists()
    # sealed bytes must not contain the plaintext
    assert b"abc123" not in path.read_bytes()
    assert store.load("github") == state


def test_store_list_and_delete(tmp_path):
    store = SealedStateStore(tmp_path)
    store.save("github", {"cookies": []})
    store.save("linear", {"cookies": []})
    assert sorted(store.list_hosts()) == ["github", "linear"]
    assert store.delete("github") is True
    assert store.list_hosts() == ["linear"]


def test_host_name_sanitized_no_path_escape(tmp_path):
    """Malicious host names must NOT escape the store root."""
    store = SealedStateStore(tmp_path)
    store.save("../../etc/passwd", {"cookies": []})
    # File must be inside tmp_path, not out at /etc/passwd
    files = list(tmp_path.glob("*.sealed"))
    assert len(files) == 1
    resolved = files[0].resolve()
    assert tmp_path.resolve() in resolved.parents
    assert "/" not in files[0].name
