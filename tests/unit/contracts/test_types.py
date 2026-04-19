import pytest
from pydantic import ValidationError

from andera.contracts import Artifact, Event, RunSpec, Sample


def test_runspec_defaults():
    r = RunSpec(
        run_id="r1",
        task_id="t1",
        task_name="x",
        task_prompt="p",
        input_path="in",
        output_dir="out",
    )
    assert r.mode == "auto"
    assert r.concurrency == 4


def test_runspec_rejects_zero_concurrency():
    with pytest.raises(ValidationError):
        RunSpec(
            run_id="r1",
            task_id="t1",
            task_name="x",
            task_prompt="p",
            input_path="in",
            output_dir="out",
            concurrency=0,
        )


def test_sample_defaults():
    s = Sample(sample_id="s1", run_id="r1", row_index=0, input_data={"k": "v"})
    assert s.status == "pending"
    assert s.attempts == 0
    assert s.extracted is None


def test_sample_rejects_negative_row_index():
    with pytest.raises(ValidationError):
        Sample(sample_id="s1", run_id="r1", row_index=-1, input_data={})


def test_artifact_requires_sha256_length():
    with pytest.raises(ValidationError):
        Artifact(sha256="deadbeef", name="x.png", mime="image/png", size=1, path="/x")


def test_artifact_valid():
    a = Artifact(
        sha256="a" * 64, name="x.png", mime="image/png", size=10, path="/tmp/x.png"
    )
    assert a.sample_id is None


def test_event_defaults():
    e = Event(event_id="e1", kind="run.started")
    assert e.prev_hash is None
    assert e.payload == {}


def test_event_rejects_unknown_kind():
    with pytest.raises(ValidationError):
        Event(event_id="e1", kind="bogus")  # type: ignore[arg-type]
