"""Andera FastAPI app.

JSON routes at `/api/...`. HTMX-served HTML routes at `/ui/...`.
Same process runs the dashboard, the run coordinator, and (in
distributed mode) the finalizer loop that watches for queue drain
and wraps up runs whose samples were processed by external agents.
"""

from __future__ import annotations

import asyncio
import logging
import os
from contextlib import asynccontextmanager
from typing import AsyncIterator

from dotenv import load_dotenv
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from andera.config import load_profile
from andera.storage import init_db

from .registry import get_registry
from .routes import connections, evidence, events, plan, runs, samples, screencast, ui

log = logging.getLogger(__name__)
FINALIZER_INTERVAL_S = 2.0


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    # .env loaded from the repo root (or whatever CWD the container runs in).
    load_dotenv()

    # SQLite init is cheap and harmless even when backend=postgres.
    init_db()

    # Postgres backend → apply schema + LangGraph checkpoint tables.
    profile = load_profile()
    if profile.storage.metadata.backend == "postgres":
        try:
            from andera.storage.pg_migrate import migrate
            result = await migrate(profile.storage.metadata.postgres_url)
            log.info("postgres migrate: %s", result)
        except Exception as e:
            log.warning("postgres migrate failed at boot: %s", e)

    # Finalizer loop: scans the in-memory registry every FINALIZER_INTERVAL_S
    # for runs whose queues have drained (pending + claimed == 0) and
    # calls their finalize() once. Safe no-op when registry is empty or
    # when runs haven't reached drain yet.
    finalizer_task = asyncio.create_task(_finalizer_loop())
    try:
        yield
    finally:
        finalizer_task.cancel()
        try:
            await finalizer_task
        except (asyncio.CancelledError, Exception):
            pass


async def _finalizer_loop() -> None:
    registry = get_registry()
    while True:
        try:
            # Iterate over a snapshot of the registry so concurrent POSTs
            # don't break the loop.
            for rec in list(registry.pending_finalization()):
                wf = getattr(rec, "workflow", None)
                if wf is None:
                    continue
                try:
                    if await wf.queue_drained():
                        result = await wf.finalize()
                        rec.status = "completed"
                        rec.total = result.total
                        rec.passed = result.passed
                        rec.failed = result.failed
                        rec.run_root = str(result.run_root)
                        registry.mark_finalized(rec.run_id)
                except Exception as e:
                    log.warning("finalizer tick for %s: %s", rec.run_id, e)
        except Exception as e:
            log.warning("finalizer loop error: %s", e)
        await asyncio.sleep(FINALIZER_INTERVAL_S)


def create_app() -> FastAPI:
    app = FastAPI(
        title="Andera",
        description="General Browser Agent — audit evidence collection",
        version="0.1.0",
        lifespan=lifespan,
    )
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_methods=["*"],
        allow_headers=["*"],
    )
    app.include_router(runs.router, tags=["runs"])
    app.include_router(samples.router, tags=["samples"])
    app.include_router(evidence.router, tags=["evidence"])
    app.include_router(events.router, tags=["events"])
    app.include_router(connections.router, tags=["connections"])
    app.include_router(screencast.router, tags=["screencast"])
    app.include_router(plan.router, tags=["plan"])
    app.include_router(ui.router, tags=["ui"])

    @app.get("/api/health")
    async def health() -> dict:
        """Never-fail health check. Surfaces config status so the
        dashboard can warn visibly on missing keys / backends."""
        out = {
            "ok": True,
            "anthropic_configured": bool(os.environ.get("ANTHROPIC_API_KEY")),
        }
        try:
            profile = load_profile()
            out["queue_backend"] = profile.queue.backend
            out["queue_distributed"] = profile.queue.distributed
            out["metadata_backend"] = profile.storage.metadata.backend
            if profile.storage.metadata.backend == "postgres":
                # Best-effort PG reachability probe. Don't fail health just
                # because PG isn't up yet at boot — let the ping inform.
                try:
                    import asyncpg
                    conn = await asyncio.wait_for(
                        asyncpg.connect(profile.storage.metadata.postgres_url),
                        timeout=1.5,
                    )
                    await conn.close()
                    out["postgres_ok"] = True
                except Exception as e:
                    out["postgres_ok"] = False
                    out["postgres_error"] = f"{type(e).__name__}: {e}"
        except Exception as e:
            out["profile_error"] = f"{type(e).__name__}: {e}"
        return out

    return app


app = create_app()
