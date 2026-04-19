import pytest

from andera.config import Profile, load_profile


def test_default_profile_loads():
    p = load_profile()
    assert isinstance(p, Profile)
    assert p.models.planner.provider == "anthropic"
    assert p.models.navigator.model.startswith("claude-")


def test_browser_defaults():
    p = load_profile()
    assert 1 <= p.browser.concurrency <= 64
    assert p.browser.backend in {"local", "browserbase"}


def test_integrations_present():
    p = load_profile()
    assert "github" in p.integrations
    assert p.integrations["github"].token_env == "GITHUB_TOKEN"


def test_missing_profile_raises(tmp_path):
    with pytest.raises(FileNotFoundError):
        load_profile(tmp_path / "nope.yaml")
