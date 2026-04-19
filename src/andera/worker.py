"""Standalone worker process — pulls jobs from the shared queue.

With `profile.queue.backend: redis`, the coordinator (one `andera run`
or an API call) enqueues samples; then ANY number of `andera worker`
processes on ANY number of machines dequeue and execute them. No
shared filesystem required for the work loop itself.

Run:
    andera worker <run_id>

Requirements:
    - `runs/<run_id>/.run_config.json` exists (written by the coordinator)
    - `config/profile.yaml` points at the same Redis as the coordinator

The worker reuses RunWorkflow's `_worker` semantics: dequeue -> acquire
browser context -> run LangGraph -> append samples.jsonl -> ack/nack.
It does NOT enqueue, does NOT finalize (no CSV / manifest); those are
the coordinator's job.
"""

from __future__ import annotations

import asyncio
import json
import signal
from pathlib import Path
from typing import Any

from andera.agent import run_sample
from andera.agent.nodes import AgentDeps
from andera.agent.plan_cache import PlanCache
from andera.browser import BrowserPool
from andera.config import Profile, load_profile
from andera.models import Role, get_model
from andera.observability import get_trace_sink, install_langfuse_if_enabled
from andera.queue import make_queue
from andera.storage import AuditLog, FilesystemArtifactStore
from andera.tools.browser import BrowserTools


def _load_run_config(run_id: str) -> dict[str, Any]:
    path = Path("runs") / run_id / ".run_config.json"
    if not path.exists():
        raise FileNotFoundError(
            f"{path} not found — coordinator must start the run first"
        )
    return json.loads(path.read_text())


def _pool_for(profile: Profile, store: FilesystemArtifactStore) -> BrowserPool:
    from andera.browser.rate_limiter import HostRateLimiter
    limiter = HostRateLimiter(
        rps=profile.browser.per_host_rps,
        burst=profile.browser.per_host_burst,
    )
    return BrowserPool(
        artifacts=store,
        concurrency=profile.browser.concurrency,
        headless=profile.browser.headless,
        viewport=profile.browser.viewport.model_dump(),
        stealth=profile.browser.stealth,
        rate_limiter=limiter,
    )


class WorkerNode:
    """One standalone consumer. Run N of these across N boxes to scale."""

    def __init__(
        self,
        *,
        profile: Profile,
        run_id: str,
        task: dict[str, Any],
        worker_id: str,
    ) -> None:
        self.profile = profile
        self.run_id = run_id
        self.task = task
        self.worker_id = worker_id

        self.run_root = Path("runs") / run_id
        self.run_root.mkdir(parents=True, exist_ok=True)
        self.store = FilesystemArtifactStore(self.run_root)
        self.pool = _pool_for(profile, self.store)
        self.plan_cache = PlanCache()
        self.queue = make_queue(
            backend=profile.queue.backend,
            run_id=run_id,
            sqlite_path=Path("data") / f"{run_id}.queue.db",
            redis_url=profile.queue.redis_url,
            redis_prefix=profile.queue.redis_prefix,
            max_attempts=profile.queue.max_attempts,
        )
        self.audit = AuditLog(Path("data") / f"{run_id}.audit.db")
        self.samples_jsonl = self.run_root / "samples.jsonl"

        self._stop = asyncio.Event()
        self._write_lock = asyncio.Lock()

    def _install_signals(self) -> None:
        loop = asyncio.get_event_loop()
        def _handler(*_a):
            self._stop.set()
        for sig in (signal.SIGTERM, signal.SIGINT):
            try:
                loop.add_signal_handler(sig, _handler)
            except (NotImplementedError, RuntimeError, ValueError):
                pass

    def _build_deps(self, session) -> AgentDeps:
        return AgentDeps(
            planner=get_model(Role.PLANNER, self.profile),
            navigator=get_model(Role.NAVIGATOR, self.profile),
            extractor=get_model(Role.EXTRACTOR, self.profile),
            judge=get_model(Role.JUDGE, self.profile),
            browser=BrowserTools(session),
            plan_cache=self.plan_cache,
            classifier=get_model(Role.EXTRACTOR, self.profile),
        )

    async def _process_one(self, job: dict[str, Any]) -> dict[str, Any]:
        sample_id = job["sample_id"]
        self.audit.append(
            kind="sample.started", run_id=self.run_id, sample_id=sample_id,
            payload={"row_index": job.get("row_index"), "worker": self.worker_id},
        )
        async with self.pool.acquire(sample_id=sample_id, run_id=self.run_id) as session:
            deps = self._build_deps(session)
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
        result = {
            "sample_id": sample_id,
            "row_index": job.get("row_index"),
            "verdict": final.get("verdict"),
            "verdict_reason": final.get("verdict_reason"),
            "extracted": final.get("extracted") or {},
            "evidence": final.get("evidence") or [],
            "evidence_count": len(final.get("evidence") or []),
            "status": final.get("status"),
            "error": final.get("error"),
            "worker_id": self.worker_id,
        }
        self.audit.append(
            kind="sample.completed" if result["verdict"] == "pass" else "sample.failed",
            run_id=self.run_id, sample_id=sample_id,
            payload={"verdict": result["verdict"], "worker": self.worker_id},
        )
        async with self._write_lock:
            with self.samples_jsonl.open("a") as f:
                f.write(json.dumps(result, default=str, ensure_ascii=False) + "\n")
        return result

    async def run(self) -> int:
        """Loop until SIGTERM / queue drained. Returns count of processed jobs."""
        self._install_signals()
        install_langfuse_if_enabled(self.profile)
        trace = get_trace_sink()
        trace.write({"kind": "worker.start", "run_id": self.run_id,
                     "worker_id": self.worker_id})
        if hasattr(self.pool, "setup"):
            await self.pool.setup()

        processed = 0
        empty_rounds = 0
        try:
            while not self._stop.is_set():
                job = await self.queue.dequeue()
                if job is None:
                    empty_rounds += 1
                    if empty_rounds >= 3:
                        break
                    await asyncio.sleep(0.5 * empty_rounds)
                    continue
                empty_rounds = 0
                item_id = job["item_id"]
                try:
                    res = await self._process_one(job)
                    if res.get("verdict") == "pass" or res.get("status") == "done":
                        await self.queue.ack(item_id)
                    else:
                        await self.queue.nack(item_id, res.get("error") or "no verdict")
                except Exception as e:
                    await self.queue.nack(item_id, f"{type(e).__name__}: {e}")
                processed += 1
        finally:
            if hasattr(self.pool, "teardown"):
                try:
                    await self.pool.teardown()
                except Exception:
                    pass
            trace.write({"kind": "worker.stop", "run_id": self.run_id,
                         "worker_id": self.worker_id, "processed": processed})
        return processed


async def run_worker(run_id: str, *, profile_path: Path | None = None,
                     worker_id: str | None = None) -> int:
    import uuid
    profile = load_profile(profile_path)
    cfg = _load_run_config(run_id)
    node = WorkerNode(
        profile=profile,
        run_id=run_id,
        task=cfg["task"],
        worker_id=worker_id or f"w-{uuid.uuid4().hex[:6]}",
    )
    return await node.run()
