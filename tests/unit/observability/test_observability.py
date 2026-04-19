import json

import litellm
import pytest

from andera.config import load_profile
from andera.observability.langfuse_adapter import install_langfuse_if_enabled
from andera.observability.trace import JsonlTraceSink


@pytest.fixture
def profile():
    return load_profile()


def test_jsonl_sink_writes_one_line_per_event(tmp_path):
    sink = JsonlTraceSink(tmp_path)
    sink.write({"kind": "run.init", "run_id": "r1"})
    sink.write({"kind": "sample.completed", "run_id": "r1", "sample_id": "s0"})
    files = list(tmp_path.iterdir())
    assert len(files) == 1  # one file per day
    lines = [json.loads(l) for l in files[0].read_text().splitlines() if l.strip()]
    assert len(lines) == 2
    assert lines[0]["kind"] == "run.init"
    assert "ts" in lines[0]  # auto-stamped


def test_langfuse_disabled_when_profile_says_no(profile):
    """Default profile has langfuse.enabled=false; installer returns False."""
    assert profile.observability.langfuse.enabled is False
    out = install_langfuse_if_enabled(profile)
    assert out is False


def test_langfuse_requires_env_keys(profile, monkeypatch):
    """Even enabled, missing env keys -> installer returns False (warn-only)."""
    profile.observability.langfuse.enabled = True
    monkeypatch.delenv("LANGFUSE_PUBLIC_KEY", raising=False)
    monkeypatch.delenv("LANGFUSE_SECRET_KEY", raising=False)
    out = install_langfuse_if_enabled(profile)
    assert out is False


def test_langfuse_install_attaches_callback(profile, monkeypatch):
    """With SDK present + env keys set, installer attaches 'langfuse' to
    litellm.success_callback. If langfuse SDK is missing, skip cleanly."""
    pytest.importorskip("langfuse")
    profile.observability.langfuse.enabled = True
    monkeypatch.setenv("LANGFUSE_PUBLIC_KEY", "pk_test")
    monkeypatch.setenv("LANGFUSE_SECRET_KEY", "sk_test")

    # clear any prior state
    litellm.success_callback = []
    litellm.failure_callback = []

    out = install_langfuse_if_enabled(profile)
    assert out is True
    assert "langfuse" in litellm.success_callback
    assert "langfuse" in litellm.failure_callback

    # idempotent — second call doesn't duplicate
    install_langfuse_if_enabled(profile)
    assert litellm.success_callback.count("langfuse") == 1
