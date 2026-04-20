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
        vision = None
        if getattr(self.profile.models, "vision", None) is not None:
            vision = get_model(Role.VISION, self.profile)
        return AgentDeps(
            planner=get_model(Role.PLANNER, self.profile),
            navigator=get_model(Role.NAVIGATOR, self.profile),
            extractor=get_model(Role.EXTRACTOR, self.profile),
            judge=get_model(Role.JUDGE, self.profile),
            browser=BrowserTools(session),
            plan_cache=self.plan_cache,
            classifier=get_model(Role.EXTRACTOR, self.profile),
            vision=vision,
        )

    async def _process_one(self, job: dict[str, Any]) -> dict[str, Any]:
        # Thin wrapper over the shared core — used by the legacy
        # per-run WorkerNode path. run_agent_pool uses the same core.
        return await _execute_sample(
            job=job,
            task=self.task,
            run_id=self.run_id,
            profile=self.profile,
            pool=self.pool,
            plan_cache=self.plan_cache,
            audit=self.audit,
            samples_jsonl_path=self.samples_jsonl,
            write_lock=self._write_lock,
            worker_id=self.worker_id,
        )

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


# =============================================================================
# Stateless core + run-agnostic agent pool
# =============================================================================


async def _execute_sample(
    *,
    job: dict[str, Any],
    task: dict[str, Any],
    run_id: str,
    profile: Profile,
    pool: BrowserPool,
    plan_cache: PlanCache,
    audit: AuditLog,
    samples_jsonl_path: Path,
    write_lock: asyncio.Lock,
    worker_id: str,
) -> dict[str, Any]:
    """The shared sample-execution core. Used by both WorkerNode
    (per-run mode) and run_agent_pool (run-agnostic). Everything it
    needs is passed in; no self-state access."""
    sample_id = job["sample_id"]
    start_url = job.get("start_url")
    from andera.credentials import SealedStateStore, host_of, looks_logged_out
    host = host_of(start_url)
    storage_state: dict[str, Any] | None = None
    creds = SealedStateStore()
    # Merge every sealed host's cookies into the context. Multi-host
    # tasks need auth for hosts the agent discovers mid-sample (e.g.
    # starts on github.com, hops to linkedin.com). Cookies stay
    # domain-scoped in the browser, so merging is safe.
    try:
        storage_state = creds.load_merged()
    except Exception:
        storage_state = None
    audit.append(
        kind="sample.started", run_id=run_id, sample_id=sample_id,
        payload={
            "row_index": job.get("row_index"), "worker": worker_id,
            "host": host, "preauthed": storage_state is not None,
        },
    )
    async with pool.acquire(
        sample_id=sample_id, run_id=run_id, storage_state=storage_state,
    ) as session:
        if start_url:
            try:
                await session.goto(start_url)
                snap = await session.snapshot()
                landed = snap.get("url") or start_url
                if looks_logged_out(landed) and host is not None:
                    msg = (
                        f"auth required for {host}: "
                        f"run `andera login {host} --url <login-url>` and retry"
                    )
                    if storage_state is not None:
                        msg += " (saved session appears expired)"
                    result = {
                        "sample_id": sample_id,
                        "row_index": job.get("row_index"),
                        "verdict": "fail",
                        "verdict_reason": msg,
                        "extracted": {},
                        "evidence": [],
                        "evidence_count": 0,
                        "status": "failed",
                        "error": msg,
                        "worker_id": worker_id,
                    }
                    audit.append(
                        kind="sample.failed", run_id=run_id, sample_id=sample_id,
                        payload={"reason": "auth_required", "host": host,
                                 "worker": worker_id},
                    )
                    async with write_lock:
                        samples_jsonl_path.parent.mkdir(parents=True, exist_ok=True)
                        with samples_jsonl_path.open("a") as f:
                            f.write(json.dumps(result, default=str, ensure_ascii=False) + "\n")
                    return result
            except Exception:
                pass
        vision = None
        if getattr(profile.models, "vision", None) is not None:
            vision = get_model(Role.VISION, profile)
        deps = AgentDeps(
            planner=get_model(Role.PLANNER, profile),
            navigator=get_model(Role.NAVIGATOR, profile),
            extractor=get_model(Role.EXTRACTOR, profile),
            judge=get_model(Role.JUDGE, profile),
            browser=BrowserTools(session),
            plan_cache=plan_cache,
            classifier=get_model(Role.EXTRACTOR, profile),
            vision=vision,
        )
        initial = {
            "run_id": run_id,
            "sample_id": sample_id,
            "task_prompt": task.get("prompt", ""),
            "input_data": job.get("input_data") or {},
            "start_url": job.get("start_url"),
            "extract_schema": task.get("extract_schema") or {},
            "status": "pending",
        }
        pg_url = (profile.storage.metadata.postgres_url
                  if profile.storage.metadata.backend == "postgres" else None)
        final = await run_sample(
            deps=deps,
            initial_state=initial,
            checkpoint_db=Path("data") / f"{run_id}.ckpt.db",
            thread_id=sample_id,
            postgres_url=pg_url,
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
        "worker_id": worker_id,
    }
    audit.append(
        kind="sample.completed" if result["verdict"] == "pass" else "sample.failed",
        run_id=run_id, sample_id=sample_id,
        payload={"verdict": result["verdict"], "worker": worker_id},
    )
    async with write_lock:
        samples_jsonl_path.parent.mkdir(parents=True, exist_ok=True)
        with samples_jsonl_path.open("a") as f:
            f.write(json.dumps(result, default=str, ensure_ascii=False) + "\n")
    return result


async def run_agent_pool(
    *,
    profile: Profile,
    agent_id: str,
    redis_url: str | None = None,
) -> int:
    """Long-lived, run-agnostic agent. Pulls from the GLOBAL Redis queue,
    executes whichever sample arrives, writes evidence to
    `runs/<job.run_id>/` (shared across agents via volume mount).

    No `.run_config.json` lookup. Each job carries its task + run_id
    inline (see orchestrator/runner.py::_enqueue_all).

    Returns the number of samples processed before shutdown."""
    if profile.queue.backend != "redis":
        raise RuntimeError(
            "run_agent_pool requires profile.queue.backend=redis; "
            f"got {profile.queue.backend!r}"
        )

    queue = make_queue(
        backend="redis",
        redis_url=redis_url or profile.queue.redis_url,
        max_attempts=profile.queue.max_attempts,
        global_queue=True,
    )

    # Per-process shared state: one Chromium for the agent's lifetime,
    # one PlanCache (in-memory + shared disk), one trace sink. The
    # artifact store root is `runs/`; per-run subdirectories are
    # created on demand by FilesystemArtifactStore.put().
    store = FilesystemArtifactStore("runs")
    from andera.browser.rate_limiter import HostRateLimiter
    pool = BrowserPool(
        artifacts=store,
        concurrency=profile.browser.concurrency,
        headless=profile.browser.headless,
        viewport=profile.browser.viewport.model_dump(),
        stealth=profile.browser.stealth,
        rate_limiter=HostRateLimiter(
            rps=profile.browser.per_host_rps,
            burst=profile.browser.per_host_burst,
        ),
    )
    plan_cache = PlanCache()
    install_langfuse_if_enabled(profile)
    trace = get_trace_sink()
    trace.write({"kind": "agent.start", "agent_id": agent_id})

    # Per-run state (audit log, samples.jsonl path) — cached lazily so
    # we don't re-open AuditLog on every sample from the same run.
    audits: dict[str, AuditLog] = {}
    jsonl_paths: dict[str, Path] = {}
    write_lock = asyncio.Lock()

    stop_event = asyncio.Event()
    loop = asyncio.get_event_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            loop.add_signal_handler(sig, stop_event.set)
        except (NotImplementedError, RuntimeError, ValueError):
            pass

    await pool.setup()
    processed = 0
    empty_rounds = 0
    try:
        while not stop_event.is_set():
            job = await queue.dequeue()
            if job is None:
                empty_rounds += 1
                # Idle agent containers stay alive — re-poll forever with
                # a small backoff rather than exiting on drain.
                await asyncio.sleep(min(0.5 * empty_rounds, 5.0))
                continue
            empty_rounds = 0
            run_id = job.get("run_id")
            task = job.get("task") or {}
            if not run_id:
                # Malformed job; dead-letter to avoid a poison pill loop.
                await queue.dead_letter(job["item_id"])
                continue
            audit = audits.setdefault(
                run_id, AuditLog(Path("data") / f"{run_id}.audit.db")
            )
            samples_path = jsonl_paths.setdefault(
                run_id, Path("runs") / run_id / "samples.jsonl"
            )
            item_id = job["item_id"]
            try:
                res = await _execute_sample(
                    job=job, task=task, run_id=run_id,
                    profile=profile, pool=pool, plan_cache=plan_cache,
                    audit=audit, samples_jsonl_path=samples_path,
                    write_lock=write_lock, worker_id=agent_id,
                )
                if res.get("verdict") == "pass" or res.get("status") == "done":
                    await queue.ack(item_id)
                else:
                    await queue.nack(item_id, res.get("error") or "no verdict")
            except Exception as e:
                await queue.nack(item_id, f"{type(e).__name__}: {e}")
            processed += 1
    finally:
        try:
            await pool.teardown()
        except Exception:
            pass
        try:
            if hasattr(queue, "close"):
                await queue.close()
        except Exception:
            pass
        trace.write({"kind": "agent.stop", "agent_id": agent_id,
                     "processed": processed})
    return processed
