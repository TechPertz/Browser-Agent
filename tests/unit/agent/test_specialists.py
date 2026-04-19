"""Specialist classifier + prompt routing tests."""

import json

import pytest

from andera.agent.classify import classify_task
from andera.agent.specialists import (
    EXTRACT_SPECIALIST_SYSTEM,
    FORM_FILL_SPECIALIST_SYSTEM,
    GENERIC_SPECIALIST_SYSTEM,
    LIST_ITER_SPECIALIST_SYSTEM,
    NAVIGATE_SPECIALIST_SYSTEM,
    system_prompt_for,
)


class Scripted:
    def __init__(self, content: str):
        self._content = content

    async def complete(self, messages, schema=None, **kwargs):
        return {"role": "assistant", "content": self._content}


@pytest.mark.asyncio
async def test_classifier_extract():
    model = Scripted('{"task_type": "extract"}')
    t = await classify_task("Grab title from GitHub issue", {"properties": {"title": {}}}, model)
    assert t == "extract"


@pytest.mark.asyncio
async def test_classifier_form_fill():
    model = Scripted('{"task_type": "form_fill"}')
    t = await classify_task("Fill out Workday form and submit", {"properties": {"confirmation": {}}}, model)
    assert t == "form_fill"


@pytest.mark.asyncio
async def test_classifier_list_iter():
    model = Scripted('{"task_type": "list_iter"}')
    t = await classify_task("Iterate 60 commits and screenshot each", {"properties": {"sha": {}}}, model)
    assert t == "list_iter"


@pytest.mark.asyncio
async def test_classifier_tolerates_fence():
    model = Scripted('```json\n{"task_type": "navigate"}\n```')
    t = await classify_task("Navigate nested pages", {}, model)
    assert t == "navigate"


@pytest.mark.asyncio
async def test_classifier_garbage_returns_unknown():
    model = Scripted("this is not json at all")
    t = await classify_task("something", {}, model)
    assert t == "unknown"


@pytest.mark.asyncio
async def test_classifier_invalid_label_coerced_to_unknown():
    model = Scripted('{"task_type": "something_weird"}')
    t = await classify_task("something", {}, model)
    assert t == "unknown"


def test_prompt_routing_covers_all_task_types():
    assert system_prompt_for("extract") is EXTRACT_SPECIALIST_SYSTEM
    assert system_prompt_for("form_fill") is FORM_FILL_SPECIALIST_SYSTEM
    assert system_prompt_for("list_iter") is LIST_ITER_SPECIALIST_SYSTEM
    assert system_prompt_for("navigate") is NAVIGATE_SPECIALIST_SYSTEM
    assert system_prompt_for("unknown") is GENERIC_SPECIALIST_SYSTEM
    # Fallback for anything we haven't seen
    assert system_prompt_for("bogus") is GENERIC_SPECIALIST_SYSTEM
