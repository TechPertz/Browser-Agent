"""Role -> ChatModel resolution.

Reads the role mapping from `profile.models` and returns a ChatModel
instance. Swapping models is a config change (profile.yaml), never a
code change.
"""

from __future__ import annotations

import os
from functools import lru_cache

from andera.config.loader import Profile

from .adapters.litellm_adapter import LiteLLMChatModel
from .roles import Role

# Env var holding the API key, indexed by provider name in profile.yaml.
_PROVIDER_KEY_ENV = {
    "anthropic": "ANTHROPIC_API_KEY",
    "openai": "OPENAI_API_KEY",
    "google": "GEMINI_API_KEY",
    "ollama": None,  # local, no key
}


def _resolve_key(provider: str) -> str | None:
    env_name = _PROVIDER_KEY_ENV.get(provider)
    if env_name is None:
        return None
    return os.environ.get(env_name)


@lru_cache(maxsize=16)
def _cached_model(provider: str, model: str, api_key: str | None) -> LiteLLMChatModel:
    return LiteLLMChatModel(provider=provider, model=model, api_key=api_key)


def get_model(role: Role, profile: Profile) -> LiteLLMChatModel:
    """Return the ChatModel configured for `role` in this profile."""
    spec = getattr(profile.models, role.value)
    api_key = _resolve_key(spec.provider)
    return _cached_model(spec.provider, spec.model, api_key)
