"""Synthesize extract_schema from form inputs — the UX for NLP tasks.

The user doesn't write JSON schema; they type 'author, date, school' in
a textbox and optionally tick 'multi_item' for fan-out. `_schema_from_fields`
is the conversion; this test pins down its behavior.
"""

from andera.api.routes.runs import _schema_from_fields


def test_empty_returns_action_mode():
    assert _schema_from_fields(None, False) == {}
    assert _schema_from_fields("", False) == {}
    assert _schema_from_fields("   ", False) == {}
    assert _schema_from_fields(",,", False) == {}


def test_single_object_schema():
    s = _schema_from_fields("author, date", multi_item=False)
    assert s["type"] == "object"
    assert s["required"] == ["author", "date"]
    # Allow null so the extractor can report "not visible" instead of inventing.
    assert s["properties"]["author"]["type"] == ["string", "null"]


def test_deduplicates_and_preserves_order():
    s = _schema_from_fields("author, date, author, school", multi_item=False)
    assert s["required"] == ["author", "date", "school"]


def test_multi_item_wraps_in_array_schema():
    s = _schema_from_fields("pr_title, author", multi_item=True)
    assert s["type"] == "array"
    assert s["items"]["type"] == "object"
    assert s["items"]["required"] == ["pr_title", "author"]
    # Makes the schema eligible for _is_array_schema() so the extract
    # node dispatches to fan-out mode.
    from andera.agent.nodes import _is_array_schema
    assert _is_array_schema(s) is True


def test_whitespace_only_fields_ignored():
    s = _schema_from_fields("author,  , , date", multi_item=False)
    assert s["required"] == ["author", "date"]
