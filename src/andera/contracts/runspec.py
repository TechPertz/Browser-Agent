from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

RunMode = Literal["manual", "integration", "auto"]


class RunSpec(BaseModel):
    """A natural-language task plus input dataset to execute."""

    run_id: str
    task_id: str
    task_name: str
    task_prompt: str
    input_path: str
    output_dir: str
    mode: RunMode = "auto"
    concurrency: int = Field(default=4, ge=1, le=64)
    max_samples: int | None = Field(default=None, ge=1)
    seed: int | None = None
