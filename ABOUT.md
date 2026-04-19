# About Andera (this project)

## What Andera (the company) asked for

From the work-trial spec:

> Build a **General Browser Agent** that takes a natural-language task
> plus an input file (CSV/Excel) and produces **audit-grade evidence**:
> a per-sample folder with screenshots + extracted data, plus an
> aggregate CSV. Must be **system-agnostic** (any browser-reachable
> system). Judged on **accuracy > generality > scalability >
> consistency > speed**.

Two execution modes are called out:
- **Manual UI** — the agent drives a real browser.
- **Integration-assisted** — when API credentials exist, prefer the
  API for speed; still produce evidence.

The spec also hints: *"leverage a file system and many sub-agents."*

## What this project does today

### The pipeline (one command)

```
andera run -t <task.yaml> -i <input.csv>
```

For each row in the input CSV:

1. **Classify** the task (Haiku) → one of `extract / form_fill / list_iter / navigate`.
2. **Plan** (Opus) — produces an ordered list of browser actions, using a specialist system prompt chosen by the task type. Plan is cached by `(task_hash, schema_hash, url_pattern)` so 1000 samples of the same task trigger the planner once.
3. **Act** — executes one plan step at a time via Playwright. Every action goes through per-host token-bucket rate limiting + (optionally) stealth-patched Chromium.
4. **Observe** — takes a rich DOM snapshot: inner text + structural outline + interactive-element list with bounding boxes + (optionally) a Set-of-Mark annotated screenshot that overlays numbered boxes on clickable elements (walks shadow DOM + same-origin iframes).
5. **Verify** (Sonnet) — did the last action actually work? If not, reflect (≤3 times); if the plan looks wrong, re-plan.
6. **Extract** (Haiku) — pull the target fields from accumulated observations, enforced to the JSON schema on the task.
7. **Judge** (Opus) — `pass / fail / uncertain` with a reason.
8. **Persist** — screenshots are saved content-addressed by `sha256(bytes)` so identical evidence is deduplicated; each sample appends one line to `samples.jsonl`; an append-only hash-chained audit log records every run/sample event; `RUN_MANIFEST.json` pins every artifact hash + the audit root hash.

At the end: `runs/<run_id>/output.csv` with one row per input row, and `runs/<run_id>/RUN_MANIFEST.json` with the full tamper-evident summary.

### What's runnable today

- **CLI**: `andera run / resume / login / verify / check`.
- **Web dashboard** (localhost:8000) with live run progress, sample evidence viewer, event stream, and optional live browser screencast.
- **Mock Workday** service (localhost:8001) for tasks 1 and 5 without a real Workday tenant.
- **Eval harness** (`pytest tests/eval/`) with a composite scorer (field match × evidence × judge) and a pass-rate gate.
- **Sealed credentials**: `andera login <host> --url <...>` opens a headed browser, waits for you to sign in, saves the session state AES-GCM encrypted per host.
- **186 tests** — all green.

### The 5 spec tasks

All 5 are shipped as YAMLs in `config/tasks/`, each declaring its `task_type` so the classifier dispatches to the right specialist planner out of the gate. Fixtures under `tests/fixtures/`.

## How platform-agnostic is this really?

Honest answer: **structurally agnostic, empirically under-tested.**

### Why it's structurally agnostic

There is **zero system-specific code** anywhere in `src/andera/`. Grep the codebase for strings like "linear.app", "github.com", "workday", "linkedin" — they appear only in task YAMLs (config, not code) and tests. The agent's vocabulary is:

- `goto(url)`, `click(selector_or_text)`, `type(selector, value)`
- `screenshot(name)`, `extract(schema)`, `snapshot()`
- `mark_and_screenshot()`, `click_mark(id)` — coordinate-based fallback for unknown UIs

Everything above those primitives is LLM-driven: the planner picks the actions, the navigator resolves targets, the extractor fills the schema. **No hand-written rules for any specific platform.**

The one-config `profile.yaml` switches model tier, browser backend, queue, storage, integrations. Nothing about the system-under-test is compiled into the pipeline.

### Why it's empirically under-tested

The agent has only been **exercised end-to-end** against:
- `example.com` (Phase 1 smoke)
- A public GitHub issue page (Phase 2 acceptance)

The other 5 tasks have correct YAMLs + fixtures, but **no one has confirmed** that the agent successfully extracts from Linear / Workday / LinkedIn / nested GitHub commits / form-fill Workday. Based on how the pipeline is built, expected behavior:

| Task | Risk of failure | Why |
|---|---|---|
| Simple public pages (GitHub, docs sites) | Low | Phase 1+2 proven similar case |
| Linear (public or logged-in) | Low-medium | Standard React SPA; snapshot + SoM should handle it |
| GitHub commits + checks + CI drill-in | Medium | Lots of tabs, dynamic content; plan may need >8 steps and replan |
| Workday mock (our service) | Low-medium | Server-rendered Jinja; we built the mock ourselves |
| Workday (real tenant) | **Unknown** | Heavy iframes, custom widgets, virtualized DOM — not tested |
| LinkedIn | **High** | Aggressive bot detection; stealth helps but isn't a silver bullet |

### What's definitely NOT handled

The agent can **not currently do** any of these:
- Multi-factor auth flows (TOTP, SMS OTP, magic links)
- Captchas of any kind
- File uploads (only downloads via link-click)
- PDF reading (the artifact is stored, but no OCR/parsing)
- Scroll-to-load infinite pagination (plans assume standard "Next" buttons)
- Rich widgets (date pickers that don't accept typed input, rich text editors, drag-and-drop)
- Cross-origin iframes (logged as unreachable; we can still screenshot, not click inside)
- Shadow DOM with `mode: "closed"` (we can't enter closed shadow roots from JS)
- Virtualized lists where the target row is below the viewport (no scroll-to-find-in-list yet)
- Recovery when the browser crashes mid-sample (sample fails, is nack'd, retried via queue)

### The generality claim, calibrated

- **Will work on any page that**: renders substantive content in the DOM (incl. shadow/iframe same-origin), accepts `click` on visible elements with accessible names or SoM marks, and exposes target fields in text the LLM can parse from the snapshot.

- **Will probably work on**: most standard React/Vue/Angular apps (Linear, GitHub, GitLab, Jira, Notion, Airtable, Asana, Zendesk, Intercom) with sealed login.

- **May fail on**: heavy enterprise UIs with custom rendering engines (Workday real tenant, Oracle Fusion, SAP Fiori), sites with closed shadow DOM, anything requiring MFA beyond sealed session.

- **Will not work on**: anything behind a captcha, file uploads, anything needing cross-app OS interaction, rich-widget-only input.

## The "any task" question

Can it do any task you throw at it? Two failure modes to worry about:

1. **Task too complex for the planner to represent in its action vocabulary.** Example: "build a pivot table from this spreadsheet and download as PDF" — the primitives don't cover cell-level Excel operations in a browser.

2. **Task requires reasoning the planner can't do from a snapshot.** Example: "find all commits that broke the build and summarize what went wrong" — this needs cross-page reasoning and inference; the current graph handles one URL at a time.

For tasks that map cleanly onto **"visit URLs, follow links, click through UIs, fill forms, extract structured fields"** — yes, it should work. For tasks that need agentic planning across arbitrary state (multi-hour investigations, correlating across sites, running arbitrary scripts) — no, this is not that agent.

## The unexercised corner

The honest biggest gap: we have **never run all 5 spec tasks end-to-end** against real targets with real API keys. The eval harness is ready to measure when that happens, but no one has pressed the button. That's the single highest-value next test, above any architectural refactor.

## Related reading

- `ARCHITECTURE.md` — mermaid diagram + where every component lives in the process (monolith today, not microservices).
- `PLAN.md` — phase-by-phase build log.
- `README.md` — quickstart + CLI reference.
