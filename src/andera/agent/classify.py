"""Task classifier — maps a NL task to a specialist type.

Runs once at graph start (per sample), but cached per
(task_prompt_hash, schema_hash) so 1000 samples of the same task
classify once. Uses Haiku for cost: classification is a one-token
answer.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any, Literal

from andera.contracts import ChatModel

TaskType = Literal["extract", "form_fill", "list_iter", "navigate", "unknown"]

_VALID: set[str] = {"extract", "form_fill", "list_iter", "navigate", "unknown"}

CLASSIFIER_SYSTEM = """You classify browser-agent tasks into ONE of:
- "extract": single-page data extraction (screenshot + pull fields)
- "form_fill": fill form fields, submit, capture confirmation
- "list_iter": iterate a list of items (pagination / search results), extract per item
- "navigate": multi-step navigation across a system (nested pages, follow links)
- "unknown": none of the above

Respond with ONE JSON object: {"task_type": "<one of the above>"}.
Choose the MOST SPECIFIC type. If the task mentions "iterate", "for each", "all N", pick list_iter.
If the task mentions filling a form or submitting, pick form_fill.
If it's one page in one pull, pick extract. Else navigate."""


def classify_cache_key(task_prompt: str, schema: dict[str, Any]) -> str:
    blob = task_prompt.strip() + "\x1f" + json.dumps(schema or {}, sort_keys=True)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


async def classify_task(
    task_prompt: str,
    schema: dict[str, Any],
    classifier: ChatModel,
) -> TaskType:
    user = (
        f"Task: {task_prompt.strip()}\n\n"
        f"Target schema keys: {list((schema or {}).get('properties', {}).keys())}"
    )
    try:
        out = await classifier.complete(
            messages=[
                {"role": "system", "content": CLASSIFIER_SYSTEM},
                {"role": "user", "content": user},
            ],
            temperature=0.0,
        )
        content = (out.get("content") or "").strip()
        if content.startswith("```"):
            content = content.strip("`")
            if content.lower().startswith("json"):
                content = content.split("\n", 1)[1] if "\n" in content else ""
        parsed = json.loads(content)
        t = str(parsed.get("task_type", "")).strip().lower()
        if t in _VALID:
            return t  # type: ignore[return-value]
    except Exception:
        pass
    return "unknown"
