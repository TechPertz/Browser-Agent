# CLAUDE.md — Andera Work Trial

**Project:** General Browser Agent for audit-evidence collection (1-day build for andera.ai trial).
**Source of truth:** `PLAN.md` (architecture, phases, acceptance gates). Read it before any non-trivial change.
**Durable context:** `memory/` (project memory + engineering-style preferences).

## Stack (locked)

- **Orchestration:** LangGraph + `langgraph-checkpoint-sqlite` (per-sample StateGraph, durable checkpoints).
- **Models:** LiteLLM with role-routed Claude (Opus planner/judge, Sonnet navigator, Haiku extractor). Swappable via `config/profile.yaml`.
- **Browser:** Playwright (async) + Stagehand (act/observe/extract). `LocalPlaywrightSession` default, `BrowserbaseSession` stub swap.
- **API / UI:** FastAPI + HTMX + WebSocket (CDP screencast for live browser view).
- **Storage:** filesystem for artifacts (content-addressed by sha256), SQLite (WAL) for metadata + queue + hash-chained audit log.
- **Observability:** Langfuse self-hosted via docker-compose.
- **Eval:** pytest harness, 90% accuracy gate over the 5 spec tasks.

## Architecture rules (non-negotiable)

1. **Hexagonal / ports-and-adapters.** Every external dep lives behind a `Protocol` in `src/andera/contracts/protocols.py`. Code depends on Protocols, not implementations.
2. **One switch panel.** All runtime swaps (model, browser backend, queue, storage, integrations) flow through `config/profile.yaml`. No hardcoded provider names outside adapters.
3. **Contracts are the only cross-module import.** Modules talk through `contracts/` types; no direct imports between `agent/`, `tools/`, `browser/`, `storage/`, `api/`.
4. **Pure agent nodes.** Every LangGraph node is a pure function: `(State) -> State`. Side effects happen through injected tool adapters.
5. **Typed Pydantic I/O everywhere.** RunSpec, Sample, Artifact, Event, ToolResult — all Pydantic. No dict-shaped payloads crossing module boundaries.
6. **Content-addressed artifacts.** Evidence filenames = `sha256(content)[:16]` + ext. Never trust caller-supplied names.
7. **Hash-chained audit log.** Every tool call / state transition appends to SQLite `audit_log` with `prev_hash` → `this_hash`. Tamper-evident by construction.
8. **Bounded reflection.** Agent self-correction capped at `N=3` retries per sample. No infinite loops.
9. **Secrets never in code.** `.env` for keys, AES-GCM sealed `storage_state` per host for browser credentials. `.env` is gitignored.

## How to work in this repo

**Package manager:** `uv` (not pip). Dep changes go in `pyproject.toml`, then `uv sync`.

**Run tests:** `uv run pytest -q` (unit), `uv run pytest tests/eval -q` (eval harness).

**Run API locally:** `uv run uvicorn andera.api.app:app --reload --port 8000`.

**Run a task end-to-end (dev CLI):** `uv run python -m andera.cli run --task config/tasks/02_linear.yaml --input runs/inputs/linear.csv`.

**Start infra (Langfuse + mock Workday):** `docker compose -f docker/docker-compose.yml up -d`.

## Commit discipline (see PLAN.md Part 6.5 for full detail)

- Conventional Commits format: `feat(scope): ...`, `fix(scope): ...`, `chore: ...`, `test(scope): ...`, `refactor(scope): ...`.
- Small commits. Aim for 50–300 lines staged; split mid-phase at natural seams.
- **Before every commit:** run `/review` on the staged diff. Resolve all P1/P2 findings before committing.
- Stage explicit files: `git add path/to/file.py` — **never** `git add -A` or `git add .`.
- One phase = at least one commit, usually 2–5.

## Build order

Sequential by phase, per PLAN.md Part 4: **Phase -1 (done) → 0 → 1 → 2 → 3 → 4 → 5a → 5b → 6 → 7 → 8 → 9 → 10**.

Each phase ends with: acceptance test passes → `/review` clean → commit(s).

## Docs lookups (use Context7 MCP, not web search)

Registered library IDs for `mcp__context7__query-docs`:
- LangGraph: `/langchain-ai/langgraph`
- FastAPI: `/fastapi/fastapi`
- Playwright Python: `/microsoft/playwright-python`
- Stagehand Python: `/browserbase/stagehand-python`
- LiteLLM: `/berriai/litellm`

Always consult Context7 before writing non-trivial code against any of these — APIs move faster than training data.

## Judging rubric priority (Andera spec — optimize in this order)

1. **Accuracy** — extracted data matches ground truth; evidence proves the claim.
2. **Generality** — agent works on unseen systems with only a NL task + input file.
3. **Scalability** — handles thousands of samples; parallel, checkpointed, resumable.
4. **Consistency** — same input → same output. Deterministic seeds, idempotent writes.
5. **Speed** — last, but don't waste cycles.

When trading off, accuracy beats everything. Never ship a faster-but-wrong path.
