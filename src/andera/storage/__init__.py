from .artifact_store import FilesystemArtifactStore, sha256_hex
from .audit_log import AuditLog
from .db import connect, init_db
from .manifest import verify_manifest, write_manifest

__all__ = [
    "AuditLog",
    "FilesystemArtifactStore",
    "connect",
    "init_db",
    "make_audit_log",
    "sha256_hex",
    "verify_manifest",
    "write_manifest",
]


def make_audit_log(
    *,
    backend: str,
    sqlite_path: str | None = None,
    postgres_url: str | None = None,
):
    """Return an audit-log adapter for the requested backend.

    Both implementations offer the SAME semantic public surface
    (append / root_hash / verify_chain / rows_for_run). The SQLite
    version is SYNC; the Postgres version is ASYNC. Callers that
    want a stable ASYNC interface can wrap the sync SQLite calls in
    `asyncio.to_thread` at the integration boundary — or stick with
    SQLite for dev and Postgres for deployed stacks.
    """
    backend = (backend or "sqlite").lower()
    if backend == "sqlite":
        if not sqlite_path:
            raise ValueError("sqlite audit log requires sqlite_path")
        return AuditLog(sqlite_path)
    if backend == "postgres":
        from .audit_log_pg import AuditLogPg
        if not postgres_url:
            raise ValueError("postgres audit log requires postgres_url")
        return AuditLogPg(postgres_url)
    raise ValueError(f"unknown audit backend: {backend!r}")
