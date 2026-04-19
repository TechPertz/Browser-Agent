"""RunWorkflow — parallel sample execution with durable queue + retry.

Flow per run:

    load_inputs -> materialize Samples -> enqueue sample_ids
    spawn N workers, each:
        dequeue -> acquire browser session from pool -> run LangGraph ->
        write sample row to samples.jsonl -> ack/nack
    finalize: rebuild output.csv from samples.jsonl + emit RUN_MANIFEST.json

Durability:
  - Queue claim-lease means two workers never take the same sample.
  - samples.jsonl is the append-only source of truth for extracted data.
  - In-memory footprint is per-sample-counters, not the full result list,
    so 10k samples don't balloon RAM.
  - A Ctrl-C / SIGTERM drains in-flight samples (within a grace window)
    then exits. `andera resume <run_id>` picks up where we stopped.
"""

from __future__ import annotations

import asyncio
import csv
import json
import signal
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
from andera.observability import get_trace_sink, install_langfuse_if_enabled
from andera.storage import AuditLog, FilesystemArtifactStore, write_manifest
from andera.tools.browser import BrowserTools

from .inputs import load_inputs


SHUTDOWN_GRACE_S = 60.0


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
        resuming: bool = False,
    ) -> None:
        self.profile = profile
        self.task = task
        self.run_id = run_id or f"run-{uuid.uuid4().hex[:8]}"
        self.rows = input_rows[:max_samples] if max_samples else input_rows
        self.resuming = resuming

        self.run_root = Path("runs") / self.run_id
        self.run_root.mkdir(parents=True, exist_ok=True)
        self.samples_jsonl = self.run_root / "samples.jsonl"
        self.run_config_path = self.run_root / ".run_config.json"
        self.store = FilesystemArtifactStore(self.run_root)
        self.pool = _pool_for(profile, self.store)
        self.plan_cache = PlanCache()
        # Queue backend is profile-driven. SQLite on a laptop, Redis across
        # worker pods. Same TaskQueue Protocol either way.
        from andera.queue import make_queue
        self.queue = make_queue(
            backend=profile.queue.backend,
            run_id=self.run_id,
            sqlite_path=Path("data") / f"{self.run_id}.queue.db",
            redis_url=profile.queue.redis_url,
            redis_prefix=profile.queue.redis_prefix,
            max_attempts=profile.queue.max_attempts,
        )
        self.audit = AuditLog(Path("data") / f"{self.run_id}.audit.db")

        # Memory-bounded counters instead of full result list.
        self._counters = {"total": 0, "passed": 0, "failed": 0}
        self._completed_ids: set[str] = set()
        self._results_lock = asyncio.Lock()
        self._stop_event: asyncio.Event | None = None

    # --- durability helpers ---

    def _save_run_config(self) -> None:
        """Persist enough to resume the run after a process restart."""
        self.run_config_path.write_text(json.dumps({
            "run_id": self.run_id,
            "task": self.task,
            "max_samples_applied": len(self.rows),
        }, indent=2))

    def _append_sample_jsonl(self, row: dict[str, Any]) -> None:
        with self.samples_jsonl.open("a") as f:
            f.write(json.dumps(row, ensure_ascii=False, default=str) + "\n")

    def _load_completed_from_disk(self) -> None:
        """On resume: rebuild completed-set + counters from samples.jsonl."""
        if not self.samples_jsonl.exists():
            return
        with self.samples_jsonl.open() as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    row = json.loads(line)
                except Exception:
                    continue
                sid = row.get("sample_id")
                if not sid or sid in self._completed_ids:
                    continue
                self._completed_ids.add(sid)
                self._counters["total"] += 1
                if row.get("verdict") == "pass":
                    self._counters["passed"] += 1
                else:
                    self._counters["failed"] += 1

    # --- sample plumbing ---

    async def _enqueue_all(self) -> None:
        """Enqueue samples. Inlines task + run_id so a long-lived agent
        pool that doesn't know which run it's serving can execute any
        sample straight off the queue — no .run_config.json lookup.
        In-process workers ignore these extra fields (harmless)."""
        for idx, row in enumerate(self.rows):
            sample_id = f"{self.run_id}-{idx:05d}"
            if sample_id in self._completed_ids:
                continue
            await self.queue.enqueue({
                "sample_id": sample_id,
                "row_index": idx,
                "input_data": row,
                "start_url": row.get("url") or self.task.get("default_url"),
                "run_id": self.run_id,
                "task": self.task,
            })

    async def _run_one(self, job: dict[str, Any]) -> dict[str, Any]:
        sample_id = job["sample_id"]
        self.audit.append(
            kind="sample.started", run_id=self.run_id, sample_id=sample_id,
            payload={"row_index": job.get("row_index")},
        )
        cast = None
        if self.profile.browser.screencast:
            from andera.api.ws import get_bus
            from andera.browser import Screencaster
        async with self.pool.acquire(sample_id=sample_id, run_id=self.run_id) as session:
            if self.profile.browser.screencast:
                page = getattr(session, "_page", None)
                if page is not None:
                    cast = Screencaster(
                        page, sample_id=sample_id, publish=get_bus().publish,
                        fps=self.profile.browser.screencast_fps,
                    )
                    try:
                        await cast.start()
                    except Exception:
                        cast = None
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
            try:
                final = await run_sample(
                    deps=deps,
                    initial_state=initial,
                    checkpoint_db=Path("data") / f"{self.run_id}.ckpt.db",
                    thread_id=sample_id,
                )
            finally:
                if cast is not None:
                    try:
                        await cast.stop()
                    except Exception:
                        pass
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
        }
        self.audit.append(
            kind="sample.completed" if result["verdict"] == "pass" else "sample.failed",
            run_id=self.run_id, sample_id=sample_id,
            payload={"verdict": result["verdict"], "row_index": result["row_index"]},
        )
        return result

    async def _record_result(self, result: dict[str, Any]) -> None:
        async with self._results_lock:
            sid = result["sample_id"]
            if sid in self._completed_ids:
                return  # idempotent on resume
            self._completed_ids.add(sid)
            self._counters["total"] += 1
            if result.get("verdict") == "pass":
                self._counters["passed"] += 1
            else:
                self._counters["failed"] += 1
            self._append_sample_jsonl(result)

    async def _worker(self, worker_id: int) -> None:
        while True:
            if self._stop_event is not None and self._stop_event.is_set():
                return
            job = await self.queue.dequeue()
            if job is None:
                return
            item_id = job["item_id"]
            try:
                res = await self._run_one(job)
                await self._record_result(res)
                if res.get("verdict") == "pass" or res.get("status") == "done":
                    await self.queue.ack(item_id)
                else:
                    await self.queue.nack(item_id, res.get("error") or "no verdict")
            except Exception as e:
                err = f"{type(e).__name__}: {e}"
                await self.queue.nack(item_id, err)

    def _install_signal_handlers(self) -> None:
        """Cooperative shutdown on SIGTERM/SIGINT. Best-effort: non-POSIX
        platforms (or tests running inside other event loops) may skip."""
        if self._stop_event is None:
            self._stop_event = asyncio.Event()
        loop = asyncio.get_event_loop()
        def _handler(*_a):
            self._stop_event.set()  # type: ignore[union-attr]
        for sig in (signal.SIGTERM, signal.SIGINT):
            try:
                loop.add_signal_handler(sig, _handler)
            except (NotImplementedError, RuntimeError, ValueError):
                pass

    async def execute(self) -> RunResult:
        """Drive the run. In distributed mode, returns immediately after
        enqueue without spawning in-process workers — external agent
        containers pull from the shared queue and the caller polls
        `queue_drained()` / calls `finalize()` when counts hit zero.
        In default (embedded) mode, workers run locally and this method
        blocks until the queue drains, then finalizes."""
        self._save_run_config()
        self._install_signal_handlers()
        # Best-effort: Langfuse if configured; always: local JSONL trace sink.
        install_langfuse_if_enabled(self.profile)
        self._trace = get_trace_sink()
        self._trace.write({"kind": "run.init", "run_id": self.run_id,
                           "task": self.task.get("task_id"),
                           "total": len(self.rows)})

        distributed = self.profile.queue.distributed
        # Chromium only belongs in-process when we'll actually run samples here.
        if not distributed and hasattr(self.pool, "setup"):
            try:
                await self.pool.setup()
            except Exception:
                pass

        if self.resuming:
            # Pull any prior completions into our counters + skip set.
            self._load_completed_from_disk()
            # Rescue claims left by a crashed process.
            reclaimed = await self.queue.reclaim_stale(older_than_seconds=0)
            self.audit.append(
                kind="run.started", run_id=self.run_id,
                payload={
                    "resumed": True, "already_completed": len(self._completed_ids),
                    "reclaimed_stale": reclaimed, "distributed": distributed,
                },
            )
        else:
            self.audit.append(
                kind="run.started", run_id=self.run_id,
                payload={
                    "task": self.task.get("task_id"),
                    "total": len(self.rows),
                    "distributed": distributed,
                },
            )

        await self._enqueue_all()

        if distributed:
            # External agents process. Hand back a partial RunResult —
            # the API-side finalizer loop calls finalize() when drained.
            return RunResult(
                run_id=self.run_id,
                run_root=self.run_root,
                total=len(self.rows),
                passed=0, failed=0,
                extracted_rows=[],
                aggregate_csv=None, manifest=None,
            )

        # Embedded mode: spawn N workers in-process and block to drain.
        n = max(1, self.profile.browser.concurrency)
        while True:
            await asyncio.gather(*[self._worker(i) for i in range(n)])
            if self._stop_event is not None and self._stop_event.is_set():
                break
            counts = await self.queue.counts()
            if counts.get("pending", 0) == 0:
                break

        return await self.finalize()

    async def queue_drained(self) -> bool:
        """True when the queue has no pending AND no claimed items.
        External finalizer polls this to decide when to wrap the run."""
        counts = await self.queue.counts()
        return (counts.get("pending", 0) == 0
                and counts.get("claimed", 0) == 0)

    async def finalize(self) -> RunResult:
        """Compute totals from samples.jsonl, write output.csv + manifest,
        emit run.completed event, and tear down the browser pool.
        Safe to call once; idempotency guarded by external caller."""
        # Re-read counters from JSONL so the finalizer (possibly a different
        # process than the one that enqueued) has the correct totals when
        # all samples came from agent containers.
        self._counters = {"total": 0, "passed": 0, "failed": 0}
        self._completed_ids = set()
        self._load_completed_from_disk()
        passed = self._counters["passed"]
        failed = self._counters["failed"]
        total = self._counters["total"]

        csv_path = self.run_root / "output.csv"
        self._rebuild_csv_from_jsonl(csv_path)

        self.audit.append(
            kind="run.completed", run_id=self.run_id,
            payload={"passed": passed, "failed": failed, "total": total},
        )
        if hasattr(self, "_trace"):
            self._trace.write({"kind": "run.completed", "run_id": self.run_id,
                               "passed": passed, "failed": failed, "total": total})
        audit_root = self.audit.root_hash(run_id=self.run_id)
        samples_summary = self._samples_summary_from_jsonl()
        manifest_path = write_manifest(
            run_root=self.run_root,
            run_id=self.run_id,
            task=self.task,
            samples=samples_summary,
            audit_root_hash=audit_root,
            profile_excerpt={
                "planner": self.profile.models.planner.model,
                "browser_backend": self.profile.browser.backend,
                "concurrency": self.profile.browser.concurrency,
                "distributed": self.profile.queue.distributed,
            },
        )

        # Shut down the shared Chromium process we launched at execute() start.
        # (No-op in distributed mode, where the pool was never .setup()'d here.)
        if hasattr(self.pool, "teardown"):
            try:
                await self.pool.teardown()
            except Exception:
                pass

        return RunResult(
            run_id=self.run_id,
            run_root=self.run_root,
            total=total,
            passed=passed,
            failed=failed,
            extracted_rows=[s.get("extracted") or {} for s in samples_summary],
            aggregate_csv=csv_path,
            manifest=manifest_path,
        )

    # --- durability rendering ---

    def _iter_sample_rows(self):
        if not self.samples_jsonl.exists():
            return
        with self.samples_jsonl.open() as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    yield json.loads(line)
                except Exception:
                    continue

    def _samples_summary_from_jsonl(self) -> list[dict[str, Any]]:
        """Compact per-sample dicts for RUN_MANIFEST (no evidence arrays)."""
        seen: dict[str, dict[str, Any]] = {}
        for row in self._iter_sample_rows():
            sid = row.get("sample_id")
            if not sid:
                continue
            seen[sid] = {
                "sample_id": sid,
                "row_index": row.get("row_index"),
                "verdict": row.get("verdict"),
                "verdict_reason": row.get("verdict_reason"),
                "status": row.get("status"),
                "evidence_count": row.get("evidence_count"),
                "extracted": row.get("extracted") or {},
                "error": row.get("error"),
            }
        return sorted(seen.values(), key=lambda s: s.get("row_index") or 0)

    def _rebuild_csv_from_jsonl(self, path: Path) -> None:
        """Emit aggregate CSV from the durable JSONL source of truth."""
        rows = self._samples_summary_from_jsonl()
        if not rows:
            path.write_text("")
            return
        keys: list[str] = []
        seen: set[str] = set()
        for r in rows:
            for k in (r.get("extracted") or {}).keys():
                if k not in seen:
                    seen.add(k)
                    keys.append(k)
        with path.open("w", newline="") as f:
            w = csv.writer(f)
            w.writerow(["sample_id", "row_index", "verdict", *keys])
            for r in rows:
                extracted = r.get("extracted") or {}
                w.writerow([
                    r.get("sample_id"),
                    r.get("row_index"),
                    r.get("verdict"),
                    *[extracted.get(k, "") for k in keys],
                ])


def _apply_task_overrides(profile: Profile, task: dict[str, Any]) -> Profile:
    """Tasks may carry `profile_overrides` that tighten the profile for
    their specific risk profile (e.g. LinkedIn drops concurrency to 1).
    We return a shallow-updated copy so other runs aren't affected.
    """
    overrides = task.get("profile_overrides") or {}
    if not overrides:
        return profile
    data = profile.model_dump()
    for top_key, inner in overrides.items():
        if top_key not in data or not isinstance(inner, dict):
            continue
        current = data[top_key]
        if isinstance(current, dict):
            current.update(inner)
        else:
            data[top_key] = inner
    return Profile.model_validate(data)


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
    profile = _apply_task_overrides(profile, task)
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


async def resume(*, profile: Profile, run_id: str) -> RunResult:
    """Resume a previously-started run from its durable state.

    Requires `runs/<run_id>/.run_config.json` + `data/<run_id>.queue.db`
    to exist. Any claimed-but-never-acked samples are released and
    retried by the new workers.
    """
    run_root = Path("runs") / run_id
    cfg_path = run_root / ".run_config.json"
    if not cfg_path.exists():
        raise FileNotFoundError(f"run config missing: {cfg_path}")
    cfg = json.loads(cfg_path.read_text())
    task = cfg["task"]
    # No re-enqueue of input rows needed: queue remembers pending items.
    wf = RunWorkflow(
        profile=profile,
        task=task,
        input_rows=[],  # nothing to enqueue; queue persists work items
        run_id=run_id,
        resuming=True,
    )
    return await wf.execute()
