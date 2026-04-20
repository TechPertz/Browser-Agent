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
    # Optional — visual_do is a no-op without a multimodal vision model.
    # Defaults to None so existing profiles (and tests) that don't
    # configure vision don't break.
    vision: ModelSpec | None = None


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
    raw = _apply_env_overrides(raw)
    return Profile.model_validate(raw)


def _apply_env_overrides(raw: dict) -> dict:
    """Let docker-compose / k8s flip backends via env WITHOUT editing
    profile.yaml (which would break local-dev SQLite defaults).

    Recognized vars:
      ANDERA_QUEUE_BACKEND            -> queue.backend
      ANDERA_QUEUE_DISTRIBUTED        -> queue.distributed  (truthy)
      ANDERA_REDIS_URL                -> queue.redis_url
      ANDERA_METADATA_BACKEND         -> storage.metadata.backend
      ANDERA_POSTGRES_URL             -> storage.metadata.postgres_url
      ANDERA_BACKEND                  -> convenience: sets BOTH
                                         queue.backend=redis AND
                                         storage.metadata.backend=postgres
                                         (when value is "postgres")
    """
    import os
    raw = dict(raw)  # shallow copy
    q = dict(raw.get("queue") or {})
    s = dict(raw.get("storage") or {})
    meta = dict((s.get("metadata") or {}))
    br = dict(raw.get("browser") or {})

    if "ANDERA_HEADLESS" in os.environ:
        br["headless"] = os.environ["ANDERA_HEADLESS"].lower() in (
            "1", "true", "yes", "on"
        )
    if "ANDERA_CONCURRENCY" in os.environ:
        try:
            br["concurrency"] = int(os.environ["ANDERA_CONCURRENCY"])
        except ValueError:
            pass

    if os.environ.get("ANDERA_BACKEND") == "postgres":
        # Convenience: flip both queue AND metadata to their production
        # defaults. Explicit per-setting env vars below still take
        # precedence if the operator needs to override granularly.
        q["backend"] = "redis"
        q["distributed"] = True
        meta["backend"] = "postgres"

    if "ANDERA_QUEUE_BACKEND" in os.environ:
        q["backend"] = os.environ["ANDERA_QUEUE_BACKEND"]
    if "ANDERA_QUEUE_DISTRIBUTED" in os.environ:
        q["distributed"] = os.environ["ANDERA_QUEUE_DISTRIBUTED"].lower() in (
            "1", "true", "yes", "on"
        )
    if "ANDERA_REDIS_URL" in os.environ:
        q["redis_url"] = os.environ["ANDERA_REDIS_URL"]
    if "ANDERA_METADATA_BACKEND" in os.environ:
        meta["backend"] = os.environ["ANDERA_METADATA_BACKEND"]
    if "ANDERA_POSTGRES_URL" in os.environ:
        meta["postgres_url"] = os.environ["ANDERA_POSTGRES_URL"]

    if q:
        raw["queue"] = q
    if meta:
        s["metadata"] = meta
        raw["storage"] = s
    if br:
        raw["browser"] = br
    return raw
