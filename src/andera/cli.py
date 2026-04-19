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


def main() -> None:
    app()


if __name__ == "__main__":
    main()
