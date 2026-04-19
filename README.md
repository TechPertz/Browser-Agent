# Andera — General Browser Agent for Audit Evidence

System-agnostic browser agent that takes a natural-language task + an
input file (CSV/Excel/JSONL) and produces audit-grade evidence: one
folder per sample with screenshots + extracted data, plus an aggregate
CSV. Works on any system reachable in a browser (Linear, GitHub,
Workday, Jira, LinkedIn, internal tools).

Built for the Andera (andera.ai) work trial. Optimized in this order:
**accuracy > generality > scalability > consistency > speed.**

## Architecture at a glance

```
 ┌─── config/profile.yaml ───── one switch panel ─────────────────┐
 │  models · browser backend · queue · storage · integrations     │
 │  per_host_rps · stealth · screencast · observability            │
 └─────────────────────────────────────────────────────────────────┘

 Web dashboard (HTMX)        CLI (dev)
 localhost:8000              andera run / resume / login / verify
         │                               │
         └──────────┬────────────────────┘
                    │
            ┌───────▼────────┐
            │  Classifier    │  NL task → extract/form_fill/list_iter/navigate
            │  (Haiku)       │
            └───────┬────────┘
                    │
            ┌───────▼─────────┐   ┌──────────────────────┐
            │  Orchestrator   │──▶│  SQLite queue        │
            │  (asyncio, N    │   │  (claim-lease,       │
            │   workers)      │   │   resumable)         │
            └───────┬─────────┘   └──────────────────────┘
                    │ fan-out per sample
     ┌──────────────┼──────────────┐
     │              │              │
┌────▼────┐   ┌─────▼────┐  ┌─────▼────┐   per-sample LangGraph:
│ Sample  │   │  Sample  │  │  Sample  │   CLASSIFY → PLAN → ACT →
│ (graph) │   │  (graph) │  │  (graph) │   OBSERVE → VERIFY → EXTRACT
└────┬────┘   └─────┬────┘  └─────┬────┘   → JUDGE → DONE
     │              │              │
     └──── Playwright (stealth + per-host rate limiter) ────┐
                                                            ▼
                                         evidence: runs/<run_id>/blobs/...
                                         audit:    hash-chained SQLite log
                                         manifest: RUN_MANIFEST.json (verifiable)
```

## Quick demo (3 commands)

```bash
# 1. Install
uv sync --extra dev
uv run playwright install chromium

# 2. Set keys
cp env.example .env        # then fill ANTHROPIC_API_KEY

# 3. Run a task
uv run andera run \
  --task config/tasks/02-linear-tickets.yaml \
  --input tests/fixtures/02-linear-tickets.csv \
  --max-samples 2

# Inspect evidence:
ls runs/<run_id>/         # output.csv + RUN_MANIFEST.json + blobs/
uv run andera verify runs/<run_id>   # re-hash + verify audit chain
```

## Dashboard

```bash
uv run uvicorn andera.api.app:app --reload --port 8000
open http://localhost:8000
```

## CLI

| Command | What it does |
|---|---|
| `andera run -t <task.yaml> -i <input>` | Execute a task across all input rows in parallel. |
| `andera resume <run_id>` | Resume a crashed/Ctrl-C'd run. |
| `andera login <host> --url <login-url>` | Open headed browser, seal session state for future runs. |
| `andera verify <run-root>` | Re-hash every artifact + confirm audit chain is unbroken. |
| `andera check` | Smoke: profile loads, DB init, real Chromium navigates example.com. |

## The 5 trial tasks (config/tasks/)

| File | Task type | Scenario |
|---|---|---|
| `01-github-workday-join.yaml` | `list_iter` | Enrich users by joining GitHub profile + Workday record. |
| `02-linear-tickets.yaml` | `extract` | Per-URL ticket metadata (ticket #, assignee, due). |
| `03-github-commits-audit.yaml` | `navigate` | Commit → Checks → CI drill-in → Jira link follow. |
| `04-linkedin-enrichment.yaml` | `extract` | Profile lookup with Google fallback. Auto-drops concurrency, enables stealth, 0.5 rps. |
| `05-workday-form-download.yaml` | `form_fill` | Fill form, submit, capture confirmation + attachment. |

Fixtures live in `tests/fixtures/0<N>-*.csv`.

## Why the design choices

- **One-switch config (`profile.yaml`)**: model tier, browser backend, queue, storage, integrations. Every external dep is swappable without code changes. When trial-day keys arrive, only `profile.yaml` changes.
- **Hexagonal / Protocol-based**: every external dep lives behind a Protocol in `src/andera/contracts/`. SQLite queue today, Redis tomorrow.
- **LangGraph state machine per sample**: durable checkpoints (SqliteSaver), bounded reflection (N=3), re-callable planner on consecutive verify failures.
- **Set-of-Mark grounding**: the navigator sees numbered interactive elements with bounding boxes — works on unknown UIs including shadow DOM + same-origin iframes.
- **Specialist subagents**: classifier picks `extract / form_fill / list_iter / navigate`. Each has a tuned system prompt.
- **Content-addressed artifacts**: filename = sha256(bytes). Immutable, deduplicated, tamper-evident.
- **Hash-chained audit log**: every run-level event chains `prev_hash → this_hash`. `RUN_MANIFEST.json` pins every artifact and the audit root.
- **AES-GCM sealed credentials**: Playwright `storage_state` per host, encrypted with `ANDERA_MASTER_KEY`. `.sealed` files can be committed; useless without the key.
- **Per-host rate limiter + stealth**: one bucket per target host. Concurrency 10 doesn't hammer a single site. LinkedIn drops to 0.5 rps + stealth + concurrency 1 via task YAML override.
- **Resumable**: `andera resume <run_id>` reads the durable queue + JSONL sample log and picks up exactly where a crash stopped.

## Observability

- **Always-on local JSONL trace** under `data/traces/<date>.jsonl`.
- **Optional Langfuse** via `docker compose -f docker/compose.yaml up -d` + env keys. LiteLLM's built-in callback traces every LLM call with cost + latency.

## Tests

```bash
uv run pytest -q                          # ~200 tests
uv run pytest tests/eval -q               # rubric gate (eval harness)
```

## Module map

```
src/andera/
├── contracts/         types + Protocol ports (the only cross-module surface)
├── config/            profile.yaml loader (typed, Literal-validated)
├── storage/           SQLite schema, content-addressed artifact store,
│                       hash-chained audit log, RUN_MANIFEST writer/verifier
├── queue/             SqliteQueue with claim-lease (safe concurrent dispatch)
├── browser/           Playwright session + pool + rate-limiter + stealth +
│                       Set-of-Mark grounding + CDP screencast
├── agent/             LangGraph + nodes + prompts + plan cache + specialists
├── models/            LiteLLM adapter, role-routed (planner/navigator/extractor/judge)
├── tools/             typed agent-facing tool wrappers
├── orchestrator/      RunWorkflow (fan-out, retry, resume, durable JSONL)
├── credentials/       AES-GCM sealed storage_state + interactive login
├── api/               FastAPI (JSON + HTMX UI + WebSocket events)
├── observability/     JSONL trace sink + opt-in Langfuse callback
├── planner/           NL → task spec
├── eval/              frozen-fixture harness with composite scorer
└── cli.py             andera {run, resume, login, verify, check}
```

## Runtime

```
Local laptop                            Production swap path (profile.yaml)
────────────────                        ───────────────────────────────────
SQLite (WAL)          queue, state   →  Redis / Postgres
Filesystem            artifacts      →  S3
LocalPlaywrightSession browser       →  BrowserbaseSession
JSONL traces          observability  →  Langfuse (self-hosted or cloud)
Claude via LiteLLM    LLM            →  any LiteLLM-supported provider
```
