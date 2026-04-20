"""Role -> ChatModel resolution.

Reads the role mapping from `profile.models` and returns a ChatModel
instance. Swapping models is a config change (profile.yaml), never a
code change.
"""

from __future__ import annotations

import os
from functools import lru_cache

from andera.config.loader import Profile

from .adapters.anthropic_direct import AnthropicDirectModel
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


# Roles whose calls involve strict JSON schemas (structured output).
# These bypass LiteLLM because LiteLLM's Anthropic bridge:
#   (a) silently injects `additionalProperties: false` into the schema,
#       causing 60s+ hangs;
#   (b) defaults `max_tokens` to the context window (128k on Opus 4),
#       which Anthropic 400s with opaque errors;
#   (c) routes through a still-changing `structured-outputs` beta.
# The direct Anthropic SDK uses the stable `tool_use` forced-tool
# pattern and returns in ~2s. Other roles (navigator, extractor, judge)
# keep using LiteLLM where the text-only path is stable.
_DIRECT_ROLES = {Role.PLANNER, Role.VISION}


@lru_cache(maxsize=4)
def _cached_direct_model(model: str, api_key: str | None) -> AnthropicDirectModel:
    return AnthropicDirectModel(model=model, api_key=api_key)


def get_model(role: Role, profile: Profile):
    """Return the ChatModel configured for `role` in this profile."""
    spec = getattr(profile.models, role.value)
    api_key = _resolve_key(spec.provider)
    if role in _DIRECT_ROLES:
        if spec.provider != "anthropic":
            # Keeping this strict until we have a second backend tested
            # end-to-end. Silently falling through to LiteLLM here puts
            # us back on the hang path.
            raise NotImplementedError(
                f"role={role.value} currently only supports anthropic; "
                f"got provider={spec.provider!r}",
            )
        return _cached_direct_model(spec.model, api_key)
    return _cached_model(spec.provider, spec.model, api_key)
