"""RUN_MANIFEST.json — per-run integrity root.

Auditors read this single file to verify a run:
  - SHA of every artifact in the evidence tree
  - Per-sample summary
  - Audit-log root hash tying it to the append-only log
  - Config snapshot (task, profile excerpt)
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


def _utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _sha_file(p: Path) -> str:
    h = hashlib.sha256()
    with p.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def write_manifest(
    *,
    run_root: Path,
    run_id: str,
    task: dict[str, Any],
    samples: list[dict[str, Any]],
    audit_root_hash: str | None,
    profile_excerpt: dict[str, Any] | None = None,
) -> Path:
    """Scan evidence tree + write manifest with tamper-evident hashes."""
    blobs_root = run_root / "blobs"
    artifacts: list[dict[str, Any]] = []
    if blobs_root.exists():
        for p in sorted(blobs_root.rglob("*")):
            if not p.is_file():
                continue
            artifacts.append({
                "sha256": _sha_file(p),
                "path": str(p.relative_to(run_root)),
                "size": p.stat().st_size,
            })

    manifest = {
        "version": 1,
        "run_id": run_id,
        "generated_at": _utcnow_iso(),
        "task": {
            "task_id": task.get("task_id"),
            "task_name": task.get("task_name"),
            "prompt": task.get("prompt"),
            "extract_schema": task.get("extract_schema"),
        },
        "profile_excerpt": profile_excerpt or {},
        "totals": {
            "samples": len(samples),
            "passed": sum(1 for s in samples if s.get("verdict") == "pass"),
            "failed": sum(1 for s in samples if s.get("verdict") != "pass"),
            "artifacts": len(artifacts),
        },
        "samples": samples,
        "artifacts": artifacts,
        "audit_root_hash": audit_root_hash,
    }
    # root_hash of the manifest = sha256 of canonical JSON (excluding itself)
    canonical = json.dumps(manifest, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    manifest["manifest_hash"] = hashlib.sha256(canonical.encode("utf-8")).hexdigest()

    out = run_root / "RUN_MANIFEST.json"
    out.write_text(json.dumps(manifest, indent=2, ensure_ascii=False))
    return out


def verify_manifest(run_root: Path) -> dict[str, Any]:
    """Re-hash artifacts on disk; confirm manifest_hash still matches.

    Returns a report dict with `ok` bool + any mismatches. Does NOT
    verify the audit_log chain — call AuditLog.verify_chain for that.
    """
    p = run_root / "RUN_MANIFEST.json"
    if not p.exists():
        return {"ok": False, "error": "manifest not found"}
    m = json.loads(p.read_text())
    # Recompute manifest_hash on the manifest minus itself.
    recorded = m.pop("manifest_hash", None)
    canonical = json.dumps(m, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    recomputed = hashlib.sha256(canonical.encode("utf-8")).hexdigest()

    # Now walk artifacts and re-hash each file
    bad_artifacts: list[dict[str, Any]] = []
    for a in m.get("artifacts", []):
        fpath = run_root / a["path"]
        if not fpath.exists():
            bad_artifacts.append({**a, "reason": "missing"})
            continue
        if _sha_file(fpath) != a["sha256"]:
            bad_artifacts.append({**a, "reason": "hash mismatch"})
    return {
        "ok": recorded == recomputed and not bad_artifacts,
        "manifest_hash_ok": recorded == recomputed,
        "artifacts_checked": len(m.get("artifacts", [])),
        "bad_artifacts": bad_artifacts,
        "audit_root_hash": m.get("audit_root_hash"),
    }
