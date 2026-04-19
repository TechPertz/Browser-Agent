"""Andera FastAPI app.

Phase 5a — API-first backbone. JSON routes are the primary surface
(`/api/...`). Phase 5b will add HTMX-served HTML routes at `/ui/...`
over the same app.
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from typing import AsyncIterator

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from andera.storage import init_db

from .routes import connections, evidence, events, runs, samples


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    # Warm: make sure the SQLite schema is applied before any request.
    init_db()
    yield


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

    @app.get("/api/health")
    async def health() -> dict:
        return {"ok": True}

    return app


app = create_app()
