# Andera — Full System Design

This is the reference document. It covers what the system does, how it's
built, what technologies it uses and why, what every file in the repo
is responsible for, and the end-to-end data flow from a user's input
row to a hash-verified piece of audit evidence.

**Companion docs:**
- `ABOUT.md` — honest capability/limitation report
- `ARCHITECTURE.md` — high-level diagram with mermaid
- `README.md` — quickstart + CLI reference
- `PLAN.md` — phase-by-phase build log

---

## 1. What this is

A **general browser agent** that takes:
1. A natural-language task description (YAML or API payload)
2. An input file (CSV, Excel, JSONL, or JSON)

…and produces for each input row:
- A folder of content-addressed **evidence** (screenshots, extracted data)
- An aggregate `output.csv` over all rows
- A tamper-evident `RUN_MANIFEST.json` with artifact hashes + audit root

The agent itself is driven by an LLM loop: a planner proposes steps, a
navigator verifies each one, an extractor fills the target schema, a
judge scores the result. No hard-coded selectors, no site-specific
code. Works on anything a browser can render.

Scale target: **1 laptop, thousands of samples, demo-grade.** Nothing
here assumes Kubernetes or a cluster; everything is one Python process
+ one shared Chromium, with clean seams to extract into services later.

---

## 2. Technology stack — what + why

| Layer | Tech | Why this |
|---|---|---|
| **Language** | Python 3.11+ (asyncio) | Playwright, LangGraph, and LiteLLM all have first-class Python. asyncio gives us cooperative concurrency without the memory cost of threads. |
| **Package mgr** | `uv` | 10-100× faster than pip, lockfile, resolves and installs in seconds. |
| **LLM orchestration** | LangGraph | StateGraph with durable checkpoints via `AsyncSqliteSaver`. We need bounded-retry loops (verify → reflect → act) — LangGraph models this as conditional edges. |
| **LLM gateway** | LiteLLM | One interface to every provider. `num_retries=3` + exponential backoff built in. Anthropic today, OpenAI/Gemini/Ollama tomorrow without touching adapter code. |
| **Agent models** | Claude Opus (planner, judge), Sonnet (navigator/verifier), Haiku (classifier, extractor) | Role-routed to the right cost/capability tier. Defined in `config/profile.yaml:models`. |
| **Browser automation** | Playwright Python (async) | Stable async API, shadow-DOM + iframe support, CDP access for screencast. Chromium only (multi-engine adds risk without gain for trial scope). |
| **Queue** | SQLite (WAL + `BEGIN IMMEDIATE` claim-lease) | Zero ops. One file per run. Claim-lease guarantees no two workers take the same item. Swap path: `TaskQueue` Protocol fits Redis / SQS / NATS. |
| **Metadata + checkpoints** | SQLite | Same reasoning. LangGraph has a native `SqliteSaver`; our own schema lives alongside. |
| **Artifact store** | Filesystem, content-addressed by `sha256(bytes)[:16]` | Immutable, dedup-by-construction, tamper-evident. Swap path: `ArtifactStore` Protocol fits S3. |
| **Audit log** | SQLite with hash-chained rows (`prev_hash → this_hash`) | Any tamper after the fact invalidates every row after it. `verify_chain()` is a cheap integrity check auditors can re-run. |
| **Credentials** | AES-GCM sealed Playwright `storage_state`, HKDF-derived key from `ANDERA_MASTER_KEY` | Sealed files are safe to commit; useless without the env key. |
| **API framework** | FastAPI | Typed routes, OpenAPI auto-gen, first-class WebSocket + lifespan. Same process as agent runtime. |
| **UI** | HTMX + Jinja | Server-rendered HTML with `hx-get`/`hx-post` for partial swaps. Live browser stream via `<img src="data:image/jpeg;base64,...">` over WebSocket. Zero npm. |
| **Observability** | JSONL trace (always on) + Langfuse (opt-in via docker) | LiteLLM's built-in Langfuse callback gives cost + latency per LLM call. Local JSONL is the no-config floor. |
| **Eval** | pytest + custom composite scorer (fields × evidence × judge) | Gate: pass_rate ≥ 0.9 per task. |
| **Secrets** | `.env` via `python-dotenv` | Gitignored. `env.example` (no leading dot, to avoid tooling auto-protecting it) is the template. |

---

## 3. Repo structure — every file, every folder

### Top level

```
andera/
├── README.md                 Quickstart + CLI reference
├── ABOUT.md                  Honest capability report
├── ARCHITECTURE.md           Mermaid diagram + topology
├── SYSTEM_DESIGN.md          This file
├── PLAN.md                   Phase-by-phase build log (source of truth)
├── CLAUDE.md                 AI-collaborator instructions for this repo
├── pyproject.toml            uv project: deps, console_scripts, pytest config
├── uv.lock                   Pinned deps
├── env.example               Template env vars (.env gitignored)
├── .gitignore                Excludes .env, data/, runs/, caches
├── config/
│   ├── profile.yaml          THE switch panel — every runtime swap
│   └── tasks/                One YAML per spec task (see §3.2)
├── services/
│   └── mock_workday/         Standalone FastAPI mock for tasks #1, #5
├── docker/
│   └── compose.yaml          Optional: Langfuse + Postgres stack
├── src/andera/               Main package (see §3.3)
├── tests/                    Unit + eval tests (see §3.4)
├── scripts/                  Dev helpers (smoke, seed, run_one_sample)
├── spec/                     Andera's original work-trial brief
├── data/                     Runtime (gitignored): queues, audit DBs, traces
├── runs/                     Evidence output (gitignored)
└── memory/                   Cross-session assistant memory (AI-only)
```

### 3.1 `config/` — the switch panel

| File | What it does |
|---|---|
| `config/profile.yaml` | Runtime config. Model roles, browser backend + concurrency + stealth + rps, queue/storage backends, integrations, observability flags, eval gate threshold. Every swap goes through here. |
| `config/tasks/01-github-workday-join.yaml` | Task #1: per-user enrichment across GitHub + Workday. `task_type: list_iter`. |
| `config/tasks/02-linear-tickets.yaml` | Task #2: per-URL Linear ticket metadata. `task_type: extract`. |
| `config/tasks/03-github-commits-audit.yaml` | Task #3: commit → Checks → CI → Jira drill-in. `task_type: navigate`. |
| `config/tasks/04-linkedin-enrichment.yaml` | Task #4: LinkedIn profile enrichment with Google fallback. `task_type: extract` + `profile_overrides` (concurrency=1, stealth=true, 0.5 rps). |
| `config/tasks/05-workday-form-download.yaml` | Task #5: fill Workday form, submit, capture confirmation. `task_type: form_fill`. |
| `config/tasks/03-github-issue.yaml` | Original phase-2 dev task. Still useful for smoke runs. |

### 3.2 `src/andera/` — the package

Organized hexagonally: **contracts** define the ports, every other
folder is an adapter or a user of a contract. No module outside
`contracts/` imports another sibling's internals.

#### `contracts/` — types + Protocol ports (the only cross-module surface)

| File | What it contains |
|---|---|
| `contracts/runspec.py` | `RunSpec` — a task + input file + exec mode + concurrency. |
| `contracts/sample.py` | `Sample` Pydantic model + `SampleStatus` literal. |
| `contracts/artifact.py` | `Artifact` — `sha256` (constrained to 64 hex chars), name, mime, size, path, tags. |
| `contracts/events.py` | `Event` + `EventKind` literal union (run.started, sample.*, tool.*, node.*, audit.*). |
| `contracts/tools.py` | `ToolCall` + `ToolResult` — the envelope every agent↔tool call uses. |
| `contracts/protocols.py` | `BrowserSession`, `TaskQueue`, `ArtifactStore`, `ChatModel`. `@runtime_checkable` so stub classes in tests can `isinstance` against them. |

#### `config/` — typed profile loader

| File | What it contains |
|---|---|
| `config/loader.py` | `Profile` + nested Pydantic models. `load_profile(path)` returns a fully-typed object. `Literal` unions on backend names reject typos at boot. |

#### `storage/` — persistence

| File | What it contains |
|---|---|
| `storage/schema.sql` | DDL: `runs`, `samples`, `artifacts`, `queue`, `audit_log`, `event_log`. WAL mode, `foreign_keys=ON`, appropriate indexes. |
| `storage/db.py` | `init_db()` (idempotent executescript) + `connect()` context manager with Row factory. |
| `storage/artifact_store.py` | `FilesystemArtifactStore`. `put(bytes, name, mime, **tags) → Artifact` writes to `blobs/<sha[:2]>/<sha><ext>` atomically (tmp+rename). `get(sha)` reads back. |
| `storage/audit_log.py` | `AuditLog` — one persistent `sqlite3.Connection` + `threading.Lock`. `append(kind, run_id, sample_id, payload) → this_hash`. Hash = `sha256(prev_hash + event_id + kind + ts + canonical(payload))`. `verify_chain()` walks + recomputes. |
| `storage/manifest.py` | `write_manifest()` scans `runs/<run_id>/blobs/*`, rehashes each file, writes `RUN_MANIFEST.json` with totals + artifacts + `audit_root_hash` + self-hash. `verify_manifest()` replays the integrity check. |

#### `queue/` — durable work distribution

| File | What it contains |
|---|---|
| `queue/sqlite_queue.py` | `SqliteQueue` implementing `TaskQueue` Protocol. `enqueue/dequeue/ack/nack/dead_letter/counts/reclaim_stale`. `dequeue` uses `BEGIN IMMEDIATE` + a `claim_token` column so two workers can't take the same row. `nack` increments attempts; after `max_attempts=3`, status flips to `dead` (DLQ). |

#### `browser/` — Playwright adapters + grounding

| File | What it contains |
|---|---|
| `browser/local.py` | `LocalPlaywrightSession`. Two factories: `create()` (owns its own browser) and `from_browser()` (shares a persistent Browser from the pool — hot path). Implements `BrowserSession` Protocol: `goto/click/type/screenshot/extract/snapshot/close` + `mark_and_screenshot/click_mark`. `click()` has strict role-match fallback — **no substring match that grabs `.first`** (accuracy bug from original). |
| `browser/pool.py` | `BrowserPool`. Launches Chromium **once** via `setup()`, holds it for the run, tears down via `teardown()`. `acquire()` opens a fresh context per sample (~5ms vs 300-800ms full browser). Semaphore enforces `concurrency` cap. |
| `browser/grounding.py` | `build_snapshot(page) → dict` — the LLM's eyes. Returns `{url, title, inner_text (6KB cap), outline (headings/landmarks), interactive ([{role, name, bbox, in_viewport}]), page_state ({scroll, viewport, ready_state, active, modal_open, modal_labels})}`. Walks shadow DOM + same-origin iframes. |
| `browser/set_of_mark.py` | Numbered-overlay injector. Draws colored boxes on visible clickable elements (walks shadow + iframes). Returns `dict[int, Mark]` with role + name + bbox. `click_mark(id)` resolves to a coordinate click — the big unlock for unknown UIs. |
| `browser/rate_limiter.py` | `HostRateLimiter` — per-hostname token bucket. Sleeps outside the internal lock so separate hosts don't serialize. Default `rps=2, burst=4`. |
| `browser/stealth.py` | Lightweight bot-detection evasion. Patches `navigator.webdriver/plugins/languages`, `window.chrome`, permissions, WebGL vendor. Tries `playwright-stealth` if installed; falls back to inline init script. UA + viewport randomization. |
| `browser/screencast.py` | `Screencaster` — CDP `Page.startScreencast`. JPEG frames published to `EventBus` keyed by `sample_id`. Off by default. |

#### `agent/` — the LangGraph state machine

| File | What it contains |
|---|---|
| `agent/state.py` | `AgentState` TypedDict. `tool_calls` + `evidence` use append reducers; `observations` is a plain list so `compact_observations()` can replace it in-place. **`kind=="extract"` entries are never compacted** (critical accuracy invariant). `OBSERVATION_WINDOW=5` caps snapshots. |
| `agent/classify.py` | `classify_task(prompt, schema, model) → TaskType`. Haiku. Returns one of `extract/form_fill/list_iter/navigate/unknown`. `classify_cache_key` for memoization. |
| `agent/prompts.py` | Role system prompts (PLANNER, NAVIGATOR, VERIFIER, EXTRACTOR, JUDGE) + user-message builders. `extractor_user` projects observations structurally (no mid-JSON slicing); `verifier_user` receives task + current step + rationale. |
| `agent/specialists/prompts.py` | Four specialist system prompts (extract/form_fill/list_iter/navigate) + a generic fallback. `system_prompt_for(task_type)` is the dispatch function. |
| `agent/plan_cache.py` | `PlanCache` on filesystem + in-memory layer. Key = `sha256(task_prompt + canonical(schema) + url_pattern)`. URL pattern normalizes `/issue/ENG-1` → `/issue/:id` so samples of the same task share a plan. |
| `agent/nodes.py` | All LangGraph nodes as factory functions returning closures that capture `AgentDeps`. Key decisions:<br/>• **classify** — memoized per `(prompt_hash, schema_hash)` in the closure<br/>• **plan** — checks plan cache first, uses specialist system prompt per task_type<br/>• **act** — dispatches to `BrowserTools`; tool errors bump `consecutive_fails` directly (don't rely on LLM verifier to notice)<br/>• **observe** — `build_snapshot` via browser, then `compact_observations`<br/>• **verify** — short-circuits on `last_tool_error`; garbled LLM output → `ok=False` (never silent-pass); reads task + current step + snapshot<br/>• **extract** — retries up to `EXTRACT_RETRY_MAX=2` with jsonschema errors + prior attempt + judge_feedback embedded<br/>• **judge** — on fail/uncertain, routes back to extract with `judge_feedback`, bounded by `reflect_count` |
| `agent/graph.py` | `build_graph(deps) → StateGraph`. Wiring: `START → classify → plan → act → (observe → verify → act/extract/plan/END)`, `extract → judge`, `judge → extract/END`. `run_sample` preserves legacy API; `invoke_compiled` is the hot path for pre-compiled graphs. |

#### `models/` — LiteLLM adapter

| File | What it contains |
|---|---|
| `models/roles.py` | `Role` enum: `PLANNER/NAVIGATOR/EXTRACTOR/JUDGE`. |
| `models/registry.py` | `get_model(role, profile) → ChatModel`. `lru_cache`'d per (provider, model, key). Env-var name per provider (`ANTHROPIC_API_KEY`, etc.). |
| `models/adapters/litellm_adapter.py` | `LiteLLMChatModel` — implements `ChatModel` Protocol via `litellm.acompletion`. `num_retries=3`, `timeout=60s`, `temperature` override, `response_format={json_schema: ...}` when a schema is passed. |

#### `tools/` — typed agent-facing tool wrappers

| File | What it contains |
|---|---|
| `tools/browser.py` | `BrowserTools(session)` + Pydantic arg models (`GotoArgs/ClickArgs/TypeArgs/ScreenshotArgs/ExtractArgs`). Every method returns a `ToolResult` envelope. |
| `tools/artifact.py` | `ArtifactTools(store)` + `PutArgs/GetArgs`. CRITICAL: `put()`'s audit-log representation substitutes `content: bytes` with `size: int` so raw file bytes never appear in the audit payload. |
| `tools/_runner.py` | `invoke(tool_name, args, fn)` — times, exceptions → normalized `ToolResult{status: "error"}`, uuid call_id. One source of truth for the envelope shape. |

#### `orchestrator/` — run coordination

| File | What it contains |
|---|---|
| `orchestrator/inputs.py` | `load_inputs(path) → list[dict]`. CSV (`csv.DictReader`), JSONL, JSON array, XLSX (optional `openpyxl`). |
| `orchestrator/runner.py` | `RunWorkflow.execute()`. Creates `BrowserPool`, `SqliteQueue`, `AuditLog`, `PlanCache`; saves `.run_config.json`; installs SIGTERM handler; calls `pool.setup()` (launches Chromium once); enqueues all rows; `asyncio.gather` N workers; each worker `dequeue → acquire session → build per-sample AgentDeps → run_sample → append to samples.jsonl → ack/nack`. On completion: rebuild `output.csv` from JSONL + write `RUN_MANIFEST.json` + call `pool.teardown()`. `resume(run_id)` hydrates completed set from JSONL, reclaims stale claims, skips enqueue, picks up remaining work. |

#### `credentials/` — sealed session state

| File | What it contains |
|---|---|
| `credentials/storage_state.py` | `SealedStateStore`. `seal/unseal` use AES-GCM with 12-byte nonce. `derive_key_from_env(ANDERA_MASTER_KEY)` uses HKDF-SHA256. `save/load/list_hosts/delete`. Host names sanitized so `../../etc/passwd` cannot escape the store root. |
| `credentials/login_flow.py` | `interactive_login(host, login_url)` — opens headed Chromium, waits for user to press ENTER, captures `context.storage_state()`, seals it. |

#### `observability/`

| File | What it contains |
|---|---|
| `observability/trace.py` | `JsonlTraceSink` — one JSONL file per day under `data/traces/`. Auto-stamped. `OSError` swallowed (never crash a run for telemetry). |
| `observability/langfuse_adapter.py` | `install_langfuse_if_enabled(profile)` — appends `"langfuse"` to `litellm.success_callback` + `failure_callback`. No-ops when disabled or when Langfuse SDK isn't installed. |

#### `planner/` — NL → task spec

| File | What it contains |
|---|---|
| `planner/planner.py` | `plan_task_from_nl(nl, planner_model, input_schema) → task_dict`. Strict validation: required fields present, `task_type` in the classifier's allowed set, `extract_schema.type="object"` with non-empty `required`. `ValueError` on any violation. |

#### `eval/` — accuracy gate

| File | What it contains |
|---|---|
| `eval/scorers.py` | `field_match` (case-insensitive fraction), `screenshot_exists`, `judge_pass`, `overall_score` (weighted: fields 0.6, evidence 0.15, judge 0.25). |
| `eval/framework.py` | `run_eval(eval_path, runner, pass_threshold)`. Reads a frozen `*.eval.json` with `{task_file, cases: [{sample_id, input, expected}]}`. Dispatches each case through the injected async runner. Catches runner exceptions → case scores 0 (no abort). Returns aggregate `{pass_rate, avg_total, avg_fields, details[]}`. |
| `eval/fixtures/*.eval.json` | Ground-truth files. 02-linear, 03-github-commits, 05-workday are shipped. |

#### `api/` — FastAPI app

| File | What it contains |
|---|---|
| `api/app.py` | `create_app()` + `app`. `lifespan` runs `init_db()`. CORS open (dev). Mounts routers: runs, samples, evidence, events, connections, screencast, plan, ui. |
| `api/ws.py` | `EventBus` — single-process pub/sub. `subscribe(run_id)` returns an `asyncio.Queue`; `publish(event)` fan-outs to run-scoped + global subscribers. Slow subscribers get evicted (drop oldest queue item rather than block writer). |
| `api/registry.py` | `RunRegistry` — in-memory record of runs kicked off via API. `list()` also hydrates completed runs from on-disk `RUN_MANIFEST.json`, so the dashboard survives process restarts. |
| `api/routes/runs.py` | `POST /api/runs` (kicks off RunWorkflow as `asyncio.create_task`). `GET /api/runs` + `GET /api/runs/{id}`. |
| `api/routes/samples.py` | `GET /api/runs/{id}/samples` + `GET /api/runs/{id}/samples/{sid}`. |
| `api/routes/evidence.py` | `GET /api/evidence/{sha256}` — scans `runs/*/blobs/<sha[:2]>/` for the blob. Validates sha format. Immutable cache-control header (content-addressed = safe forever). |
| `api/routes/events.py` | `WS /api/events?run_id=<id>` — JSON event stream. Initial `ws.ready` frame, 30s ping, scoped by run_id. |
| `api/routes/screencast.py` | `WS /api/screencast?sample_id=<id>` — base64 JPEG frame relay. Filters bus events to matching sample. |
| `api/routes/connections.py` | `GET /api/connections` — `SealedStateStore.list_hosts()`. |
| `api/routes/plan.py` | `POST /api/plan {nl, input_schema?}` — NL → task spec via planner. |
| `api/routes/ui.py` | HTML routes at `/ui/*`. Reuses the JSON routes internally (form POST delegates to `create_run`). |
| `api/templates/*.html` | Jinja templates. `base.html` (dark monospace chrome), `runs.html` + `runs_fragment.html` (HTMX polling), `new_run.html`, `run_detail.html` (live event stream + sample grid), `samples_fragment.html`, `sample_detail.html` (evidence carousel + extracted JSON + live browser panel), `connections.html`. |

#### `cli.py` — entry point

`andera run / resume / login / verify / check`. Registered as a
console_scripts entry in `pyproject.toml`.

### 3.3 `services/mock_workday/`

Standalone FastAPI mock. Runs on `:8001` so it doesn't clash with the
main app on `:8000`. Endpoints: `/directory`, `/directory/search`,
`/people/{id}`, `/forms/new`, `POST /forms/submit` (writes an
attachment, returns a confirmation page linking to it), `/attachments/{name}`.
`scripts/seed_mock_workday.py` generates 100 deterministic fake
employees.

### 3.4 `tests/`

```
tests/
├── unit/
│   ├── contracts/             — type + Protocol tests
│   ├── storage/               — artifact store, schema init, audit log, manifest
│   ├── queue/                 — FIFO, claim-lease under concurrency, DLQ
│   ├── browser/               — grounding, SoM, rate limiter, stealth, pool,
│   │                            click safety, screencast
│   ├── agent/                 — nodes, state compaction, plan cache,
│   │                            specialists, extractor retry + judge feedback,
│   │                            garbled-verifier regression
│   ├── models/                — LiteLLM registry (mocked)
│   ├── tools/                 — tool envelope + byte-safety
│   ├── orchestrator/          — inputs, runner fan-out, resume
│   ├── credentials/           — sealed roundtrip, path-traversal safety
│   ├── observability/         — JSONL sink, langfuse installer
│   ├── planner/               — NL → task spec validation
│   ├── api/                   — routes + WebSocket + UI renders
│   ├── services/              — mock Workday
│   └── tasks/                 — every shipped task YAML parses + has fixture
└── eval/
    └── test_eval_harness.py   — scorers + gate + scripted runner
```

**~200 tests total. Full suite runs in ~20s.**

### 3.5 `scripts/`

| File | What it does |
|---|---|
| `scripts/smoke_browser.py` | Phase 1 acceptance — drives Chromium to example.com, screenshots, prints sha256. |
| `scripts/run_one_sample.py` | Phase 2 acceptance — runs the full graph on one URL end-to-end. |
| `scripts/seed_mock_workday.py` | Deterministic 100-employee seed for the mock service. |

### 3.6 Runtime directories (gitignored)

| Path | What lands here |
|---|---|
| `data/state.db` | Baseline DB (schema only). |
| `data/<run_id>.queue.db` | Per-run work queue. |
| `data/<run_id>.audit.db` | Per-run hash-chained audit log. |
| `data/<run_id>.ckpt.db` | LangGraph checkpoints per sample. |
| `data/plan_cache/<sha>.json` | Cached plans (cross-run). |
| `data/credentials/<host>.sealed` | AES-GCM sealed `storage_state`. |
| `data/traces/<YYYY-MM-DD>.jsonl` | Always-on local telemetry. |
| `runs/<run_id>/blobs/<sha[:2]>/<sha><ext>` | Content-addressed evidence. |
| `runs/<run_id>/samples.jsonl` | Per-sample durable record. |
| `runs/<run_id>/output.csv` | Aggregate export (rebuilt from JSONL). |
| `runs/<run_id>/RUN_MANIFEST.json` | Tamper-evident summary. |
| `runs/<run_id>/.run_config.json` | Minimal state to resume. |

---

## 4. End-to-end data flow

### 4.1 One row of a CSV, traced through the system

```mermaid
sequenceDiagram
    autonumber
    participant CLI as andera run
    participant WF as RunWorkflow
    participant Q as SqliteQueue
    participant W as Worker (asyncio)
    participant Pool as BrowserPool
    participant S as Chromium session
    participant G as LangGraph
    participant LLM as Claude (LiteLLM)
    participant Store as Artifact store
    participant Audit as Audit log
    participant JSONL as samples.jsonl

    CLI->>WF: execute()
    WF->>WF: save .run_config.json
    WF->>Pool: setup() - launch Chromium ONCE
    WF->>Audit: append run.started
    loop for each input row
        WF->>Q: enqueue({sample_id, row, start_url})
    end
    par N workers in parallel
        W->>Q: dequeue (BEGIN IMMEDIATE + claim_token)
        Q-->>W: job
        W->>Audit: append sample.started
        W->>Pool: acquire()
        Pool->>S: new context + page (cheap, ~5ms)
        Pool-->>W: session
        W->>G: run_sample(state, deps)

        G->>LLM: classify (Haiku, memoized per task)
        LLM-->>G: task_type
        G->>G: plan_cache.get(key)
        alt cache miss
            G->>LLM: plan (Opus, specialist prompt)
            LLM-->>G: plan
            G->>G: plan_cache.put(key, plan)
        end
        loop each step
            G->>S: goto / click / type / screenshot
            S->>Store: screenshot bytes -> sha256 file
            Store-->>S: Artifact
            G->>S: snapshot()
            S-->>G: rich state (text + outline + interactive + page_state)
            G->>LLM: verify (Sonnet; receives task + step + snapshot)
            LLM-->>G: {ok, reason}
        end
        G->>LLM: extract (Haiku; structured output via JSON schema)
        LLM-->>G: parsed fields
        Note over G: retry w/ validation errors if schema-invalid
        G->>LLM: judge (Opus)
        LLM-->>G: {verdict, reason}
        Note over G: if fail/uncertain + budget, route back to extract w/ feedback

        G-->>W: final state
        W->>Audit: append sample.completed / sample.failed
        W->>JSONL: append one line
        W->>Q: ack(item_id)   alt fail -> nack, retry up to 3x, then dead
    end
    WF->>Store: scan blobs, re-hash each
    WF->>Audit: append run.completed
    WF->>WF: rebuild output.csv from samples.jsonl
    WF->>WF: write RUN_MANIFEST.json (artifacts + audit_root + manifest_hash)
    WF->>Pool: teardown()
    WF-->>CLI: RunResult
```

### 4.2 Per-sample state transitions (LangGraph)

```
START
  │
  ▼
classify ── (cached? pass-through, else Haiku)
  │
  ▼
plan ────── (cache hit? reuse, else Opus w/ specialist prompt)
  │
  ▼
act ─────── action ∈ {goto, click, type, screenshot, extract, done}
  │           │
  │           └──▶ status="extracting" when action=done
  │
  ├───▶ if status=="extracting" → extract
  ├───▶ if status=="failed" → END
  │
  ▼
observe ──── rich snapshot (text + outline + interactive + page_state)
  │           + compact_observations (extracts preserved, snapshots windowed)
  │
  ▼
verify ──── (short-circuit to ok=false if last_tool_error)
  │          (LLM with task + step + snapshot)
  │          (garbled output = ok=false; NEVER silent pass)
  │
  ├───▶ ok                     → step_index++ → act
  ├───▶ reflect_count < MAX    → act
  ├───▶ consecutive_fails >= 2 → plan (replan)
  ├───▶ reflect_count >= MAX   → END(failed)
  │
  ▼ (step_index past end of plan)
extract ─── (Haiku w/ projected observations + prior attempt + judge feedback)
  │          (jsonschema validation + up to 2 retries)
  │
  ├───▶ failed (unparseable) → END
  │
  ▼
judge ───── (Opus)
  │
  ├───▶ verdict="pass"                    → END(done)
  └───▶ verdict in {fail, uncertain}
        ├── reflect_count < MAX → extract (with judge_feedback)
        └── else                 → END
```

### 4.3 Lifecycles at a glance

| Lifecycle | Scope | Lives |
|---|---|---|
| Playwright process | Per-run | `pool.setup()` at execute start → `pool.teardown()` at end |
| Chromium browser | Per-run | Same as above |
| BrowserContext + Page | Per-sample | `pool.acquire()` yields it → closed in `finally` |
| LangGraph compile | Per-sample (today) | Built in `run_sample`; saver context inside |
| AgentDeps | Per-sample | Contains BrowserTools bound to the per-sample session |
| PlanCache | Per-run | In-memory + filesystem; survives across runs on disk |
| ClassifyMemo | Per-run | Closure dict inside `make_classify_node` |
| AuditLog connection | Per-run | Persistent sqlite3.Connection + threading.Lock |
| SqliteQueue | Per-run | One DB file per run_id |

---

## 5. Concurrency model

- **1** Python process, **1** asyncio event loop.
- **N** worker coroutines launched via `asyncio.gather` where N = `profile.browser.concurrency`.
- **1** shared Chromium subprocess.
- **N** concurrent `BrowserContext` + `Page` instances (one per active worker).
- **Semaphore** on the BrowserPool enforces the N cap even if more coroutines try to acquire.
- **Per-host token bucket** throttles `goto()` to `per_host_rps` regardless of concurrency.

### Serialization points

| Resource | How it's serialized |
|---|---|
| SQLite queue writes | In-process `asyncio.Lock` + SQLite file lock (WAL) |
| Audit log writes | `threading.Lock` on a persistent connection (hash chain integrity) |
| `samples.jsonl` appends | `asyncio.Lock` (`self._results_lock`) inside `_record_result` |
| Plan cache puts | Atomic `tmp+rename`; single writer effectively |

### Backpressure

- **Target site**: `HostRateLimiter` throttles at the `goto` level.
- **LLM**: LiteLLM's `num_retries=3` handles 429s; `timeout=60s` fails fast.
- **Agent crashes mid-sample**: queue `nack` + retry up to 3×, then DLQ.
- **Process crash**: stale claims reclaimed on `andera resume`; queue state + `samples.jsonl` recover progress.

---

## 6. Configuration surface

Everything swappable flows through **`config/profile.yaml`**:

```yaml
models:
  planner:   { provider: anthropic, model: claude-opus-4-7 }
  navigator: { provider: anthropic, model: claude-sonnet-4-6 }
  extractor: { provider: anthropic, model: claude-haiku-4-5-20251001 }
  judge:     { provider: anthropic, model: claude-opus-4-7 }
browser:
  backend: local              # local | browserbase (swap impl via Protocol)
  headless: false
  concurrency: 4
  stealth: true
  screencast: false
  per_host_rps: 2.0
  per_host_burst: 4
queue:
  backend: sqlite             # sqlite | redis | nats (future)
  path: ./data/queue.db
storage:
  artifacts: { backend: filesystem, root: ./runs }  # filesystem | s3
  metadata:  { backend: sqlite, path: ./data/state.db }
integrations:
  github:  { mode: auto, token_env: GITHUB_TOKEN }
  linear:  { mode: auto, token_env: LINEAR_TOKEN }
observability:
  langfuse:
    enabled: false
    host: http://localhost:3000
eval:
  gate_accuracy: 0.9
```

Tasks can tighten via `profile_overrides` in their YAML — e.g., LinkedIn
forces `concurrency: 1 / stealth: true / per_host_rps: 0.5`.

---

## 7. Security + integrity

| Concern | Mitigation | Where |
|---|---|---|
| Secrets in code | `.env` gitignored; env var names referenced, values never | `config/loader.py`, `models/registry.py` |
| Evidence tamper | Content-addressing: filename = sha256(bytes); any swap changes the hash | `storage/artifact_store.py` |
| Audit tamper | Hash-chained rows: prev_hash → this_hash; `verify_chain()` | `storage/audit_log.py` |
| Manifest tamper | `manifest_hash = sha256(canonical_json_minus_itself)`; artifacts rehashed on verify | `storage/manifest.py` |
| Sealed creds | AES-GCM + HKDF(SHA256) from `ANDERA_MASTER_KEY` | `credentials/storage_state.py` |
| Path traversal | Host names sanitized; resolved paths checked inside store root | same |
| Byte leakage in logs | Tool-call envelope substitutes raw bytes with size before audit payload | `tools/artifact.py` |
| SQL injection | Parameterized queries everywhere; no string interp into SQL | `queue/sqlite_queue.py`, `storage/audit_log.py` |

---

## 8. Running it

```bash
# dev
uv sync --extra dev
uv run playwright install chromium
cp env.example .env                    # fill ANTHROPIC_API_KEY

# CLI
uv run andera check                    # profile + db + live browser
uv run andera run -t config/tasks/02-linear-tickets.yaml -i tests/fixtures/02-linear-tickets.csv
uv run andera resume <run_id>          # after Ctrl-C / crash
uv run andera verify runs/<run_id>     # re-hash + audit-chain verification
uv run andera login github --url https://github.com/login

# dashboard (same process; JSON + HTMX + WS)
uv run uvicorn andera.api.app:app --port 8000 &
open http://localhost:8000

# mock Workday (for tasks 1 + 5)
uv run python services/mock_workday/app.py &

# optional Langfuse (docker)
docker compose -f docker/compose.yaml up -d

# tests
uv run pytest -q                       # 198 pass in ~20s
uv run pytest tests/eval -q            # rubric gate
```

---

## 9. The production-swap map

Nothing in `src/` depends on any implementation; everything flows
through Protocols. Swap candidates (all documented in
`ARCHITECTURE.md`):

| Concern | Today | Swap candidate | Interface |
|---|---|---|---|
| Queue | `SqliteQueue` | `RedisQueue` / SQS / NATS | `TaskQueue` in `contracts/protocols.py` |
| Artifacts | `FilesystemArtifactStore` | `S3ArtifactStore` | `ArtifactStore` |
| Browser | `LocalPlaywrightSession` | `BrowserbaseSession` | `BrowserSession` |
| Models | `LiteLLMChatModel` → Anthropic | any LiteLLM-supported provider or a direct adapter | `ChatModel` |
| Metadata | SQLite | Postgres | Same SQL — no SQLite-specific features used |
| Traces | JSONL + Langfuse | OpenTelemetry / Honeycomb / Datadog | New callback in LiteLLM |
| Workers | in-process asyncio | Celery / Arq / Temporal | `orchestrator/_worker` lifted out to a separate entry point |

None of these require changes to `agent/`, `orchestrator/`, or `api/`.

---

## 10. Where to start reading

If you're landing on this codebase for the first time:

1. `PLAN.md` — why phases are ordered the way they are
2. `contracts/protocols.py` — the shape of every external dependency
3. `agent/graph.py` — how the state machine is wired
4. `agent/nodes.py` — what each node actually does
5. `orchestrator/runner.py` — how runs are orchestrated end-to-end
6. `browser/grounding.py` — what the LLM "sees"
7. `config/profile.yaml` — where every runtime dial lives

That sequence walks you from the abstractions down to the
runtime-observable behavior in under an hour.
