import json
import pytest

from andera.orchestrator import load_inputs


def test_csv(tmp_path):
    p = tmp_path / "in.csv"
    p.write_text("url,name\nhttps://a,Alice\nhttps://b,Bob\n")
    rows = load_inputs(p)
    assert rows == [
        {"url": "https://a", "name": "Alice"},
        {"url": "https://b", "name": "Bob"},
    ]


def test_jsonl(tmp_path):
    p = tmp_path / "in.jsonl"
    p.write_text('{"id": 1}\n{"id": 2}\n\n')
    rows = load_inputs(p)
    assert rows == [{"id": 1}, {"id": 2}]


def test_json_list(tmp_path):
    p = tmp_path / "in.json"
    p.write_text(json.dumps([{"a": 1}, {"a": 2}]))
    assert load_inputs(p) == [{"a": 1}, {"a": 2}]


def test_missing_file_raises(tmp_path):
    with pytest.raises(FileNotFoundError):
        load_inputs(tmp_path / "nope.csv")


def test_unknown_format_raises(tmp_path):
    p = tmp_path / "in.txt"
    p.write_text("hello")
    with pytest.raises(ValueError):
        load_inputs(p)


def test_json_non_list_raises(tmp_path):
    p = tmp_path / "in.json"
    p.write_text(json.dumps({"not": "a list"}))
    with pytest.raises(ValueError):
        load_inputs(p)
