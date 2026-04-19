"""Typed loader for `config/profile.yaml` — the single switch panel."""

from __future__ import annotations

from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, Field


class ModelSpec(BaseModel):
    provider: str
    model: str


class RolesConfig(BaseModel):
    planner: ModelSpec
    navigator: ModelSpec
    extractor: ModelSpec
    judge: ModelSpec


class Viewport(BaseModel):
    width: int = Field(default=1440, ge=320)
    height: int = Field(default=900, ge=240)


class BrowserConfig(BaseModel):
    backend: Literal["local", "browserbase"] = "local"
    headless: bool = False
    concurrency: int = Field(default=4, ge=1, le=64)
    stealth: bool = True
    viewport: Viewport = Field(default_factory=Viewport)
    # When true, the orchestrator starts a CDP screencast per sample and
    # publishes frames on the EventBus. Off by default: adds CPU load and
    # is only useful when a dashboard is watching.
    screencast: bool = False
    screencast_fps: int = Field(default=10, ge=1, le=30)
    # Per-host politeness. Token bucket at `per_host_rps` with `per_host_burst`
    # capacity. Applied to every `goto` inside a browser session.
    per_host_rps: float = Field(default=2.0, gt=0.0)
    per_host_burst: int = Field(default=4, ge=1)


class QueueConfig(BaseModel):
    backend: Literal["sqlite", "redis", "nats"] = "sqlite"
    path: str = "./data/queue.db"
    redis_url: str = "redis://localhost:6379/0"
    redis_prefix: str | None = None     # default: andera:queue:<run_id>
    max_attempts: int = Field(default=3, ge=1, le=20)
    # When true, the API/coordinator enqueues samples but does NOT
    # spawn in-process workers — external agent processes (e.g. the
    # `agent` containers in docker-compose) pull from the shared queue.
    # Requires backend=redis. Uses the GLOBAL prefix so any agent
    # can serve any run.
    distributed: bool = False


class ArtifactsConfig(BaseModel):
    backend: Literal["filesystem", "s3"] = "filesystem"
    root: str = "./runs"


class MetadataConfig(BaseModel):
    backend: Literal["sqlite", "postgres"] = "sqlite"
    path: str = "./data/state.db"
    postgres_url: str = "postgresql://andera:andera@localhost:5432/andera"


class StorageConfig(BaseModel):
    artifacts: ArtifactsConfig = Field(default_factory=ArtifactsConfig)
    metadata: MetadataConfig = Field(default_factory=MetadataConfig)


class IntegrationConfig(BaseModel):
    mode: Literal["auto", "manual", "off"] = "manual"
    token_env: str | None = None
    base_url: str | None = None


class LangfuseConfig(BaseModel):
    enabled: bool = False
    host: str = "http://localhost:3000"
    public_key_env: str = "LANGFUSE_PUBLIC_KEY"
    secret_key_env: str = "LANGFUSE_SECRET_KEY"


class ObservabilityConfig(BaseModel):
    langfuse: LangfuseConfig = Field(default_factory=LangfuseConfig)


class EvalConfig(BaseModel):
    gate_accuracy: float = Field(default=0.9, ge=0.0, le=1.0)
    tasks_dir: str = "./config/tasks"


class Profile(BaseModel):
    models: RolesConfig
    browser: BrowserConfig = Field(default_factory=BrowserConfig)
    queue: QueueConfig = Field(default_factory=QueueConfig)
    storage: StorageConfig = Field(default_factory=StorageConfig)
    integrations: dict[str, IntegrationConfig] = Field(default_factory=dict)
    observability: ObservabilityConfig = Field(default_factory=ObservabilityConfig)
    eval: EvalConfig = Field(default_factory=EvalConfig)


DEFAULT_PROFILE_PATH = Path("config/profile.yaml")


def load_profile(path: str | Path | None = None) -> Profile:
    p = Path(path) if path else DEFAULT_PROFILE_PATH
    if not p.exists():
        raise FileNotFoundError(f"profile not found at {p.resolve()}")
    with p.open() as f:
        raw = yaml.safe_load(f) or {}
    return Profile.model_validate(raw)
