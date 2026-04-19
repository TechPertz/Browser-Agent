import json

import pytest

from andera.storage import FilesystemArtifactStore, verify_manifest, write_manifest


@pytest.fixture
async def populated_run(tmp_path):
    run_root = tmp_path / "run-xyz"
    store = FilesystemArtifactStore(run_root)
    await store.put(b"screenshot-1-bytes", "step_00.png", mime="image/png")
    await store.put(b"screenshot-2-bytes", "step_01.png", mime="image/png")
    samples = [
        {"sample_id": "s1", "row_index": 0, "verdict": "pass", "evidence_count": 1},
        {"sample_id": "s2", "row_index": 1, "verdict": "fail", "evidence_count": 1},
    ]
    task = {"task_id": "t1", "task_name": "Test", "prompt": "do thing"}
    write_manifest(
        run_root=run_root, run_id="run-xyz", task=task, samples=samples,
        audit_root_hash="deadbeef" * 8,
    )
    return run_root


async def test_manifest_written_with_artifacts_and_totals(populated_run):
    m = json.loads((populated_run / "RUN_MANIFEST.json").read_text())
    assert m["run_id"] == "run-xyz"
    assert m["totals"]["samples"] == 2
    assert m["totals"]["passed"] == 1
    assert m["totals"]["failed"] == 1
    assert m["totals"]["artifacts"] == 2
    assert m["audit_root_hash"] == "deadbeef" * 8
    assert len(m["manifest_hash"]) == 64


async def test_verify_clean_manifest(populated_run):
    report = verify_manifest(populated_run)
    assert report["ok"] is True
    assert report["manifest_hash_ok"] is True
    assert report["artifacts_checked"] == 2
    assert report["bad_artifacts"] == []


async def test_verify_detects_file_tamper(populated_run):
    # Tamper with one blob
    blobs = list((populated_run / "blobs").rglob("*"))
    blobs = [p for p in blobs if p.is_file()]
    assert blobs
    blobs[0].write_bytes(b"tampered-content")
    report = verify_manifest(populated_run)
    assert report["ok"] is False
    assert len(report["bad_artifacts"]) == 1
    assert report["bad_artifacts"][0]["reason"] == "hash mismatch"


async def test_verify_detects_missing_file(populated_run):
    blobs = [p for p in (populated_run / "blobs").rglob("*") if p.is_file()]
    blobs[0].unlink()
    report = verify_manifest(populated_run)
    assert report["ok"] is False
    assert any(b["reason"] == "missing" for b in report["bad_artifacts"])
