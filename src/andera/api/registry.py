"""In-process registry of runs visible to the API.

Phase 5a keeps this in memory — runs launched via the API are
trackable until the process restarts. Completed runs persist to disk
(RUN_MANIFEST.json) and can be re-inspected even after the process
cycles.
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


@dataclass
class RunRecord:
    run_id: str
    task_id: str | None
    status: str = "queued"        # queued | running | completed | failed
    total: int = 0
    passed: int = 0
    failed: int = 0
    run_root: str | None = None
    task: dict[str, Any] | None = None
    error: str | None = None
    task_fut: asyncio.Task | None = None
    samples: list[dict[str, Any]] = field(default_factory=list)

    def public_dict(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "task_id": self.task_id,
            "status": self.status,
            "total": self.total,
            "passed": self.passed,
            "failed": self.failed,
            "run_root": self.run_root,
            "error": self.error,
        }


class RunRegistry:
    def __init__(self) -> None:
        self._runs: dict[str, RunRecord] = {}

    def register(self, rec: RunRecord) -> None:
        self._runs[rec.run_id] = rec

    def get(self, run_id: str) -> RunRecord | None:
        rec = self._runs.get(run_id)
        if rec is not None:
            return rec
        # Not in memory — try to hydrate from on-disk manifest
        manifest_path = Path("runs") / run_id / "RUN_MANIFEST.json"
        if manifest_path.exists():
            return self._hydrate(manifest_path)
        return None

    def list(self) -> list[dict[str, Any]]:
        live = [rec.public_dict() for rec in self._runs.values()]
        # Also surface completed-on-disk runs not tracked in memory
        seen = {r["run_id"] for r in live}
        root = Path("runs")
        if root.exists():
            for p in sorted(root.iterdir()):
                if not p.is_dir() or p.name in seen:
                    continue
                manifest = p / "RUN_MANIFEST.json"
                if manifest.exists():
                    try:
                        rec = self._hydrate(manifest)
                        live.append(rec.public_dict())
                    except Exception:
                        continue
        return live

    @staticmethod
    def _hydrate(manifest_path: Path) -> RunRecord:
        data = json.loads(manifest_path.read_text())
        totals = data.get("totals") or {}
        rec = RunRecord(
            run_id=data.get("run_id", manifest_path.parent.name),
            task_id=(data.get("task") or {}).get("task_id"),
            status="completed",
            total=totals.get("samples", 0),
            passed=totals.get("passed", 0),
            failed=totals.get("failed", 0),
            run_root=str(manifest_path.parent),
            task=data.get("task"),
            samples=data.get("samples", []),
        )
        return rec


_registry = RunRegistry()


def get_registry() -> RunRegistry:
    return _registry
