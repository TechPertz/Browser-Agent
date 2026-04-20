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
    async def screenshot(
        self, name: str, *, full_page: bool = True, folder: str | None = None,
    ) -> Artifact: ...
    async def scroll(self, amount: str | int) -> dict[str, Any]: ...
    async def scroll_to(self, target: str) -> dict[str, Any]: ...
    async def screenshot_chunks(
        self, name: str, *, folder: str | None = None,
    ) -> list[Artifact]: ...
    async def visit_each_link(
        self, *, url_pattern: str, limit: int = 10,
        name_template: str = "item_{i:02d}", folder: str | None = None,
        full_page: bool = False,
    ) -> dict[str, Any]: ...
    async def extract(self, schema: dict[str, Any]) -> dict[str, Any]: ...
    async def snapshot(self) -> dict[str, Any]: ...
    # Set-of-Mark visual grounding. Annotate draws numbered boxes on every
    # interactive element and captures a screenshot; click_mark / type_mark
    # execute by coordinates looked up from the most recent annotation.
    async def mark_and_screenshot(
        self, name: str,
    ) -> tuple[Artifact, list[dict[str, Any]]]: ...
    async def click_mark(self, mark_id: int) -> None: ...
    async def type_mark(self, mark_id: int, value: str) -> None: ...
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
