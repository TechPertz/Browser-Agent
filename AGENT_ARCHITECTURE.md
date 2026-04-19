# The Andera Agent — Architecture Reference

This document is exclusively about the **agent**: the thing that takes one
sample (one row from the input CSV) and drives a browser until that sample
either passes, fails, or exhausts its retry budget. No orchestration, no
Docker, no API. Just the agent.

**Concepts covered** (in order):

1. The runtime model — LangGraph, and why
2. `AgentState` — the shape of what flows between nodes
3. Reducers — why some state keys append and others replace
4. `AgentDeps` — the injected capabilities every node sees
5. Nodes — exact inputs/outputs of `classify / plan / act / observe / verify / extract / judge`
6. Edges — how the nodes are wired
7. Routers — conditional edges that make loops possible
8. Tools — the typed I/O primitives the agent actually uses
9. Prompts — role prompts + specialist prompts
10. Caches — plan cache + classify memoization
11. Retry budgets — reflection, replan, extract retries, judge feedback
12. Checkpointing — why crashes don't mean starting over
13. Observation compaction — the accuracy invariant
14. Life of one sample — an annotated trace

---

## 1. The runtime model

The agent is a **state machine** implemented with
[LangGraph](https://langchain-ai.github.io/langgraph/). A `StateGraph` is a
directed graph where:

- Each **node** is a callable: `async def node(state: AgentState) -> dict`.
- Each **edge** connects two nodes unconditionally.
- A **conditional edge** reads the state and picks which node runs next.
- The graph is **compiled with a checkpointer** so every state transition is
  written to durable storage. A crashed sample resumes from the last node
  that completed.

Why a graph + checkpointer instead of a plain async function? Because
agents need **bounded loops** (verifier retries the same step, planner
replans on repeated failure, judge sends the extractor back once). A
linear function can't represent that cleanly; a state machine can.

The compiled graph is invoked per sample:

```python
final_state = await compiled_graph.ainvoke(
    initial_state,
    config={"configurable": {"thread_id": sample_id}},
)
```

`thread_id` is the checkpoint key. One sample = one thread. LangGraph
uses this to persist and (on resume) replay.

Relevant files:

- `src/andera/agent/graph.py` — `build_graph(deps)`, `run_sample(...)`
- `src/andera/agent/state.py` — the state shape

---

## 2. `AgentState`

`AgentState` is a `TypedDict`. LangGraph picks up the type hints and
inspects them to find `Annotated[..., reducer]` — which fields to *append*
versus which to *replace* across transitions.

Defined in `src/andera/agent/state.py`:

```python
class AgentState(TypedDict, total=False):
    # --- inputs (set once at START) ---
    run_id: str
    sample_id: str
    task_prompt: str                   # NL task description
    input_data: dict[str, Any]         # one row of the input file
    start_url: str                     # optional starting URL
    extract_schema: dict[str, Any]     # JSON schema of target fields

    # --- agent working memory ---
    plan: list[PlanStep]               # ordered steps proposed by planner
    step_index: int                    # which step we're on
    observations: list[dict[str, Any]] # REPLACES (compacted in place)
    tool_calls: Annotated[list, _append]   # APPENDS (audit trail)
    evidence: Annotated[list, _append]     # APPENDS (Artifact dumps)
    extracted: dict[str, Any]          # final extracted data

    # --- retry budgets ---
    reflect_count: int                 # total reflections on a bad step (≤3)
    consecutive_fails: int             # verify-fails on the SAME step (≤2 → replan)

    # --- control / result ---
    status: Status                     # pending|planning|acting|verifying|...
    verdict: Literal["pass","fail","uncertain"]
    verdict_reason: str
    error: str

    # --- cross-node signals ---
    last_tool_error: str               # act → verify: definitive failure
    extract_errors: list[str]          # jsonschema errors for the extractor to fix
    judge_feedback: str                # judge → extract: "fix X"
    task_type: str                     # classify → plan: picks specialist prompt
    plan_cache_hit: bool               # telemetry
```

A node returns a `dict` of just the keys it wants to update. LangGraph
merges: scalars replace, annotated lists reduce via their reducer.

### `PlanStep`

A single action the agent will execute. Very small:

```python
class PlanStep(TypedDict, total=False):
    action: str       # goto | click | type | screenshot | extract | done
    target: str       # URL | selector | text | mark_id | schema key
    value: str        # for type actions
    rationale: str    # optional human-readable explanation
```

---

## 3. Reducers

Most fields in `AgentState` **replace** on update (the default LangGraph
behavior). A handful **append** because we want a cumulative record:

| Field | Reducer | Why |
|---|---|---|
| `tool_calls` | `_append` | Full tool-call history is audit evidence. |
| `evidence` | `_append` | Artifact receipts; never drop one. |
| `observations` | **none — plain list** | We need to *replace* with a compacted version. See §13. |
| everything else | replace | Normal TypedDict semantics. |

`_append` is literally `left + right`. Defined once in `state.py`.

---

## 4. `AgentDeps`

Every node needs three kinds of dependencies:
- **LLMs** (one per role)
- **Browser** (as a typed tool surface, not a raw Playwright handle)
- **Auxiliary state** (plan cache, classifier)

They're bundled in a dataclass and passed to every `make_*_node(deps)`
factory. Defined in `src/andera/agent/nodes.py`:

```python
@dataclass
class AgentDeps:
    planner:    ChatModel       # Opus 4.7 — builds the plan
    navigator:  ChatModel       # Sonnet 4.6 — verifies each step
    extractor:  ChatModel       # Haiku 4.5 — fills the schema
    judge:      ChatModel       # Opus 4.7 — final pass/fail verdict
    browser:    BrowserTools    # typed wrapper around BrowserSession
    plan_cache: PlanCache | None = None
    classifier: ChatModel | None = None  # usually reused = extractor
```

`ChatModel` is a `Protocol` in `contracts/protocols.py`:

```python
class ChatModel(Protocol):
    async def complete(
        self,
        messages: list[dict[str, Any]],
        schema: dict[str, Any] | None = None,
        **kwargs,
    ) -> dict[str, Any]: ...
```

The concrete impl is `LiteLLMChatModel` in `models/adapters/litellm_adapter.py`.
One adapter, every provider (Anthropic, OpenAI, Gemini, Ollama). Role →
model mapping lives in `config/profile.yaml:models`.

---

## 5. Nodes

All factories live in `src/andera/agent/nodes.py`. Each returns an
`async def node(state) -> dict` closure that captured `deps`.

### 5.1 `classify`

**Purpose.** Determine the *shape* of the task so the planner uses the
right specialist prompt.

**Input:** `task_prompt`, `extract_schema`.
**Output:** `{"task_type": "extract"|"form_fill"|"list_iter"|"navigate"|"unknown"}`.
**LLM:** classifier (Haiku).
**Caching:** Memoized in a closure dict keyed on
`classify_cache_key(prompt, schema)`. Every sample of a run classifies **once** total — N-1 calls are free dict lookups.
**Pass-through:** If `state["task_type"]` is already set (resume case), noop.

```python
async def classify(state) -> dict:
    if state.get("task_type"): return {}
    key = classify_cache_key(state["task_prompt"], state["extract_schema"])
    if key in _memo: return {"task_type": _memo[key]}
    ...
```

### 5.2 `plan`

**Purpose.** Produce an ordered list of `PlanStep` to accomplish the task.

**Input:** `task_prompt`, `input_data`, `start_url`, `extract_schema`, `task_type`.
**Output:** `{"plan": [...], "step_index": 0, "status": "acting", "reflect_count": 0, "consecutive_fails": 0, "plan_cache_hit": bool}`.
**LLM:** planner (Opus).
**Caching:** `PlanCache` keyed on `sha256(prompt + canonical_schema + url_pattern)`. `url_pattern` collapses IDs (`/issue/ENG-1` → `/issue/:id`) so 1000 samples of one task hit the cache 999 times. First sample writes the plan, then in-memory fast path.
**Specialist selection:** `prompts.system_prompt_for(task_type)` picks one of 5 system prompts (`extract / form_fill / list_iter / navigate / generic`).
**Safety:** Output is JSON-parsed with a fence-tolerant parser; non-list output sets `status=failed`.

### 5.3 `act`

**Purpose.** Execute the current plan step against the browser.

**Input:** `plan`, `step_index`.
**Output:** updates `tool_calls`, optionally `evidence`, optionally `observations` (for `extract` action). Sets `status` for routing.

**Branch per `action`:**

| action | Browser tool | Effect |
|---|---|---|
| `goto` | `BrowserTools.goto(GotoArgs(url=target))` | Rate-limited navigation |
| `click` | `BrowserTools.click(ClickArgs(...))` | Strict role-match; ambiguous text raises |
| `type` | `BrowserTools.type(TypeArgs(...))` | `page.fill(selector, value)` |
| `screenshot` | `BrowserTools.screenshot(...)` | Content-addressed PNG → `evidence` |
| `extract` | `BrowserTools.extract(...)` | Appends `{kind: "extract", data: ...}` to `observations` |
| `done` | — | Sets `status: extracting`, routes out of the loop |

**Tool error handling.** If the `ToolResult.status == "error"`, `act`
increments `consecutive_fails` and stashes `last_tool_error`. Verifier
reads this and short-circuits (no LLM call) — a tool error is never
papered over by a lenient verifier verdict. This is a deliberate
accuracy fix (see `tests/unit/agent/test_extractor_retry.py::test_garbled_verifier_does_not_silently_pass`).

### 5.4 `observe`

**Purpose.** Take a fresh page snapshot so the verifier has current
ground truth.

**Input:** previous `observations`.
**Output:** `{"observations": compact_observations(observations + [new_snap])}`.

Calls `BrowserTools.snapshot()` → `build_snapshot(page)` which returns
`{url, title, inner_text, outline, interactive, page_state}`. See
`browser/grounding.py`.

Runs `compact_observations` defensively — even if this step doesn't
overflow the window, the next one might.

### 5.5 `verify`

**Purpose.** Did the last action actually advance the plan?

**Two paths:**
1. **Tool error path.** `state["last_tool_error"]` is set → `ok=False`, skip LLM.
2. **Normal path.** Build a prompt containing the task goal, the current plan step, the last `ToolCall`, and the post-action snapshot. Navigator LLM returns `{"ok": bool, "reason": str}`.

**Safety default.** Garbled LLM output → `ok=False` (never silently pass — another accuracy fix).

**State transitions:**

| Condition | Next state |
|---|---|
| `ok=True` | `step_index + 1`, `status=acting`, `consecutive_fails=0` |
| `ok=False`, `reflect_count < 3`, `consecutive_fails < 2` | `status=acting`, `reflect_count++`, `consecutive_fails++` — retry same step |
| `ok=False`, `consecutive_fails >= 2`, `reflect_count < 3` | `status=replanning`, `reflect_count++` — escalate to planner |
| `ok=False`, `reflect_count >= 3` | `status=failed, error="reflection cap reached"` |

### 5.6 `extract`

**Purpose.** Fill the `extract_schema` from accumulated observations.

**Payload projection.** `prompts.extractor_user(...)` projects each
observation to a bounded shape (`snapshot` → `{url, title, inner_text[:2000], interactive_names}`, `extract` → kept whole). Then tail-reverses and
serializes. Fixes a bug where the old implementation sliced
`json.dumps(...)[:8000]` mid-structure, giving the LLM garbage.

**Retry loop.** Up to `EXTRACT_RETRY_MAX=2` retries:

```
attempt = extractor.complete(messages, schema=target_schema)
errors = jsonschema.iter_errors(parsed, target_schema)
if not errors: break
# Next attempt gets: prior output + validation errors + judge_feedback
```

Judge feedback (§5.7) is propagated into the extractor retry so one
round of judge-catches-mistake → extractor-fixes-it is possible.

### 5.7 `judge`

**Purpose.** Final pass/fail verdict with an evidence-citation check.

**Input:** `task_prompt`, `extracted`, `evidence`.
**LLM:** judge (Opus).
**Output:** `{"verdict", "verdict_reason"}`.

**Feedback loop.** On `verdict in {"fail", "uncertain"}`, if
`reflect_count < 3`, sets `status=extracting` and `judge_feedback=reason`
— router sends the flow back to `extract` with the judge's critique
embedded in the prompt. Else `status=done`.

---

## 6. Edges

Defined in `src/andera/agent/graph.py::build_graph`. The static wiring:

```
 START
   │
   ▼
 classify ──(unconditional)──▶ plan
                                │
                                ▼
                      (conditional) route_after_plan
                       ├─ "act"    → act
                       └─ "failed" → END
                                │
                                ▼
                              act
                                │
                      (conditional) route_after_act
                       ├─ "observe" → observe ──▶ verify
                       ├─ "extract" → extract
                       └─ "failed"  → END
                                │
                  (from verify, conditional) route_after_verify
                       ├─ "act"     → act       (continue plan)
                       ├─ "extract" → extract   (plan done)
                       ├─ "plan"    → plan      (REPLAN escalation)
                       └─ "failed"  → END
                                │
                                ▼
                              extract
                                │
                      (conditional) route_after_extract
                       ├─ "judge"  → judge
                       └─ "failed" → END
                                │
                                ▼
                              judge
                                │
                      (conditional) route_after_judge
                       ├─ "extract" → extract   (judge feedback loop)
                       └─ "end"     → END
```

---

## 7. Routers

The five conditional edges above are simple pure functions of `state`:

### `route_after_plan(state)`
```python
return "failed" if state.get("status") == "failed" else "act"
```

### `route_after_act(state)`
```python
s = state.get("status")
if s == "failed":     return "failed"
if s == "extracting": return "extract"  # 'done' action short-circuit
return "observe"
```

### `route_after_verify(state)`
```python
s = state.get("status")
if s == "failed":     return "failed"
if s == "replanning": return "plan"
if state["step_index"] >= len(state["plan"]): return "extract"
return "act"
```

### `route_after_extract(state)`
```python
return "failed" if state.get("status") == "failed" else "judge"
```

### `route_after_judge(state)`
```python
return "extract" if state.get("status") == "extracting" else "end"
```

These are the only control-flow decisions in the entire agent. Every
other behavior is expressed in the node bodies.

---

## 8. Tools

Tools are the agent's I/O — the only way nodes talk to the outside world
(browser, filesystem). Every tool returns a **`ToolResult` envelope**
with `status`, `data`, `error`, `elapsed_ms`, `call_id`. Defined in
`src/andera/contracts/tools.py`.

### Tool categories

**Browser tools** (`src/andera/tools/browser.py`): wrap a `BrowserSession`:

| Tool | Pydantic args | Backing call |
|---|---|---|
| `goto` | `GotoArgs(url)` | `session.goto(url)` (rate-limited per host) |
| `click` | `ClickArgs(selector_or_text)` | Strict role-match fallback |
| `type` | `TypeArgs(selector, value)` | `page.fill(selector, value)` |
| `screenshot` | `ScreenshotArgs(name)` | `page.screenshot()` → content-addressed Artifact |
| `extract` | `ExtractArgs(json_schema)` | Minimal scaffold — real extraction is in the `extract` node |
| `snapshot` | — | `build_snapshot(page)` — rich grounding |

**Artifact tools** (`src/andera/tools/artifact.py`): typed `put/get` against
`FilesystemArtifactStore`. Critical: `put()`'s audit-log representation
substitutes raw bytes for a `size: int` so evidence bytes never appear
in the audit payload.

### Why typed envelopes?

Every tool call goes through `src/andera/tools/_runner.py::invoke`,
which:

1. Constructs a `ToolCall` with a UUID.
2. Times the call.
3. Wraps exceptions → `ToolResult{status: "error", error: str}` — no
   naked exceptions escape.
4. Returns a `ToolResult{status: "ok", data: dict, elapsed_ms: int}`.

This uniform shape is what lets the audit log record every call without
special-casing per tool, and what lets `act` detect tool errors
deterministically (`r.status == "error"`).

### The hexagonal seam

`BrowserSession` is a `Protocol` (`contracts/protocols.py`). Today's
impl is `LocalPlaywrightSession`. Swaps:
- `BrowserbaseSession` — cloud browsers.
- `SetOfMarkSession` — overlays numbered marks for coordinate-based clicks.

None of the nodes change. They depend on the Protocol.

---

## 9. Prompts

All in `src/andera/agent/prompts.py` + `src/andera/agent/specialists/prompts.py`.

### Role prompts

| Role | System prompt | Specialist-swapped? |
|---|---|---|
| Planner | `PLANNER_SYSTEM` (generic) | **Yes** — one of 5 specialist variants picked by `task_type` |
| Navigator (verifier) | `VERIFIER_SYSTEM` | No |
| Extractor | `EXTRACTOR_SYSTEM` | No |
| Judge | `JUDGE_SYSTEM` | No |
| Classifier | `CLASSIFIER_SYSTEM` (in `classify.py`) | No |

### Specialist prompts

When `task_type` is set, the planner's system prompt swaps to one of:

| Task type | Prompt | Plan shape |
|---|---|---|
| `extract` | `EXTRACT_SPECIALIST_SYSTEM` | 3–5 steps: goto → screenshot → extract → done |
| `form_fill` | `FORM_FILL_SPECIALIST_SYSTEM` | goto → screenshot → N×type → submit → screenshot → extract → done |
| `list_iter` | `LIST_ITER_SPECIALIST_SYSTEM` | goto → screenshot → N×(click item → screenshot → extract → back) → done |
| `navigate` | `NAVIGATE_SPECIALIST_SYSTEM` | Free-form multi-step; screenshot before and after every click |
| `unknown` | `GENERIC_SPECIALIST_SYSTEM` | Free-form, ≤ 8 steps |

All specialists output **the same action vocabulary**. Specialization is
in the plan shape, not the runtime. Same graph executes any of them.

### User-message builders

Pure functions that interpolate state into a user prompt — no
`format()` stringly-typed bugs. Examples:

- `planner_user(task_prompt, input_data, start_url, schema)` — initial plan.
- `verifier_user(task_prompt, current_step, last_action, snapshot)` — step verification. Note `task_prompt` + `current_step` are passed so the verifier knows *what it's checking against* (accuracy fix).
- `extractor_user(observations, schema, judge_feedback=None, prior_extraction=None, validation_errors=None)` — retry-aware.
- `judge_user(task_prompt, extracted, evidence)` — final review.

---

## 10. Caches

### 10.1 Plan cache

`src/andera/agent/plan_cache.py`.

Key: `sha256(task_prompt + canonical(schema) + url_pattern(start_url))`.
- `canonical(schema)` — sorted-keys, no whitespace JSON.
- `url_pattern(url)` — normalizes `/issue/ENG-123` → `/issue/:id`. Two
  samples with different numeric/ID segments hit the same key. Query
  strings stripped.

Storage:
- `_mem: dict[str, list[dict]]` in memory — O(1) hit after first write.
- `data/plan_cache/<key>.json` on disk — survives process restarts,
  shared across agent containers via bind mount (or via Redis when
  swapped in production).

Writes are atomic (`tmp` + `rename`), so concurrent putters can't
produce a half-written plan with a valid hash name.

### 10.2 Classify memo

`make_classify_node(deps)` holds a closure dict `_memo: dict[str, str]`
keyed on `classify_cache_key(prompt, schema)`. One Haiku call per
(prompt, schema) pair per agent process. On a 1000-sample run with
3 agent containers, that's 3 classifier calls total instead of 1000.

---

## 11. Retry budgets

Three separate retry surfaces — each bounded, each for a distinct
failure mode:

| Budget | Constant | What it covers |
|---|---|---|
| `reflect_count` ≤ 3 | `REFLECT_MAX` | Total verifier-said-no on this sample. Caps at 3 to prevent infinite loops. |
| `consecutive_fails` ≥ 2 | `REPLAN_AFTER_CONSECUTIVE_FAILS` | Same step fails twice in a row → escalate to planner (replan). Fresh plan = fresh chances, but still inside `reflect_count`. |
| `EXTRACT_RETRY_MAX = 2` | node-local | Extractor emitted schema-invalid output. Retries with errors embedded. |
| Judge feedback | shares `reflect_count` budget | Judge says fail/uncertain → one re-extract with feedback. Bounded by reflect_count so judge can't loop forever. |

Also at the LLM level: LiteLLM retries on 429/timeout 3× with
exponential backoff (`num_retries=3` in `LiteLLMChatModel`). Invisible
to the agent.

---

## 12. Checkpointing

Every state transition is persisted by LangGraph's checkpointer:

```python
# src/andera/agent/graph.py::run_sample
if postgres_url:
    async with AsyncPostgresSaver.from_conn_string(postgres_url) as saver:
        graph = build_graph(deps).compile(checkpointer=saver)
        return await graph.ainvoke(initial_state, config={"configurable": {"thread_id": sample_id}})
else:
    async with AsyncSqliteSaver.from_conn_string(ckpt_db) as saver:
        ...
```

- **SQLite path**: `data/<run_id>.ckpt.db`. Used in local dev / laptop
  mode.
- **Postgres path**: shared `checkpoints` + `checkpoint_blobs` +
  `checkpoint_writes` tables across all runs, used in the Docker compose
  stack.

Thread id = sample id. If the process crashes mid-sample, resuming with
the same thread id replays state from the last completed node. The
agent resumes cleanly from a crash between `act` and `observe`, or
between `observe` and `verify` — LLM calls don't re-execute unless
their node didn't complete.

Checkpointing tables are created idempotently by
`storage/pg_migrate.py::migrate` (called from the API lifespan in the
compose stack, or explicitly via `andera migrate`).

---

## 13. Observation compaction

**The accuracy-critical invariant.**

`observations` is a plain list (no reducer) so we can *replace* it with a
compacted version. Defined in `src/andera/agent/state.py::compact_observations`.

Rules:

- **`kind == "extract"` entries are NEVER compacted.** They carry per-item
  data the final extractor aggregates. Summarizing them into 1-liners
  used to silently drop list_iter results — fixed with a pinned
  regression test.
- **Snapshots** are window-bounded to the most recent
  `OBSERVATION_WINDOW = 5`. Older snapshots get replaced by a
  `{kind: "snapshot.abstract", summary: "snapshot: <title> @ <url>"}`
  breadcrumb.
- **Non-extract, non-snapshot** entries follow the snapshot rule.

Return shape: `[all extracts] + [abstract-older-snapshots] + [last-5-snapshots]`.

This single function is why a 60-item list_iter flow doesn't lose item
0's extracted data by the time the agent extracts item 59.

Test pins the invariant: `tests/unit/agent/test_state_compaction.py::test_extract_observations_never_compacted`.

---

## 14. Life of one sample — annotated trace

Imagine task 02 (Linear tickets). Input row: `{url: "https://linear.app/a/issue/LIN-1"}`.

```
┌─────────────────────────────────────────────────────────────┐
│ initial_state = {                                           │
│   run_id, sample_id,                                        │
│   task_prompt: "Navigate to each Linear URL...",            │
│   input_data: {url: "https://linear.app/a/issue/LIN-1"},    │
│   start_url: "https://linear.app/a/issue/LIN-1",            │
│   extract_schema: {type: object, required: [ticket_no,      │
│                                              assignee], ...},│
│   status: "pending",                                        │
│ }                                                           │
└─────────────────────────────────────────────────────────────┘
                         │
                         ▼
 ┌──────────────────────────────────────────────────────────┐
 │ classify                                                 │
 │  memo miss → Haiku call → "extract"                      │
 │  subsequent samples of same task: memo HIT → 0 calls     │
 │ state.task_type = "extract"                              │
 └──────────────────────────────────────────────────────────┘
                         │
                         ▼
 ┌──────────────────────────────────────────────────────────┐
 │ plan                                                     │
 │  cache_key = sha256(prompt + schema + url_pattern)       │
 │  url_pattern("linear.app/a/issue/LIN-1")                 │
 │    = "linear.app/a/issue/:id"                            │
 │  cache MISS → Opus call with EXTRACT specialist prompt   │
 │  plan = [                                                │
 │    {action: goto,       target: <url>},                  │
 │    {action: screenshot, target: "ticket_page"},          │
 │    {action: extract,    target: "fields"},               │
 │    {action: done,       target: "ok"},                   │
 │  ]                                                       │
 │  plan_cache.put(key, plan)                               │
 │ state.plan = [...]; state.step_index = 0                 │
 └──────────────────────────────────────────────────────────┘
                         │   route_after_plan → "act"
                         ▼
 ┌──────────────────────────────────────────────────────────┐
 │ act (step 0 = goto)                                      │
 │  BrowserTools.goto(url) → rate limiter → page.goto(url)  │
 │  ToolResult{status: ok, elapsed_ms: 430}                 │
 │ state.tool_calls += [result]; state.status = verifying   │
 └──────────────────────────────────────────────────────────┘
                         │   route_after_act → "observe"
                         ▼
 ┌──────────────────────────────────────────────────────────┐
 │ observe                                                  │
 │  build_snapshot(page) → {url, title, inner_text,         │
 │                          outline, interactive,           │
 │                          page_state}                     │
 │  compact_observations adds this snapshot                 │
 │ state.observations = [snapshot_0]                        │
 └──────────────────────────────────────────────────────────┘
                         │
                         ▼
 ┌──────────────────────────────────────────────────────────┐
 │ verify                                                   │
 │  Navigator (Sonnet):                                     │
 │    "Task goal: ... / Current step: goto / Last action /  │
 │     Resulting snapshot title+url+text" → {ok: true}      │
 │  state.step_index = 1; consecutive_fails = 0             │
 └──────────────────────────────────────────────────────────┘
                         │   route_after_verify → "act"
                         ▼
  (repeat for screenshot, extract)
                         │
                         ▼ done action, route_after_act → "extract"
 ┌──────────────────────────────────────────────────────────┐
 │ extract                                                  │
 │  attempt 1: extractor (Haiku) with schema, observations  │
 │     → parse → jsonschema validate                        │
 │  valid? → done. invalid? → retry with errors in prompt   │
 │ state.extracted = {ticket_no: "LIN-1", assignee: "..."}  │
 └──────────────────────────────────────────────────────────┘
                         │   route_after_extract → "judge"
                         ▼
 ┌──────────────────────────────────────────────────────────┐
 │ judge                                                    │
 │  Judge (Opus): "Task / Extracted / Evidence" →           │
 │     {verdict: "pass", reason: "all required fields..."}  │
 │ state.verdict = "pass"; status = done                    │
 └──────────────────────────────────────────────────────────┘
                         │   route_after_judge → "end"
                         ▼
                        END
                         │
           ┌─────────────┴─────────────┐
           ▼                           ▼
  samples.jsonl                runs/<id>/blobs/
  (one line)                   (screenshot PNG, sha-addressed)
```

### Failure path: verifier says no twice on the same step

- `verify` returns `ok=False` → `consecutive_fails = 1`, retry `act`.
- `act` re-runs step — still fails → `consecutive_fails = 2`.
- `verify` sees `consecutive_fails >= REPLAN_AFTER_CONSECUTIVE_FAILS`
  → `status = replanning`, `reflect_count++`.
- `route_after_verify` routes to `plan` (not `act`).
- `plan` re-runs with the same cache key → **cache HIT → same plan**.
  Hmm. TODO: invalidate plan cache on replan (known limitation).
- If `reflect_count >= REFLECT_MAX`, `status = failed, error = "reflection cap reached"` → END.

### Failure path: judge says fail

- `extract` produces output, passes schema.
- `judge` returns `{verdict: "fail", reason: "author field doesn't match evidence"}`.
- `judge` node sets `status=extracting`, `judge_feedback=reason`,
  `reflect_count++`.
- `route_after_judge` → `extract`.
- `extract` re-runs with the judge's complaint embedded in the user
  prompt and `prior_extraction` shown.
- If judge passes this time → `status=done, verdict=pass`.
- If judge fails again AND `reflect_count >= 3` → `status=done,
  verdict=fail`. The loop is bounded.

---

## 15. Where to go next (in this repo)

- `src/andera/agent/nodes.py` — start here; every node is ~30 lines.
- `src/andera/agent/prompts.py` — read a few to see the style.
- `tests/unit/agent/test_graph.py` — full state-machine trace with a scripted LLM + FakeSession.
- `tests/unit/agent/test_extractor_retry.py` — pins schema retry + judge feedback + garbled-verifier regression.
- `tests/unit/agent/test_state_compaction.py` — pins the extract-preservation invariant.

Three files, 700 lines total, is the whole agent.

## Related docs

- `ABOUT.md` — honest capability report (what the agent can/can't do today).
- `ARCHITECTURE.md` — system topology (orchestrator, queue, API). Zooms out from the agent.
- `SYSTEM_DESIGN.md` — per-file walkthrough of the whole repo.
- `README.md` — quickstart.
