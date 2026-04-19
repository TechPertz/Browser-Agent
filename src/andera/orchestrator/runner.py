"""RunWorkflow — parallel sample execution with durable queue + retry.

Flow per run:

    load_inputs -> materialize Samples -> enqueue sample_ids
    spawn N workers, each:
        dequeue -> acquire browser session from pool -> run LangGraph ->
        write result.json + append row to aggregate CSV -> ack/nack
    finalize: return RunResult + emit RUN_MANIFEST.json

Safety:
  - Queue claim-lease means two workers never take the same sample.
  - Each sample has its own browser context (pool) and LangGraph
    checkpoint thread, so a crashed sample cannot corrupt its neighbor.
  - LiteLLM retries + bounded reflection mean no infinite loops.
  - On worker exception, nack() requeues up to max_attempts.
"""

from __future__ import annotations

import asyncio
import csv
import json
import traceback
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from andera.agent import run_sample
from andera.agent.nodes import AgentDeps
from andera.agent.plan_cache import PlanCache
from andera.browser import BrowserPool
from andera.config import Profile
from andera.models import Role, get_model
from andera.storage import FilesystemArtifactStore
from andera.tools.browser import BrowserTools

from .inputs import load_inputs


@dataclass
class RunResult:
    run_id: str
    run_root: Path
    total: int
    passed: int
    failed: int
    extracted_rows: list[dict[str, Any]] = field(default_factory=list)
    aggregate_csv: Path | None = None
    manifest: Path | None = None


def _pool_for(profile: Profile, store: FilesystemArtifactStore) -> BrowserPool:
    return BrowserPool(
        artifacts=store,
        concurrency=profile.browser.concurrency,
        headless=profile.browser.headless,
        viewport=profile.browser.viewport.model_dump(),
    )


def _build_deps(
    profile: Profile,
    browser_tools: BrowserTools,
    plan_cache: PlanCache,
) -> AgentDeps:
    return AgentDeps(
        planner=get_model(Role.PLANNER, profile),
        navigator=get_model(Role.NAVIGATOR, profile),
        extractor=get_model(Role.EXTRACTOR, profile),
        judge=get_model(Role.JUDGE, profile),
        browser=browser_tools,
        plan_cache=plan_cache,
        # Haiku classifier — reuse the extractor model (cheapest tier).
        classifier=get_model(Role.EXTRACTOR, profile),
    )


class RunWorkflow:
    def __init__(
        self,
        *,
        profile: Profile,
        task: dict[str, Any],
        input_rows: list[dict[str, Any]],
        run_id: str | None = None,
        max_samples: int | None = None,
    ) -> None:
        self.profile = profile
        self.task = task
        self.run_id = run_id or f"run-{uuid.uuid4().hex[:8]}"
        self.rows = input_rows[:max_samples] if max_samples else input_rows

        self.run_root = Path("runs") / self.run_id
        self.run_root.mkdir(parents=True, exist_ok=True)
        self.store = FilesystemArtifactStore(self.run_root)
        self.pool = _pool_for(profile, self.store)
        self.plan_cache = PlanCache()
        # Import late so tests can substitute via monkeypatch.
        from andera.queue import SqliteQueue
        self.queue = SqliteQueue(Path("data") / f"{self.run_id}.queue.db")

        self._results: list[dict[str, Any]] = []
        self._results_lock = asyncio.Lock()

    # --- sample plumbing ---

    async def _enqueue_all(self) -> None:
        for idx, row in enumerate(self.rows):
            sample_id = f"{self.run_id}-{idx:05d}"
            await self.queue.enqueue({
                "sample_id": sample_id,
                "row_index": idx,
                "input_data": row,
                "start_url": row.get("url") or self.task.get("default_url"),
            })

    async def _run_one(self, job: dict[str, Any]) -> dict[str, Any]:
        """Execute one sample through the LangGraph. Returns result dict."""
        sample_id = job["sample_id"]
        async with self.pool.acquire(sample_id=sample_id, run_id=self.run_id) as session:
            deps = _build_deps(self.profile, BrowserTools(session), self.plan_cache)
            initial = {
                "run_id": self.run_id,
                "sample_id": sample_id,
                "task_prompt": self.task.get("prompt", ""),
                "input_data": job.get("input_data") or {},
                "start_url": job.get("start_url"),
                "extract_schema": self.task.get("extract_schema") or {},
                "status": "pending",
            }
            final = await run_sample(
                deps=deps,
                initial_state=initial,
                checkpoint_db=Path("data") / f"{self.run_id}.ckpt.db",
                thread_id=sample_id,
            )
        return {
            "sample_id": sample_id,
            "row_index": job.get("row_index"),
            "verdict": final.get("verdict"),
            "verdict_reason": final.get("verdict_reason"),
            "extracted": final.get("extracted") or {},
            "evidence_count": len(final.get("evidence") or []),
            "status": final.get("status"),
            "error": final.get("error"),
        }

    async def _worker(self, worker_id: int) -> None:
        while True:
            job = await self.queue.dequeue()
            if job is None:
                # Queue drained; short sleep then recheck. When every
                # worker sees None we could exit, but the outer loop
                # handles termination via a global "pending==0" check.
                return
            item_id = job["item_id"]
            try:
                res = await self._run_one(job)
                async with self._results_lock:
                    self._results.append(res)
                if res["verdict"] == "pass" or res["status"] == "done":
                    await self.queue.ack(item_id)
                else:
                    await self.queue.nack(item_id, res.get("error") or "no verdict")
            except Exception as e:
                err = f"{type(e).__name__}: {e}"
                await self.queue.nack(item_id, err)

    async def execute(self) -> RunResult:
        await self._enqueue_all()

        n = max(1, self.profile.browser.concurrency)
        while True:
            await asyncio.gather(*[self._worker(i) for i in range(n)])
            counts = await self.queue.counts()
            if counts.get("pending", 0) == 0:
                break
        # All pending drained; final run rolled up.

        passed = sum(1 for r in self._results if r.get("verdict") == "pass")
        failed = len(self._results) - passed

        csv_path = self.run_root / "output.csv"
        self._write_aggregate_csv(csv_path)
        manifest_path = self.run_root / "RUN_MANIFEST.json"
        self._write_manifest(manifest_path, passed, failed)

        return RunResult(
            run_id=self.run_id,
            run_root=self.run_root,
            total=len(self._results),
            passed=passed,
            failed=failed,
            extracted_rows=[r.get("extracted") or {} for r in self._results],
            aggregate_csv=csv_path,
            manifest=manifest_path,
        )

    def _write_aggregate_csv(self, path: Path) -> None:
        if not self._results:
            path.write_text("")
            return
        # Collect all keys from extracted payloads so columns are stable.
        keys: list[str] = []
        seen: set[str] = set()
        for r in self._results:
            for k in (r.get("extracted") or {}).keys():
                if k not in seen:
                    seen.add(k)
                    keys.append(k)
        with path.open("w", newline="") as f:
            w = csv.writer(f)
            w.writerow(["sample_id", "row_index", "verdict", *keys])
            for r in sorted(self._results, key=lambda x: x.get("row_index", 0)):
                extracted = r.get("extracted") or {}
                w.writerow([
                    r.get("sample_id"),
                    r.get("row_index"),
                    r.get("verdict"),
                    *[extracted.get(k, "") for k in keys],
                ])

    def _write_manifest(self, path: Path, passed: int, failed: int) -> None:
        manifest = {
            "run_id": self.run_id,
            "task": self.task.get("task_id") or self.task.get("task_name"),
            "total": len(self._results),
            "passed": passed,
            "failed": failed,
            "samples": [
                {
                    "sample_id": r["sample_id"],
                    "row_index": r["row_index"],
                    "verdict": r.get("verdict"),
                    "verdict_reason": r.get("verdict_reason"),
                    "status": r.get("status"),
                    "evidence_count": r.get("evidence_count"),
                    "error": r.get("error"),
                }
                for r in self._results
            ],
        }
        path.write_text(json.dumps(manifest, indent=2, default=str))


async def run(
    *,
    profile: Profile,
    task: dict[str, Any],
    input_path: str | Path,
    run_id: str | None = None,
    max_samples: int | None = None,
) -> RunResult:
    """Convenience wrapper: load inputs + run the workflow."""
    rows = load_inputs(input_path)
    wf = RunWorkflow(
        profile=profile,
        task=task,
        input_rows=rows,
        run_id=run_id,
        max_samples=max_samples,
    )
    try:
        return await wf.execute()
    except Exception:
        traceback.print_exc()
        raise
