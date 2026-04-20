"""LiteLLM adapter implementing the `ChatModel` Protocol.

One adapter handles every provider LiteLLM supports (Anthropic today,
OpenAI/Gemini/Ollama tomorrow). The model string is prefixed with the
provider per LiteLLM's convention (e.g. `anthropic/claude-opus-4-7`).
"""

from __future__ import annotations

import json
import os
import traceback
from typing import Any

import litellm

# Opt-in verbose LiteLLM logging. Set `LITELLM_DEBUG=1` in .env (or
# export it) and restart the API to see full request/response payloads
# — invaluable for diagnosing silent hangs where LiteLLM only prints
# "Give Feedback / Get Help" banners with no actual error text.
if os.environ.get("LITELLM_DEBUG", "").lower() in ("1", "true", "yes", "on"):
    try:
        litellm._turn_on_debug()
    except Exception:
        # Older litellm versions exposed this via a different path;
        # fall back to setting the verbose flag directly.
        litellm.set_verbose = True  # type: ignore[attr-defined]


class LiteLLMChatModel:
    """Concrete ChatModel backed by LiteLLM async completion."""

    def __init__(
        self,
        provider: str,
        model: str,
        *,
        api_key: str | None = None,
        default_temperature: float = 0.2,
        num_retries: int = 3,
        request_timeout: float = 60.0,
    ) -> None:
        self.provider = provider
        self.model = model
        self._model_string = f"{provider}/{model}" if "/" not in model else model
        self._api_key = api_key
        self._default_temperature = default_temperature
        self._num_retries = num_retries
        self._request_timeout = request_timeout

    async def complete(
        self,
        messages: list[dict[str, Any]],
        schema: dict[str, Any] | None = None,
        **kwargs: Any,
    ) -> dict[str, Any]:
        """Return a normalized message dict: {role, content, parsed?}.

        If `schema` is provided, enforces JSON-mode structured output and
        returns the parsed JSON under `parsed`. If the model fails to
        emit valid JSON we raise — callers decide whether to retry.
        """
        params: dict[str, Any] = {
            "model": self._model_string,
            "messages": messages,
            # LiteLLM handles retry+backoff natively; these cover 429 and
            # transient timeout failures without cluttering the node code.
            "num_retries": self._num_retries,
            "timeout": self._request_timeout,
        }
        # `temperature` is deprecated on newer Anthropic models (Opus 4.7,
        # Sonnet 4.6). Only pass it when the caller explicitly asks.
        if "temperature" in kwargs:
            params["temperature"] = kwargs.pop("temperature")
        if self._api_key is not None:
            params["api_key"] = self._api_key
        if schema is not None:
            params["response_format"] = {
                "type": "json_schema",
                "json_schema": {
                    "name": schema.get("title", "response"),
                    "strict": True,
                    "schema": schema,
                },
            }
        params.update(kwargs)

        try:
            resp = await litellm.acompletion(**params)
        except Exception as e:
            # LiteLLM's own logging prints "Give Feedback" banners without
            # the actual error, which makes silent hangs unfixable. Surface
            # the exception class + message + truncated request so the
            # parent node's logger catches it.
            msg = str(e)[:500]
            print(
                f"[LiteLLM adapter] {type(e).__name__}: {msg}\n"
                f"  model={self._model_string} has_schema={schema is not None} "
                f"messages={len(messages)}",
                flush=True,
            )
            if os.environ.get("LITELLM_DEBUG"):
                traceback.print_exc()
            raise
        choice = resp.choices[0]
        content = choice.message.content or ""
        out: dict[str, Any] = {"role": "assistant", "content": content}
        if schema is not None:
            try:
                out["parsed"] = json.loads(content)
            except Exception as e:
                print(
                    f"[LiteLLM adapter] JSON parse failed: {e}\n"
                    f"  raw content (first 500): {content[:500]!r}",
                    flush=True,
                )
                raise
        usage = getattr(resp, "usage", None)
        if usage is not None:
            out["usage"] = {
                "prompt_tokens": getattr(usage, "prompt_tokens", None),
                "completion_tokens": getattr(usage, "completion_tokens", None),
                "total_tokens": getattr(usage, "total_tokens", None),
            }
        return out
