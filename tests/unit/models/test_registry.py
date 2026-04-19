from unittest.mock import AsyncMock, patch

import pytest

from andera.config import load_profile
from andera.contracts import ChatModel
from andera.models import Role, get_model
from andera.models.adapters.litellm_adapter import LiteLLMChatModel


@pytest.fixture
def profile():
    return load_profile()


def test_get_model_returns_chat_model_protocol(profile):
    m = get_model(Role.PLANNER, profile)
    assert isinstance(m, ChatModel)
    assert isinstance(m, LiteLLMChatModel)


def test_role_mapping_uses_profile(profile):
    planner = get_model(Role.PLANNER, profile)
    extractor = get_model(Role.EXTRACTOR, profile)
    assert planner.model == profile.models.planner.model
    assert extractor.model == profile.models.extractor.model
    assert planner.model != extractor.model  # different Claude tiers


def test_model_string_includes_provider_prefix(profile):
    m = get_model(Role.NAVIGATOR, profile)
    assert m._model_string.startswith("anthropic/")


def test_model_cached(profile):
    # same role -> same instance (cached)
    a = get_model(Role.JUDGE, profile)
    b = get_model(Role.JUDGE, profile)
    assert a is b


async def test_complete_calls_litellm(profile):
    m = get_model(Role.PLANNER, profile)
    fake_resp = type(
        "R",
        (),
        {
            "choices": [
                type("C", (), {"message": type("M", (), {"content": "hi there"})()})()
            ],
            "usage": type("U", (), {"prompt_tokens": 5, "completion_tokens": 2, "total_tokens": 7})(),
        },
    )()
    with patch("andera.models.adapters.litellm_adapter.litellm.acompletion", new=AsyncMock(return_value=fake_resp)):
        out = await m.complete(messages=[{"role": "user", "content": "hi"}])
    assert out["role"] == "assistant"
    assert out["content"] == "hi there"
    assert out["usage"]["total_tokens"] == 7


async def test_complete_with_schema_parses_json(profile):
    m = get_model(Role.EXTRACTOR, profile)
    fake_resp = type(
        "R",
        (),
        {
            "choices": [
                type("C", (), {"message": type("M", (), {"content": '{"x": 1}'})()})()
            ],
            "usage": None,
        },
    )()
    with patch("andera.models.adapters.litellm_adapter.litellm.acompletion", new=AsyncMock(return_value=fake_resp)) as mock:
        out = await m.complete(
            messages=[{"role": "user", "content": "extract"}],
            schema={"title": "X", "type": "object", "properties": {"x": {"type": "integer"}}},
        )
    assert out["parsed"] == {"x": 1}
    assert mock.call_args.kwargs["response_format"]["type"] == "json_schema"
