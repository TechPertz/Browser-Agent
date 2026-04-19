"""Andera CLI — `andera run <task.yaml> --input <file>`.

Dev-only entrypoint. The primary product UX is the FastAPI dashboard.
"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

import typer
import yaml
from dotenv import load_dotenv

from andera.config import load_profile
from andera.orchestrator import run as orchestrator_run

app = typer.Typer(
    name="andera",
    help="Andera browser agent — audit evidence collection at scale.",
    no_args_is_help=True,
)


def _load_task(path: Path) -> dict:
    with path.open() as f:
        return yaml.safe_load(f)


@app.command()
def run(
    task: Path = typer.Option(..., "--task", "-t", help="Path to task YAML."),
    input: Path = typer.Option(..., "--input", "-i", help="Path to input CSV/JSONL/XLSX."),
    run_id: str | None = typer.Option(None, "--run-id", help="Override auto-generated run id."),
    max_samples: int | None = typer.Option(None, "--max-samples", help="Cap rows processed."),
    profile_path: Path | None = typer.Option(None, "--profile", help="Alt profile.yaml path."),
) -> None:
    """Execute a task across all rows in an input file, in parallel."""
    load_dotenv()
    if not task.exists():
        typer.echo(f"task file not found: {task}", err=True)
        raise typer.Exit(2)
    if not input.exists():
        typer.echo(f"input file not found: {input}", err=True)
        raise typer.Exit(2)

    profile = load_profile(profile_path)
    task_spec = _load_task(task)

    typer.echo(f"run: task={task_spec.get('task_id')} profile={profile.models.planner.model}")

    result = asyncio.run(
        orchestrator_run(
            profile=profile,
            task=task_spec,
            input_path=input,
            run_id=run_id,
            max_samples=max_samples,
        )
    )

    typer.echo(json.dumps({
        "run_id": result.run_id,
        "total": result.total,
        "passed": result.passed,
        "failed": result.failed,
        "evidence_root": str(result.run_root),
        "aggregate_csv": str(result.aggregate_csv) if result.aggregate_csv else None,
        "manifest": str(result.manifest) if result.manifest else None,
    }, indent=2))

    raise typer.Exit(0 if result.failed == 0 else 1)


@app.command()
def worker(
    run_id: str = typer.Argument(..., help="Run id the coordinator already started."),
    worker_id: str | None = typer.Option(None, "--worker-id", help="Stable name; default is a uuid."),
    profile_path: Path | None = typer.Option(None, "--profile", help="Alt profile.yaml path."),
) -> None:
    """Consume samples from the shared queue.

    Run one of these per worker pod. Requires `profile.queue.backend=redis`
    (or another shared backend) for multi-box scaling. With sqlite, it
    still works on the same filesystem. Coordinator finalizes the run
    once the queue drains.
    """
    load_dotenv()
    from andera.worker import run_worker

    processed = asyncio.run(
        run_worker(run_id, profile_path=profile_path, worker_id=worker_id)
    )
    typer.echo(json.dumps({"run_id": run_id, "processed": processed}))


@app.command()
def resume(
    run_id: str = typer.Argument(..., help="Existing run id under runs/."),
    profile_path: Path | None = typer.Option(None, "--profile", help="Alt profile.yaml path."),
) -> None:
    """Resume a previously-started run after a crash or Ctrl-C."""
    load_dotenv()
    from andera.orchestrator import resume as orchestrator_resume

    profile = load_profile(profile_path)
    result = asyncio.run(orchestrator_resume(profile=profile, run_id=run_id))
    typer.echo(json.dumps({
        "run_id": result.run_id,
        "total": result.total,
        "passed": result.passed,
        "failed": result.failed,
        "evidence_root": str(result.run_root),
    }, indent=2))
    raise typer.Exit(0 if result.failed == 0 else 1)


@app.command()
def check() -> None:
    """Smoke-check: profile loads, DB init, browser launches example.com."""
    from andera.browser import BrowserPool
    from andera.storage import FilesystemArtifactStore, init_db

    profile = load_profile()
    init_db()
    typer.echo(f"profile ok: planner={profile.models.planner.model}")

    async def _browser():
        store = FilesystemArtifactStore("runs/smoke")
        pool = BrowserPool(artifacts=store, concurrency=1, headless=True)
        async with pool.acquire(sample_id="check", run_id="check") as s:
            await s.goto("https://example.com")
            snap = await s.snapshot()
            return snap

    snap = asyncio.run(_browser())
    typer.echo(f"browser ok: {snap.get('title')!r} @ {snap.get('url')}")


@app.command()
def login(
    host: str = typer.Argument(..., help="Short name for the host (e.g. github, linear)."),
    url: str = typer.Option(..., "--url", "-u", help="Login URL to open."),
) -> None:
    """Open a headed browser, wait for you to sign in, seal the session state."""
    load_dotenv()
    from andera.credentials.login_flow import interactive_login

    path = asyncio.run(interactive_login(host=host, login_url=url))
    typer.echo(f"sealed state saved: {path}")


@app.command()
def verify(
    run_root: Path = typer.Argument(..., help="Path to the run directory (runs/<run_id>)."),
) -> None:
    """Re-hash every artifact + verify RUN_MANIFEST + audit-log chain."""
    from andera.storage import AuditLog, verify_manifest

    if not run_root.exists():
        typer.echo(f"run not found: {run_root}", err=True)
        raise typer.Exit(2)

    report = verify_manifest(run_root)
    typer.echo(f"manifest: {'OK' if report['ok'] else 'FAIL'}")
    typer.echo(f"  manifest_hash_ok: {report['manifest_hash_ok']}")
    typer.echo(f"  artifacts_checked: {report['artifacts_checked']}")
    if report["bad_artifacts"]:
        typer.echo(f"  bad_artifacts: {len(report['bad_artifacts'])}")

    # Walk the run_id audit DB if present (data/<run_id>.audit.db)
    run_id = run_root.name
    audit_path = Path("data") / f"{run_id}.audit.db"
    if audit_path.exists():
        audit = AuditLog(audit_path)
        chain_ok = audit.verify_chain()
        typer.echo(f"audit_chain: {'OK' if chain_ok else 'FAIL'}")
        typer.echo(f"  root_hash: {audit.root_hash(run_id)}")
    else:
        typer.echo(f"audit_chain: SKIPPED (no audit db at {audit_path})")

    raise typer.Exit(0 if report["ok"] else 1)


def main() -> None:
    app()


if __name__ == "__main__":
    main()
