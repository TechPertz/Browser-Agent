# Andera Work Trial — General Browser Agent

**Target:** Andera (andera.ai) work trial
**Timeline:** ~1 day build + demo day
**Runtime:** local laptop, Docker allowed, no cloud deps required
**Primary UX:** web dashboard (localhost:8000). CLI is dev-only.
**Source of truth:** this file. Claude Code executes phase-by-phase from Part 4.

---

# Part 1 — What we're building

A **General Browser Agent** that takes a natural-language task + input file (CSV/Excel/JSONL) and produces **audit-grade evidence**: one folder per sample with screenshots + downloads, plus an aggregate CSV. System-agnostic — works on Linear, GitHub, Workday, Jira, LinkedIn, anything in a browser.

Two execution modes (from spec):
- **Manual UI mode** — agent drives a real browser.
- **Integration-assisted mode** — when API creds exist, use fast path; emit the same evidence.

**Rubric (priority order):** Accuracy · Generality · Scalability · Consistency · Speed.

---

# Part 2 — Architecture

```
┌───────────────────── config/profile.yaml (the switch panel) ────────────────────┐
│  models per role · browser backend · queue · storage · integrations · obs       │
└─────────────────────────────────────────────────────────────────────────────────┘

 Web Dashboard (primary UX)                                CLI (dev-only)
 FastAPI + HTMX + WebSocket                                andera run <task> --input <f>
 localhost:8000                                            andera login <host>
           │                                                          │
           └────────────┬─────────────────────────────────────────────┘
                        │
                ┌───────▼────────┐
                │    Planner     │  NL + schema → RunSpec
                │   (Opus 4.7)   │
                └───────┬────────┘
                        │
                ┌───────▼──────────┐      ┌──────────────────┐
                │  Orchestrator    │─────▶│  Task Queue      │
                │  (state machine, │      │  (SQLite WAL,    │
                │   checkpointed)  │      │   swap: Redis)   │
                └───────┬──────────┘      └──────────────────┘
                        │ fan-out N samples  (bounded asyncio semaphore)
         ┌──────────────┼──────────────┐
         │              │              │
    ┌────▼────┐    ┌────▼────┐    ┌────▼────┐
    │ Sample  │    │ Sample  │... │ Sample  │    LangGraph per sample:
    │Workflow │    │Workflow │    │Workflow │    PLAN→NAV→OBS→ACT→VERIFY
    └────┬────┘    └────┬────┘    └────┬────┘         →EXTRACT→PERSIST
         └──────────────┼──────────────┘                →JUDGE→DONE
                        │                            (checkpointed at every node)
          ┌─────────────▼──────────────┐
          │   Tool Plane (typed)       │
          │  browser.*  artifact.*     │
          │  evidence.* integration.*  │
          └─────────────┬──────────────┘
                        │
          ┌─────────────▼──────────────┐
          │   BrowserSession pool      │
          │  Local Playwright + Stage- │
          │  hand (swap: Browserbase)  │
          └────────────────────────────┘

 DATA PLANE:  runs/<run_id>/samples/<sample_id>/...   (filesystem, per spec)
              SQLite: runs, samples, artifacts, audit_log (hash-chained), FTS5

 OBS PLANE:   Langfuse self-hosted (docker) — every LLM call logged
              OpenTelemetry spans + JSONL traces
              Pytest eval harness on the 5 example tasks — 90% accuracy gate
```

**Per-sample agent state machine (LangGraph, SqliteSaver):**

```
  PLAN → NAVIGATE → OBSERVE → ACT → VERIFY ─┬─ pass → EXTRACT
                      ▲                     │
                      │                    fail
                      │                     │
                      └── REFLECT ◄─────────┘  (bounded N=3)

  EXTRACT → PERSIST → JUDGE ─┬─ pass → DONE
                              │
                             fail → re-queue with judge's notes
```

**Agent roles and model routing (configurable):**

| Role | Default | Why |
|---|---|---|
| Planner | Opus 4.7 | Heavy reasoning once per run |
| Navigator | Sonnet 4.6 | Many cheap action decisions |
| Extractor | Haiku 4.5 | Pure pattern completion, cheapest |
| Judge | Opus 4.7 | Independent verification |

All routed via `models.get(role) → ChatModel`, reading `profile.yaml`. LiteLLM normalizes providers (Anthropic, OpenAI, Gemini, Ollama).

---

# Part 3 — UX (the real one: web app, not CLI)

The spec says *"product interface and authentication system is up for you to spec out."* The user doesn't run terminal commands. **The dashboard IS the product.** CLI exists only for us during development.

## User journey

1. **Connections page** — click "Connect Linear" → popup opens a real Chrome, user logs in once, we capture & seal `storage_state`. Same for GitHub, Jira, Workday.
2. **New Run** — pick a template OR type the task in English. Upload input file.
3. **Review plan** — Planner LLM drafts a RunSpec. Shown as an editable flowchart. User confirms.
4. **Live run view** (the "wow" screen):
   - Sample grid showing queued/running/done/failed
   - **Live browser stream** on the right (Playwright CDP screencast → WebSocket → `<img>`, 10–15 FPS)
   - Agent's current action shown as text overlay ("clicking title…")
5. **Sample detail** — screenshot carousel, action trace, extracted fields, deep link to Langfuse for every LLM call.
6. **Run complete** — "Download evidence" (zip of folder tree) + "Download CSV".
7. **Settings** — per-system "Use API when available" toggle (integration mode). Model picker per role.

## Why web app, not Chrome extension

Extension is tempting (reuses user's login tab) but doesn't parallelize across 1000 samples and can't retain evidence server-side. An audit product needs server-side parallelism + persistent evidence. Web app wins. (Could add an extension v2 later as a "one-off assistant.")

---

# Part 4 — Phase-by-phase build plan (Claude Code executes this)

Each phase is a vertical slice with a **concrete acceptance test**. Don't move to the next phase until the current one passes its test. Each phase is sized to one focused CC session (~1–3 hours). Phases are roughly sequential but flagged where parallelization is safe.

## Phase -1 — Prerequisites (human does this, 10 min)

- Install: `uv` (Python), `docker`, Node (for dashboard templates if needed)
- Write `.env` with: `ANTHROPIC_API_KEY=...` (required); `GITHUB_TOKEN` + `LINEAR_TOKEN` optional
- Run `playwright install chromium`
- **Initialize git**: `git init && git add PLAN.md memory/ && git commit -m "chore: seed plan and memory"` — this becomes the baseline. Every phase ends with its own commit (see Part 6.5 Commit discipline).

---

## Phase 0 — Bootstrap (~1h)

**Goal:** repo scaffold + contracts + config loader. No business logic yet.

**Deliverables:**
- `pyproject.toml` (uv-managed, deps: pydantic, pyyaml, playwright, stagehand-py, litellm, langgraph, langgraph-checkpoint-sqlite, fastapi, uvicorn, httpx, sqlite-utils, jinja2, python-dotenv, cryptography, pytest, pytest-asyncio, opentelemetry-api, rich, typer)
- Repo tree exactly as in Part 5
- `src/andera/contracts/`: `runspec.py`, `sample.py`, `artifact.py`, `events.py`, `protocols.py` — all Pydantic types + Protocols filled in
- `src/andera/config/loader.py` — loads `config/profile.yaml` into a typed `Profile` model
- `config/profile.yaml` starter file
- `src/andera/storage/schema.sql` — SQLite DDL for runs, samples, artifacts, audit_log, queue, event_log
- `src/andera/storage/db.py` — connection factory, migration runner
- `.env.example`, `.gitignore`, `README.md` stub

**Acceptance test:**
```bash
uv sync
uv run pytest tests/unit/contracts -q           # passes
uv run python -c "from andera.config import load_profile; print(load_profile())"
uv run python -c "from andera.storage.db import init_db; init_db()"
ls data/state.db                                 # exists
```

---

## Phase 1 — Core ports: models + browser + storage (~2h)

**Goal:** the three ports that everything else depends on, each with one working implementation.

**Deliverables:**
- `src/andera/models/`: `registry.py` (LiteLLM-backed), `roles.py` enum, `adapters/anthropic.py`. Function: `get_model(role: Role, profile: Profile) -> ChatModel`.
- `src/andera/browser/`: `session.py` (Protocol), `local.py` (LocalPlaywrightSession with Stagehand), `pool.py` (bounded session pool with acquire/release). Session provides: `goto`, `click`, `type`, `select`, `screenshot`, `extract`, `snapshot` (DOM + a11y + annotated SoM image), `download`, `close`.
- `src/andera/storage/artifact_store.py`: Protocol + `FilesystemArtifactStore` (content-addressed by sha256, path layout per spec).
- `src/andera/tools/browser.py` — wraps BrowserSession into agent-facing tools with Pydantic I/O.
- `src/andera/tools/artifact.py` — put/get typed wrappers.

**Acceptance test:**
```bash
uv run python scripts/smoke_browser.py
# navigates to https://example.com, takes screenshot, saves to runs/smoke/sample-1/step_00_home.png
# prints artifact sha256 and verifies file exists under content-addressed path
```

**Smart cuts:** if Stagehand install is painful, fall back to raw Playwright + our own DOM-snapshot; Stagehand is an optimization, not a blocker.

---

## Phase 2 — Agent state graph (~3h — the heart)

**Goal:** one Linear ticket, end-to-end, agent-driven.

**Deliverables:**
- `src/andera/agent/state.py` — `AgentState` TypedDict
- `src/andera/agent/grounding.py` — builds multi-modal observation (DOM summary + a11y tree + Set-of-Mark screenshot)
- `src/andera/agent/nodes/`: `plan.py`, `navigate.py`, `observe.py`, `act.py`, `verify.py`, `reflect.py`, `extract.py`, `persist.py`, `judge.py` — each a pure function `(state) -> state_update`
- `src/andera/agent/graph.py` — assembles LangGraph with SqliteSaver, wires conditional edges (verify → reflect → act loop bounded N=3; judge → done or requeue)
- `src/andera/agent/prompts/` — one file per role
- `config/tasks/02-linear-screenshots.yaml` — handcrafted RunSpec for Linear ticket screenshot+extract
- `scripts/run_one_sample.py` — runs the graph on one sample dict

**Acceptance test:**
```bash
uv run python scripts/run_one_sample.py \
  --task config/tasks/02-linear-screenshots.yaml \
  --url "https://linear.app/<demo>/issue/ENG-1"
# produces:
#   runs/<run_id>/samples/<sample_id>/step_00_nav.png
#   runs/<run_id>/samples/<sample_id>/step_01_ticket.png
#   runs/<run_id>/samples/<sample_id>/result.json  with ticket_no, assignee, due_date
# Judge status: pass
```

---

## Phase 2.5 — Grounding + scalability hardening (~4h)

**Goal:** generic agent that works on unseen systems (Workday, LinkedIn, NetSuite) without task-specific tuning, AND scales to thousands of samples without cost/latency blowup.

Driven by `/plan-eng-review` findings on the original Phase 2.5 proposal — three scalability gaps were missed (plan cache, state compaction, rate limits) and two generality gaps needed sharpening (shadow-DOM/iframe aware SoM, re-callable plan).

**Deliverables:**

1. **Rich snapshot** — `browser/grounding.py::build_snapshot(page)` returns:
   - `url`, `title`, `inner_text` (truncated to 6KB, body-only)
   - `a11y` — accessibility tree via `page.accessibility.snapshot()`
   - `interactive` — list of `{role, name, bbox, mark_id}` for visible clickables, buttons, links, inputs, selects

2. **Set-of-Mark overlay** — `browser/set_of_mark.py`:
   - JS-injected script walks DOM + `shadowRoot` + same-origin iframes
   - Draws numbered colored boxes over interactive elements
   - Returns `(annotated_screenshot_bytes, marks: dict[int, Mark])`
   - Adds `BrowserSession.mark_and_screenshot()` + `click_mark(id)` methods
   - Extract semantic info (role + accessible name) per mark for the navigator
   - Cross-origin iframes logged as `unreachable` (coordinate-only fallback)

3. **Plan cache** — `agent/plan_cache.py`:
   - Key = `sha256(task_prompt + json(schema) + normalized_url_pattern)`
   - Storage: `data/plan_cache/{key}.json`
   - Hit: skip planner LLM call, reuse plan as template
   - Miss: run planner, write result
   - Cuts 1000-sample runs from 1000 planner calls → 1

4. **State compaction** — `agent/state.py::compact_observations`:
   - Cap `observations` at last N=5 full entries
   - Older entries summarized by Haiku into single-line abstracts
   - Prevents context-window overflow on long flows (20+ step tasks)

5. **Re-callable plan node** — `agent/graph.py`:
   - Planner becomes first-class re-entrant: verifier can route to `plan` when >= 2 consecutive verify failures on same step
   - Planner sees prior plan + failure context; can revise tail of plan

6. **LiteLLM retry + backoff** — `models/adapters/litellm_adapter.py`:
   - Wire `num_retries=3`, `request_timeout=60s`, exponential backoff
   - Gracefully handle 429s at concurrency=20+

**Acceptance test:**
```bash
uv run pytest tests/unit/browser/test_grounding.py -q      # rich snapshot
uv run pytest tests/unit/browser/test_set_of_mark.py -q    # SoM incl. shadow-DOM
uv run pytest tests/unit/agent/test_plan_cache.py -q       # cache hit/miss
uv run pytest tests/unit/agent/test_state_compaction.py -q # observation cap
uv run python scripts/smoke_mark.py                        # real page with marks
```

---

## Phase 2.75 — Specialist subagents (~3h)

**Goal:** literal "many subagents" per Andera spec. Task-type classifier dispatches to a specialist planner per task shape. Addresses the /plan-eng-review Finding 1C.

**Deliverables:**

1. **Task classifier** — `agent/classify.py::classify_task(task_prompt, schema) -> TaskType`
   - Uses Haiku (cheap): returns one of `extract | form_fill | list_iter | navigate | unknown`
   - Result cached per `(task_prompt_hash, schema_hash)` so 1000 samples classify once

2. **Specialist planners** — `agent/specialists/`
   - `extract_planner.py` — for simple 1-page extractions (GitHub issue, public blog)
   - `form_fill_planner.py` — for flows that fill a form + submit + download (Workday apply)
   - `list_iter_planner.py` — for pagination/iteration over a list of items (60 commits, N users)
   - `generic_planner.py` — fallback when classifier returns `unknown`
   - Each specialist is a `(ChatModel, system_prompt, plan_template)` triple. Same graph runtime, specialized prompts.

3. **Dispatch wiring** — `agent/graph.py`:
   - New `classify` node runs once at graph start
   - `route_after_classify` picks which specialist planner to call
   - State gains `task_type: TaskType` field

4. **Specialist prompts** — `agent/prompts.py` additions:
   - `EXTRACT_SPECIALIST_SYSTEM` — focus on navigating to the target page + screenshotting evidence + extraction
   - `FORM_FILL_SPECIALIST_SYSTEM` — focus on field-by-field fill, verify each entry, submit, capture confirmation
   - `LIST_ITER_SPECIALIST_SYSTEM` — focus on iterating a list, extracting per item, paginating until done

**Acceptance test:**
```bash
uv run pytest tests/unit/agent/test_specialists.py -q
# - classifier routes "extract title from GitHub issue" -> extract_planner
# - classifier routes "fill Workday form and submit" -> form_fill_planner
# - classifier routes "iterate 60 commits and screenshot each" -> list_iter_planner
```

---

## Phase 3 — Orchestration + queue (~1.5h)

**Goal:** Phase 2, but at 20–50 samples in parallel with crash recovery.

**Deliverables:**
- `src/andera/queue/queue.py` Protocol + `sqlite_queue.py` (enqueue, dequeue-w-lease, ack, nack, dead_letter, scan)
- `src/andera/orchestrator/runner.py` — `RunWorkflow`: load inputs → materialize Samples → enqueue → dispatch with `asyncio.Semaphore(N)` → collect results → finalize
- `src/andera/orchestrator/checkpoint.py` — per-sample state in SQLite; resume picks up at last completed node
- Retry policy: on agent failure, requeue up to 2x; then DLQ
- CLI entry: `andera run <task.yaml> --input <csv>`

**Acceptance test:**
```bash
uv run andera run config/tasks/02-linear-screenshots.yaml \
  --input tests/fixtures/linear_20_tickets.csv
# 20 folders under runs/<run_id>/samples/, all judged pass
# output.csv has 20 rows with all 3 fields populated
# kill -9 mid-run, rerun same command → resumes from where it stopped
```

---

## Phase 4 — Persistence: audit log + sealed creds (~1h)

**Goal:** make the evidence audit-grade and the auth flow reusable.

**Deliverables:**
- `src/andera/storage/audit_log.py` — append-only hash-chained rows; `write(row) -> new_hash`; `verify_chain() -> bool`
- `src/andera/storage/manifest.py` — generates `RUN_MANIFEST.json` with run metadata + root hash + per-sample summary
- `src/andera/credentials/storage_state.py` — AES-GCM encrypted `storage_state` per host, keyed from `ANDERA_MASTER_KEY` env
- `src/andera/credentials/login_flow.py` — opens headed Playwright, waits for user-initiated navigation to complete, saves sealed state
- CLI: `andera login <host>` runs the login flow

**Acceptance test:**
```bash
uv run andera login github    # opens browser, user logs in, state sealed
uv run python scripts/verify_manifest.py runs/<run_id>
# prints: chain valid ✓, artifacts: N, root_hash: <sha>
```

---

## Phase 5a — Web API backbone (~1.5h)

**Goal:** HTTP endpoints that expose everything the CLI does.

**Deliverables:**
- `src/andera/api/app.py` — FastAPI app with CORS, lifespan (init DB, warm pool)
- `src/andera/api/routes/`: `runs.py` (POST create, GET list, GET detail), `samples.py` (GET list by run, GET detail, POST retry), `evidence.py` (serve artifacts by sha), `events.py` (WebSocket subscriber), `connections.py` (list hosts + status)
- `src/andera/api/ws.py` — in-process pub/sub for run events, surfaced over WebSocket
- Orchestrator publishes to the same pub/sub so UI sees live progress

**Acceptance test:**
```bash
uv run uvicorn andera.api.app:app --reload
curl -X POST localhost:8000/api/runs -d '{"task":"02-linear-screenshots","input_file":"..."}'
curl localhost:8000/api/runs/<id>/samples
# wscat -c ws://localhost:8000/api/events?run_id=<id> → streams sample.* events
```

---

## Phase 5b — Live dashboard + CDP screencast (~2h, the "wow")

**Goal:** the screen the trial reviewer will remember.

**Deliverables:**
- `src/andera/api/templates/`: `base.html`, `runs.html`, `run_detail.html`, `sample_detail.html`, `connections.html`, `new_run.html`
- HTMX-driven: sample grid auto-updates via `hx-sse` or WebSocket
- `src/andera/browser/screencast.py` — hooks Playwright's `page.on("screencast")` OR `Page.startScreencast` via CDP; emits JPEG frames
- `src/andera/api/routes/screencast.py` — WebSocket route that multiplexes frames for the currently-selected sample to connected clients
- Live view renders `<img>` swapped with base64 JPEGs at ~10 FPS
- "Review plan" step — Planner's RunSpec rendered as an editable flowchart (simple: numbered steps list with inline editors)
- Connections page: list of known hosts, "Connect" button opens the sealed-login flow via a backend-initiated headed browser

**Acceptance test:** open `http://localhost:8000`, click New Run, upload CSV, watch sample grid fill in, click a running sample → see live Chrome stream on the right, click a done sample → carousel + extracted JSON + link to Langfuse trace.

---

## Phase 5c — Politeness + stealth (~1.5h)

**Goal:** agent behaves like a human at scale — doesn't hammer target
hosts, doesn't get bot-flagged, honors rate limits when sites push back.
Prerequisite for LinkedIn / Workday / any bot-sensitive site.

**Deliverables:**
- `src/andera/browser/rate_limiter.py` — per-host token-bucket + queue.
  `browser.per_host_rps` + `browser.per_host_burst` in profile.yaml.
- `src/andera/browser/stealth.py` — applies `playwright-stealth` or an
  inlined equivalent (removes navigator.webdriver, patches plugins/
  languages/permissions) before each `new_context()`. Gated on
  `profile.browser.stealth=true`.
- User-agent + viewport randomization per context.
- Target-site 429/503 backoff: browser tool calls detect HTTP status
  and emit a `wait_for` with exponential backoff + jitter before retry.
- Optional `robots.txt` check in a "respect_robots" mode (off by default
  for authorized testing; on for public scraping).

**Acceptance test:**
```bash
uv run pytest tests/unit/browser/test_rate_limiter.py -q
# - 3 workers, per_host_rps=2 -> observed request rate per host <= 2/sec
# - separate hosts do not interfere
# - 429 response triggers exponential backoff with jitter
```

---

## Phase 5d — Fault tolerance + scale hardening (~1h)

**Goal:** a run that crashes at sample 700/1000 resumes at 700, not 0.
Memory footprint doesn't grow linearly with sample count.

**Deliverables:**
- `andera resume <run_id>` — reads the run's checkpoint DB + queue,
  reclaims stale claims, re-spawns workers against the remaining
  pending rows. Preserves the existing run_id and appends to the
  same audit log.
- Startup hook: orchestrator calls `SqliteQueue.reclaim_stale()` on
  boot so any run started before a crash is recoverable.
- Graceful SIGTERM: orchestrator catches signal, stops accepting new
  jobs, lets in-flight samples finish (up to a 60s grace), then exits.
- Streaming aggregate CSV: `output.csv` is flushed per-sample rather
  than written once at the end, so a Ctrl-C still leaves a usable CSV.
- Memory-bounded results: completed samples are persisted and evicted
  from `self._results` once reflected in the CSV + manifest roll-up.

**Acceptance test:**
```bash
uv run andera run config/tasks/03-github-issue.yaml -i rows_1000.csv &
sleep 30 && kill -TERM %1        # graceful shutdown
uv run andera resume <run_id>     # resumes; total samples processed = 1000
```

---

## Phase 5e — Task library (~1h)

**Goal:** all 5 spec tasks shippable as one YAML each. Without these,
"generality" is a claim, not a demo.

**Deliverables:**
- `config/tasks/01-github-workday-join.yaml` — enrichment across both systems
- `config/tasks/02-linear-screenshots.yaml` — ticket metadata (baseline task)
- `config/tasks/03-github-commits-audit.yaml` — nested PR/CI/Jira drilldown
- `config/tasks/04-linkedin-enrichment.yaml` — concurrency=1 override,
  Google snippet fallback hint
- `config/tasks/05-workday-form-download.yaml` — fill form, submit,
  capture confirmation + any attachments the mock emits
- Each YAML carries the right `task_type` hint so the classifier +
  specialist planner routing hits the right specialist first time.

**Acceptance test:**
```bash
for t in config/tasks/0{1..5}-*.yaml; do
  uv run andera run -t "$t" -i tests/fixtures/$(basename $t .yaml).csv --max-samples 3
done
# each run exits 0 (or reasonable fail for LinkedIn if no cookies); evidence folders present
```

---

## Phase 6 — Planner + task library (~1.5h)

**Goal:** user types English, gets a runnable RunSpec. Complements the
pre-built library in Phase 5e.

**Deliverables:**
- `src/andera/planner/planner.py` — `plan(task_nl: str, input_schema: dict) -> RunSpec`
- `src/andera/planner/prompts/` — system + few-shot
- 5 prebuilt RunSpecs already shipped in Phase 5e.
- API: `POST /api/plan` with `{task_nl, input_file}` → RunSpec preview

**Acceptance test:**
```bash
curl -X POST localhost:8000/api/plan -d '{
  "task_nl":"For each Linear URL, open it, screenshot, get ticket#/assignee/due",
  "input_schema":{"columns":["url"]}
}'
# returns valid RunSpec matching 02-linear-screenshots.yaml within Pydantic validation
```

---

## Phase 7 — Observability: Langfuse + tracing (~1h)

**Goal:** every LLM call traced, every agent run traced.

**Deliverables:**
- `docker/docker-compose.yml` — Langfuse + its Postgres
- `src/andera/observability/langfuse_adapter.py` — wraps LiteLLM calls to emit generations + traces
- `src/andera/observability/otel.py` — spans: `run > sample > node > tool_call`
- `src/andera/observability/trace.py` — JSONL fallback (always-on, local)
- Dashboard: per-sample "View in Langfuse" deep link

**Acceptance test:**
```bash
docker compose -f docker/docker-compose.yml up -d
# set LANGFUSE_PUBLIC_KEY / SECRET_KEY in .env
# run any task → open Langfuse UI → see every LLM call with cost, latency, input, output
```

---

## Phase 8 — Mock Workday + fast-path integrations (~1.5h)

**Goal:** Tasks 1 and 5 can run fully locally; GitHub/Linear integration mode works.

**Deliverables:**
- `services/mock_workday/` — FastAPI + Jinja: `/login`, `/search?q=`, `/people/:id`, `/forms/new`, `/reports/:id` (with tabs + downloadable attachments)
- `scripts/seed_mock_workday.py` — generates 100 fake employees
- `src/andera/tools/integrations/github.py` — REST client for user lookup, commits listing, PR details
- `src/andera/tools/integrations/linear.py` — GraphQL client for ticket metadata
- Planner + agent: when `profile.integrations.<system>.mode == "auto"` and token present, use integration first, browser screenshot second (evidence still required)

**Acceptance test:**
```bash
uv run python services/mock_workday/app.py &
uv run andera run config/tasks/01-github-workday-join.yaml \
  --input tests/fixtures/users_60.csv
# 60 rows, github + workday fields populated; with GITHUB_TOKEN set, ~5x faster
```

---

## Phase 9 — Eval harness + task hardening (~1.5h)

**Goal:** accuracy ≥90% across all 5 tasks; LinkedIn doesn't get blocked; Task 3 nested nav works.

**Deliverables:**
- `src/andera/eval/framework.py` — loads a frozen task set, runs the agent, compares output to ground-truth
- `src/andera/eval/scorers.py` — field match, screenshot-exists, judge-pass
- `src/andera/eval/tasks/` — fixtures for each of the 5 tasks with ground-truth CSVs
- LinkedIn hardening: concurrency=1 for LinkedIn RunSpecs, stealth plugin, Google cached snippet fallback
- Task 3 navigation polish: checks tab, failed-check drill-in, CI page screenshot, Jira link follow
- `pytest eval/` as a gate

**Acceptance test:**
```bash
uv run pytest src/andera/eval -q --gate 0.9
# all 5 tasks ≥90% per the scorers
```

---

## Phase 10 — Demo prep (~0.5h)

**Deliverables:**
- `scripts/demo-all-tasks.sh` — runs all 5 tasks against fixtures end-to-end, opens dashboard
- `README.md` — 5-minute quickstart (install → .env → `docker compose up` → `uv run uvicorn…` → open browser)
- `docs/ARCHITECTURE.md` — 1-pager with the system diagram + swap story
- Screencast backup of all 5 tasks running (for offline demo if live net fails)

**Acceptance test:** fresh-clone dry run: follow README, all 5 tasks green in under 30 min.

---

# Part 5 — Repo layout (target state after Phase 10)

```
andera_worktrial/
├── PLAN.md                               # this file
├── README.md
├── pyproject.toml
├── .env.example
├── .gitignore
├── config/
│   ├── profile.yaml
│   └── tasks/ 01..05 yaml
├── docker/
│   └── docker-compose.yml                # langfuse + pg
├── services/
│   └── mock_workday/                     # FastAPI static site
├── src/andera/
│   ├── contracts/                        # Pydantic types + Protocols (the ONLY cross-module import)
│   ├── config/                           # profile.yaml loader
│   ├── planner/                          # NL → RunSpec
│   ├── orchestrator/                     # run workflow, scheduler, checkpointer
│   ├── agent/                            # LangGraph state graph
│   │   ├── nodes/ {plan,navigate,observe,act,verify,reflect,extract,persist,judge}
│   │   └── grounding.py
│   ├── tools/                            # MCP-shaped tools
│   │   ├── browser.py  artifact.py  evidence.py
│   │   └── integrations/ github.py linear.py
│   ├── browser/                          # BrowserSession protocol + impls
│   │   ├── session.py  local.py  browserbase.py  pool.py  screencast.py
│   ├── models/                           # LiteLLM registry + role routing
│   ├── storage/                          # artifact_store, metadata, audit_log, search
│   ├── queue/
│   ├── credentials/
│   ├── eval/
│   ├── observability/
│   ├── api/                              # FastAPI + HTMX templates
│   └── cli.py
├── scripts/
├── tests/ {unit, integration, e2e}
└── runs/                                 # gitignored: actual evidence
```

---

# Part 6 — Modularity rules (enforced)

1. **Contracts are the only cross-module import.** Need B's internals from A? Refactor into `contracts/`.
2. **Every external dep has a Protocol** + ≥1 implementation. Swap = new impl + yaml change.
3. **Every agent node is pure:** `(state, tools, models) -> state_update`. Unit-testable with fakes.
4. **All tool I/O is Pydantic.** No raw dicts crossing module boundaries.
5. **No circular imports** (enforced by `import-linter`).
6. **Public surface per package declared in `__init__.py` `__all__`.**
7. **DI via config, not module-level globals.**
8. **One concern per file; split at ~300 lines.**
9. **Test file mirrors source file path.**

---

# Part 6.5 — Commit discipline (applies to every phase)

Small, frequent commits with good messages. Every commit goes through `/review` first. This is not negotiable — it's how we stay confident the plan is landing cleanly and how we keep a real story of what changed.

## Cadence

- **One phase = at least one commit**, usually 2–5. If a phase produces >~300 lines of diff, split mid-phase at a natural seam (e.g., Phase 1: one commit for models, one for browser, one for artifact store).
- **Never commit without the acceptance test passing** for whatever you're claiming.
- Commit when something becomes independently reviewable. Avoid "work-in-progress" blobs.

## Pre-commit checklist (Claude Code runs this every time)

1. **Run `/review`** on the staged diff. The review must be clean (no unresolved P1/P2 findings) before committing. If the review flags issues, fix them first, re-stage, re-review.
2. Run `uv run pytest tests/unit -q` on the changed packages — green.
3. Run `uv run ruff check . && uv run ruff format --check .` — clean.
4. Run `uv run mypy src/andera` (if configured) — no new errors.
5. Stage files explicitly by name (never `git add .` or `-A`). Skip `.env`, `data/`, `runs/`, `logs/`.
6. Write the commit message in **Conventional Commits** style.
7. Commit.

## Commit message format

```
<type>(<scope>): <short imperative summary, lowercase, ≤70 chars>

<optional body: WHY this change, not WHAT. 1–3 short paragraphs.>

<optional footer: closes #N, breaking changes, co-author>
```

Allowed `<type>`: `feat`, `fix`, `refactor`, `test`, `docs`, `chore`, `perf`, `build`.
`<scope>` examples: `agent`, `browser`, `orchestrator`, `planner`, `api`, `ui`, `eval`, `contracts`, `config`, `ci`.

**Good examples:**
- `feat(browser): add LocalPlaywrightSession with Stagehand grounding`
- `feat(agent): implement VERIFY and REFLECT nodes with bounded retry`
- `fix(queue): dequeue now respects lease expiry on crash recovery`
- `refactor(contracts): extract Protocol for ArtifactStore out of storage module`
- `test(agent): cover REFLECT retry exhaustion path`
- `chore(plan): add commit discipline section`

**Bad examples (avoid):**
- `update stuff`
- `fix bug`
- `WIP`
- `final changes`

## Gitignore

Seeded at Phase 0:
```
.env
.venv/
__pycache__/
*.pyc
data/
runs/
logs/
node_modules/
.DS_Store
.pytest_cache/
.ruff_cache/
.mypy_cache/
```

## What `/review` looks for before each commit

- Modularity rules (Part 6) — any cross-module import not via `contracts/`?
- Typed Pydantic boundaries — any raw dicts crossing module lines?
- Protocol conformance — does the new impl fully satisfy its Protocol?
- Dead code, unused imports, TODOs without owners
- Error handling at true boundaries only (no over-defensive internal try/except)
- Tests next to code, acceptance test of the phase is wired
- Secret leakage / logging PII
- Commit scope — is the diff one coherent idea? If not, split.

---

# Part 7 — `profile.yaml` (the switch panel)

```yaml
models:
  planner:    { provider: anthropic, model: claude-opus-4-7 }
  navigator:  { provider: anthropic, model: claude-sonnet-4-6 }
  extractor:  { provider: anthropic, model: claude-haiku-4-5-20251001 }
  judge:      { provider: anthropic, model: claude-opus-4-7 }

browser:
  backend: local              # local | browserbase
  headless: false
  concurrency: 4
  stealth: true
  user_data_dir: ./data/browser-profiles

queue:    { backend: sqlite, path: ./data/queue.db }

storage:
  artifacts: { backend: filesystem, root: ./runs }
  metadata:  { backend: sqlite, path: ./data/state.db }

integrations:
  github:  { mode: auto, token: ${GITHUB_TOKEN} }
  linear:  { mode: auto, token: ${LINEAR_TOKEN} }
  jira:    { mode: browser-only }

observability:
  traces:   { backend: jsonl, path: ./logs }
  langfuse: { enabled: true, host: http://localhost:3000 }

eval:
  suite: ./src/andera/eval/tasks
  gate_accuracy: 0.9
```

---

# Part 8 — Risks & mitigations

| Risk | Mitigation |
|---|---|
| LinkedIn bot detection | concurrency=1 for that task, stealth plugin, Google-snippet fallback |
| Real Workday unavailable | mock site under `services/mock_workday/` |
| 1-day scope overrun | hard MVP gate: Task 2 end-to-end by hour 7; tasks 1/3/4/5 layer on top |
| Model outage / key swap during trial | `profile.yaml` swap in 30s (LiteLLM makes any provider drop-in) |
| Browser flakiness | Stagehand + VERIFY node + bounded REFLECT loop + per-host rate limit |
| LLM cost runaway | per-sample token budget; Haiku for extractor; hard abort on budget breach |
| Creds leak in logs | redactor at trace writer; `.env` gitignored; storage_state AES-GCM sealed |

---

# Part 9 — Demo day runbook

1. `docker compose -f docker/docker-compose.yml up -d`  (Langfuse)
2. `uv run uvicorn andera.api.app:app --reload`  (API + dashboard)
3. Open `http://localhost:8000`
4. Connections → connect Linear + GitHub (popup logins)
5. New Run → pick template OR paste NL task → upload CSV → Review plan → Run
6. Show live browser stream + live sample grid
7. Click done samples → carousel + Langfuse trace
8. Download evidence zip + CSV
9. If they throw a new task: type it in NL, Planner drafts RunSpec, run.
10. If they hand over a new API key: flip `profile.yaml` line, rerun. Zero code change.

---

# Part 10 — Glossary

| Term | Meaning |
|---|---|
| **RunSpec** | Our YAML DSL for a task: steps, fields, success criteria. System-agnostic. |
| **Sample** | One input row = one independent unit of work. |
| **LangGraph** | Python lib that builds LLM agents as checkpointable state graphs. |
| **Playwright** | Controls a real Chrome from Python. |
| **Stagehand** | LLM-friendly wrapper on Playwright: `act / observe / extract`. |
| **LiteLLM** | Unified API over Anthropic/OpenAI/Gemini/Ollama. |
| **Langfuse** | LLM observability — every model call with cost/latency/IO. |
| **SoM** | Set-of-Mark: annotated screenshot with numbered element overlays. |
| **CDP screencast** | Chrome DevTools Protocol's built-in frame streamer; powers the live browser view. |
| **MCP** | Model Context Protocol — standard for exposing tools to LLM agents. |
| **Ports & adapters** | Core logic depends on interfaces, not concretions → trivial swaps. |

---

**Build order summary (for Claude Code):**
`Phase 0 → 1 → 2 → 3 → 4 → 5a → 5b → 6 → 7 → 8 → 9 → 10`

Phase 7 (Langfuse docker) can run in background any time after Phase 0. Phase 8 (mock Workday) can parallelize with Phase 5b via a second worktree if desired. Everything else is sequential because each phase's acceptance test depends on the previous.

**Every phase ends with:**
1. Acceptance test passes.
2. `/review` the staged diff — no P1/P2 findings unresolved.
3. Small, well-scoped commit(s) per Part 6.5. Split into multiple commits if the diff covers more than one idea.
4. `git log --oneline` should always read like a coherent narrative of the build.
