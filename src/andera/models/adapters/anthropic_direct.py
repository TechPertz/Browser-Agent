"""Direct Anthropic SDK adapter — used for planner + visual resolver.

Why not LiteLLM for these roles?

1. Strict JSON schema hangs. LiteLLM's translation of
   `response_format={type: json_schema, strict: true}` on the Anthropic
   backend silently hangs for 60+ seconds and returns opaque errors.
   We proved this with a minimal repro.
2. `max_tokens` default bug. LiteLLM's model-cost table for Claude
   Opus 4 reports 128,000 as the context window, and LiteLLM uses that
   value as the *output* `max_tokens` default when the caller doesn't
   set one. Anthropic 400s anything above 8192 output tokens on Opus —
   so every plan call 400s with zero useful error text.
3. Silent schema mutation. LiteLLM injects `additionalProperties:
   false` into the schema when translating to Anthropic's beta
   structured-outputs endpoint. That combo was exactly what caused the
   original hang; we deliberately omit it.

This adapter takes the same {messages, schema} shape and uses
Anthropic's native `tool_use` forced-tool pattern for structured
output — which is what Anthropic actually recommends and is rock solid
in ~2s. Navigator/extractor/judge keep using LiteLLM where the
text-only + no-schema path works fine.

Implements the ChatModel Protocol so callers don't change.
"""

from __future__ import annotations

import os
from typing import Any

from anthropic import AsyncAnthropic


class AnthropicDirectModel:
    """ChatModel-compatible wrapper around the Anthropic SDK.

    Handles both text-only (planner) and multimodal (vision resolver)
    calls. Forced structured output via `tool_use` when a schema is
    provided. Returns {role, content, parsed, usage} to match the
    LiteLLM adapter's shape so nodes.py doesn't need to branch.
    """

    def __init__(
        self,
        *,
        model: str = "claude-opus-4-7",
        api_key: str | None = None,
        max_tokens: int = 4096,
        timeout: float = 60.0,
    ) -> None:
        key = api_key or os.environ.get("ANTHROPIC_API_KEY")
        if not key:
            raise RuntimeError(
                "AnthropicDirectModel needs ANTHROPIC_API_KEY in env or passed "
                "explicitly — it does not fall through LiteLLM.",
            )
        # Provider prefix is sometimes present on model strings coming
        # from the profile ("anthropic/claude-opus-4-7"). Strip it — the
        # SDK wants the bare model id.
        if model.startswith("anthropic/"):
            model = model[len("anthropic/"):]
        self._client = AsyncAnthropic(api_key=key, timeout=timeout)
        self._model = model
        self._max_tokens = max_tokens

    @property
    def provider(self) -> str:
        return "anthropic"

    @property
    def model(self) -> str:
        return self._model

    async def complete(
        self,
        messages: list[dict[str, Any]],
        schema: dict[str, Any] | None = None,
        **kwargs: Any,
    ) -> dict[str, Any]:
        """Run the model and return {role, content, parsed, usage}.

        Anthropic doesn't have a `response_format=json_schema` knob like
        OpenAI. The idiomatic pattern for forced structured output is a
        `tools=[{…}]` declaration + `tool_choice` pointing at it — the
        model is then required to emit exactly that tool's input, which
        we pull out as the parsed result.
        """
        system, chat = _split_system(messages)

        tool_name = schema.get("title", "structured_output") if schema else ""
        tools = None
        tool_choice = None
        if schema is not None:
            tools = [{
                "name": tool_name,
                "description": "Emit the structured output for this task.",
                "input_schema": schema,
            }]
            tool_choice = {"type": "tool", "name": tool_name}

        call_kwargs: dict[str, Any] = {
            "model": self._model,
            "max_tokens": self._max_tokens,
            "messages": chat,
        }
        if system:
            call_kwargs["system"] = system
        if tools:
            call_kwargs["tools"] = tools
            call_kwargs["tool_choice"] = tool_choice
        if "temperature" in kwargs:
            call_kwargs["temperature"] = kwargs.pop("temperature")

        resp = await self._client.messages.create(**call_kwargs)

        parsed = None
        content_text = ""
        for block in resp.content or []:
            if block.type == "tool_use":
                parsed = block.input
            elif block.type == "text":
                content_text += block.text

        out: dict[str, Any] = {
            "role": "assistant",
            "content": content_text,
        }
        if parsed is not None:
            out["parsed"] = parsed
        usage = resp.usage
        if usage is not None:
            out["usage"] = {
                "prompt_tokens": usage.input_tokens,
                "completion_tokens": usage.output_tokens,
                "total_tokens": (usage.input_tokens or 0) + (usage.output_tokens or 0),
            }
        return out


def _split_system(
    messages: list[dict[str, Any]],
) -> tuple[str, list[dict[str, Any]]]:
    """Anthropic's API takes `system` as a top-level param, not as a
    message with role=system. Split the list accordingly."""
    system_parts: list[str] = []
    rest: list[dict[str, Any]] = []
    for m in messages:
        if m.get("role") == "system":
            c = m.get("content")
            if isinstance(c, str):
                system_parts.append(c)
            elif isinstance(c, list):
                # Collapse content blocks (rare for system) to text.
                for b in c:
                    if isinstance(b, dict) and b.get("type") == "text":
                        system_parts.append(b.get("text", ""))
        else:
            rest.append(m)
    return "\n\n".join(p for p in system_parts if p), rest
