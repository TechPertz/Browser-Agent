"""Phase 2 acceptance: run the full agent graph on one sample.

Usage:
  ANTHROPIC_API_KEY=... uv run python scripts/run_one_sample.py \
    --task config/tasks/03-github-issue.yaml \
    --url https://github.com/python/cpython/issues/101

Without ANTHROPIC_API_KEY set, the script exits cleanly with instructions.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import uuid
from pathlib import Path

import yaml
from dotenv import load_dotenv

from andera.agent import run_sample
from andera.agent.nodes import AgentDeps
from andera.browser import BrowserPool
from andera.config import load_profile
from andera.models import Role, get_model
from andera.storage import FilesystemArtifactStore
from andera.tools.browser import BrowserTools


def _load_task(path: Path) -> dict:
    with path.open() as f:
        return yaml.safe_load(f)


async def main() -> int:
    load_dotenv()
    ap = argparse.ArgumentParser()
    ap.add_argument("--task", required=True, type=Path)
    ap.add_argument("--url", required=True)
    ap.add_argument("--headless", action="store_true")
    args = ap.parse_args()

    if not os.environ.get("ANTHROPIC_API_KEY"):
        print(
            "ANTHROPIC_API_KEY is not set.\n"
            "Copy env.example to .env and fill ANTHROPIC_API_KEY to run end-to-end.",
            file=sys.stderr,
        )
        return 2

    profile = load_profile()
    task = _load_task(args.task)

    run_id = f"run-{uuid.uuid4().hex[:8]}"
    sample_id = f"sample-{uuid.uuid4().hex[:8]}"
    run_root = Path("runs") / run_id
    store = FilesystemArtifactStore(run_root)
    pool = BrowserPool(
        artifacts=store,
        concurrency=1,
        headless=args.headless,
        viewport=profile.browser.viewport.model_dump(),
    )

    async with pool.acquire(sample_id=sample_id, run_id=run_id) as session:
        deps = AgentDeps(
            planner=get_model(Role.PLANNER, profile),
            navigator=get_model(Role.NAVIGATOR, profile),
            extractor=get_model(Role.EXTRACTOR, profile),
            judge=get_model(Role.JUDGE, profile),
            browser=BrowserTools(session),
        )
        initial = {
            "run_id": run_id,
            "sample_id": sample_id,
            "task_prompt": task["prompt"],
            "input_data": {"url": args.url},
            "start_url": args.url,
            "extract_schema": task["extract_schema"],
            "status": "pending",
        }
        final = await run_sample(deps=deps, initial_state=initial, thread_id=sample_id)

    print(json.dumps(
        {
            "run_id": run_id,
            "sample_id": sample_id,
            "status": final.get("status"),
            "verdict": final.get("verdict"),
            "verdict_reason": final.get("verdict_reason"),
            "extracted": final.get("extracted"),
            "evidence_count": len(final.get("evidence") or []),
            "steps_executed": final.get("step_index"),
        },
        indent=2,
        default=str,
    ))

    # also persist a result.json next to the artifacts
    (run_root / f"{sample_id}.result.json").write_text(json.dumps(final, default=str, indent=2))

    return 0 if final.get("verdict") == "pass" else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
