"""Protocols (ports) that external dependencies implement.

All code depends on these abstractions, not concrete backends. Swaps flow
through `config/profile.yaml` per the one-switch-panel rule.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Protocol, runtime_checkable

from .artifact import Artifact


@runtime_checkable
class BrowserSession(Protocol):
    """A single browser tab / context. One per sample during execution."""

    async def goto(self, url: str) -> None: ...
    async def click(self, selector_or_text: str) -> None: ...
    async def type(self, selector: str, value: str) -> None: ...
    async def screenshot(self, name: str) -> Artifact: ...
    async def extract(self, schema: dict[str, Any]) -> dict[str, Any]: ...
    async def snapshot(self) -> dict[str, Any]: ...
    async def close(self) -> None: ...


@runtime_checkable
class TaskQueue(Protocol):
    """Durable queue for samples awaiting execution."""

    async def enqueue(self, item: dict[str, Any]) -> str: ...
    async def dequeue(self) -> dict[str, Any] | None: ...
    async def ack(self, item_id: str) -> None: ...
    async def nack(self, item_id: str, reason: str) -> None: ...
    async def dead_letter(self, item_id: str) -> None: ...


@runtime_checkable
class ArtifactStore(Protocol):
    """Content-addressed store for evidence artifacts."""

    async def put(
        self, content: bytes, name: str, mime: str, **tags: Any
    ) -> Artifact: ...
    async def get(self, sha: str) -> bytes: ...
    def local_path(self, artifact: Artifact) -> Path: ...


@runtime_checkable
class ChatModel(Protocol):
    """LLM chat completion. LiteLLM adapter implements this for all providers."""

    async def complete(
        self,
        messages: list[dict[str, Any]],
        schema: dict[str, Any] | None = None,
        **kwargs: Any,
    ) -> dict[str, Any]: ...
