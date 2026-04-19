"""Idempotent Postgres migration.

Run at container boot (via `andera migrate` or API lifespan) to ensure
the audit_log table and LangGraph's checkpoint tables exist. Safe to
run many times — all DDL is `IF NOT EXISTS`.
"""

from __future__ import annotations

from .audit_log_pg import _PG_SCHEMA


async def migrate(postgres_url: str) -> dict[str, str]:
    """Apply the schema. Returns a dict of {table_name: "ok"} for logging."""
    result: dict[str, str] = {}

    # Audit log schema
    try:
        import asyncpg
        conn = await asyncpg.connect(postgres_url)
        try:
            await conn.execute(_PG_SCHEMA)
            result["audit_log"] = "ok"
        finally:
            await conn.close()
    except Exception as e:
        result["audit_log"] = f"error: {type(e).__name__}: {e}"

    # LangGraph checkpoint tables (checkpoints + checkpoint_blobs +
    # checkpoint_writes + checkpoint_migrations). saver.setup() is the
    # blessed path per langgraph-checkpoint-postgres docs.
    try:
        from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
        async with AsyncPostgresSaver.from_conn_string(postgres_url) as saver:
            await saver.setup()
        result["langgraph_checkpoints"] = "ok"
    except Exception as e:
        result["langgraph_checkpoints"] = f"error: {type(e).__name__}: {e}"

    return result
